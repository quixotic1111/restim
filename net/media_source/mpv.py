"""mpv, over its JSON IPC socket.

Why this exists alongside the HTTP players: mpv reports `time-pos` as an
absolute float in seconds. There is no length to guess and no fraction to
rescale, which is the failure mode the HTTP sources are prone to — measured on
one rig, VLC's `position` fraction behaved as though a 12.000 s clip were
11.855 s, so reconstructing `duration * fraction` ran +1.2% fast.

It also PUSHES property changes rather than answering polls, so position
updates arrive at the video frame rate instead of on a poll interval. Measured
on the same rig: mpv reported 0.997 x wall with 15 ms of jitter (frame
quantisation), against VLC's 149 ms.

Start mpv with an IPC socket for this to connect to:

    mpv --input-ipc-server=/tmp/mpv-socket <file>
"""
import json
import logging
import os
import time

from PySide6 import QtCore
from PySide6.QtNetwork import QLocalSocket

from net.media_source.mediasource import MediaSource, MediaStatusReport
from net.media_source.interface import MediaConnectionState
from qt_ui import settings

logger = logging.getLogger('restim.media.mpv')

# observe_property ids. mpv echoes these back on every change.
ID_TIME_POS = 1
ID_PAUSE = 2
ID_PATH = 3
ID_SPEED = 4
ID_IDLE = 5
ID_WORKING_DIR = 6

RECONNECT_INTERVAL_MS = 1000
# mpv pushes time-pos per frame; if it goes quiet for this long the file
# almost certainly ended or mpv wedged, so stop claiming to be playing.
STALL_TIMEOUT_S = 1.0


class Mpv(MediaSource):
    def __init__(self, parent):
        super(Mpv, self).__init__(parent)
        self._enabled = False

        self.socket = QLocalSocket(self)
        self.socket.connected.connect(self.on_connected)
        self.socket.disconnected.connect(self.on_disconnected)
        self.socket.readyRead.connect(self.on_ready_read)

        self.reconnect_timer = QtCore.QTimer(self)
        self.reconnect_timer.setInterval(RECONNECT_INTERVAL_MS)
        self.reconnect_timer.timeout.connect(self.try_connect)

        # mpv stops pushing time-pos when playback stops, so a timer — not the
        # absence of a message — is what turns "playing" off.
        self.stall_timer = QtCore.QTimer(self)
        self.stall_timer.setInterval(int(STALL_TIMEOUT_S * 1000))
        self.stall_timer.setSingleShot(True)
        self.stall_timer.timeout.connect(self.on_stalled)

        self._buffer = b''
        self._time_pos = None
        self._paused = None
        self._path = None
        self._speed = 1.0
        self._idle = None
        self._working_dir = None

    # ------------------------------------------------------------ lifecycle

    def enable(self):
        self._enabled = True
        self.try_connect()
        self.reconnect_timer.start()

    def disable(self):
        self._enabled = False
        self.reconnect_timer.stop()
        self.stall_timer.stop()
        self.socket.abort()
        self._reset()
        self.set_state(MediaStatusReport(time.time()))

    def is_enabled(self) -> bool:
        return self._enabled

    def media_path(self) -> str:
        return self.last_state.filePath

    # ----------------------------------------------------------- connection

    def socket_path(self) -> str:
        return os.path.expanduser(settings.media_sync_mpv_socket.get())

    def try_connect(self):
        if not self._enabled:
            return
        if self.socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            return
        path = self.socket_path()
        if not os.path.exists(path):
            # Nothing to connect to yet. Not an error — mpv simply is not up.
            return
        self.socket.connectToServer(path)

    def on_connected(self):
        logger.info('connected to mpv at %s', self.socket_path())
        self._buffer = b''
        self._reset()
        for prop, obs_id in (('time-pos', ID_TIME_POS),
                             ('pause', ID_PAUSE),
                             ('path', ID_PATH),
                             ('speed', ID_SPEED),
                             ('idle-active', ID_IDLE),
                             ('working-directory', ID_WORKING_DIR)):
            self._send({'command': ['observe_property', obs_id, prop]})

    def on_disconnected(self):
        logger.info('mpv disconnected')
        self.stall_timer.stop()
        self._reset()
        self.set_state(MediaStatusReport(time.time()))

    def _send(self, obj):
        try:
            self.socket.write(json.dumps(obj).encode() + b'\n')
        except Exception:
            logger.debug('mpv write failed', exc_info=True)

    def _reset(self):
        self._time_pos = None
        self._paused = None
        self._path = None
        self._speed = 1.0
        self._idle = None
        self._working_dir = None

    # -------------------------------------------------------------- reading

    def on_ready_read(self):
        self._buffer += bytes(self.socket.readAll())
        while b'\n' in self._buffer:
            line, self._buffer = self._buffer.split(b'\n', 1)
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                logger.debug('unparseable line from mpv: %r', line[:200])
                continue
            self.handle_message(message)

    def handle_message(self, message: dict) -> None:
        if message.get('event') != 'property-change':
            # Command replies and other events (seek, file-loaded, ...) carry
            # nothing we do not already learn from the observed properties.
            return

        name, data = message.get('name'), message.get('data')
        if name == 'time-pos':
            self._time_pos = data
        elif name == 'pause':
            self._paused = data
        elif name == 'path':
            self._path = data
        elif name == 'speed':
            self._speed = 1.0 if data is None else float(data)
        elif name == 'idle-active':
            self._idle = data
        elif name == 'working-directory':
            self._working_dir = data
        else:
            return

        self.emit_state()

    def on_stalled(self):
        """time-pos stopped arriving — treat as not playing, keep the cursor."""
        if self.state().is_playing():
            logger.debug('mpv went quiet; reporting paused')
            self.emit_state(force_paused=True)

    def resolved_path(self) -> str:
        """mpv reports `path` as it was given, which may be relative.

        Funscript auto-detection looks next to the media file, so a relative
        path would silently find nothing.
        """
        if not self._path:
            return ''
        if os.path.isabs(self._path):
            return self._path
        if self._working_dir:
            return os.path.normpath(os.path.join(self._working_dir, self._path))
        return self._path

    # --------------------------------------------------------------- report

    def connection_state(self, force_paused: bool = False) -> MediaConnectionState:
        if self.socket.state() != QLocalSocket.LocalSocketState.ConnectedState:
            return MediaConnectionState.NOT_CONNECTED
        if self._idle or not self._path:
            return MediaConnectionState.CONNECTED_BUT_NO_FILE_LOADED
        if force_paused or self._paused or self._time_pos is None:
            return MediaConnectionState.CONNECTED_AND_PAUSED
        return MediaConnectionState.CONNECTED_AND_PLAYING

    def emit_state(self, force_paused: bool = False) -> None:
        state = self.connection_state(force_paused)
        if state == MediaConnectionState.CONNECTED_AND_PLAYING:
            self.stall_timer.start()
        else:
            self.stall_timer.stop()

        self.set_state(MediaStatusReport(
            timestamp=time.time(),
            connectionState=state,
            filePath=self.resolved_path(),
            playbackRate=self._speed,
            claimed_media_position=(self._time_pos
                                    if self._time_pos is not None else -1),
        ))
