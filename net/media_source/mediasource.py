import copy
from dataclasses import dataclass
import logging

from PySide6.QtCore import QObject

from net.media_source.interface import MediaSourceInterface, MediaConnectionState

logger = logging.getLogger('restim.media')


@dataclass
class MediaState:
    connectionState: MediaConnectionState
    filePath: str = ''
    cursor: float = -1   # when paused, the media cursor. When playing, the time at which media was started.
    media_play_timestamp: float = -1
    # Media seconds per wall-clock second. The player's clock is slaved to its
    # audio device, whose real rate differs from the (NTP-disciplined) system
    # clock by ~0.1-0.5%. Tracking it is what keeps a long session in sync.
    rate: float = 1.0
    # Wall time playback started, so the start-up transient can be skipped.
    play_wall_timestamp: float = -1


@dataclass
class MediaStatusReport:
    timestamp: float    # timestamp the report was generated
    errorString: str = ''
    connectionState: MediaConnectionState = MediaConnectionState.NOT_CONNECTED
    filePath: str = ''
    playbackRate: float = 1
    claimed_media_position: float = -1   # seconds since start of the file


# --- media clock tracking -------------------------------------------------
# A player's clock is slaved to its audio device; the system clock is not.
# Their rates differ by ~0.1-0.5%, so an anchor taken once at play and then
# extrapolated on wall time slips by a couple of ms per second. Rather than
# waiting for that to pass a 2 s threshold and snapping, track it: a slew
# limited phase correction plus a rate estimate, both fed by the drift the
# poll already gives us.
_SEEK_THRESHOLD_S = 2.0        # beyond this it is a seek, not drift: hard re-anchor
_SETTLE_S = 1.5                # after play, stay pinned to the player: its reported
                               # position and its audible output are furthest apart
                               # while the output primes, and restim's own volume
                               # ramp means nothing is audible yet anyway
_PHASE_GAIN = 0.5              # 1/s   — closes a phase error with tau ~2 s
_RATE_GAIN = 0.05              # 1/s^2 — integrator, removes the steady-state slip
_MAX_PHASE_FRACTION = 0.5      # of the error, per poll, whatever the interval
_MAX_SLEW_S_PER_S = 0.02       # never move the clock faster than this
_RATE_MIN, _RATE_MAX = 0.95, 1.05
_MIN_POLL_INTERVAL_S, _MAX_POLL_INTERVAL_S = 0.001, 5.0


def _anchor(state: 'MediaState', report: 'MediaStatusReport') -> None:
    """Pin the media clock to this report and restart rate tracking."""
    state.cursor = report.claimed_media_position
    state.media_play_timestamp = report.timestamp
    state.play_wall_timestamp = report.timestamp
    state.rate = 1.0


class A(type(QObject), type(MediaSourceInterface)):
    pass


class MediaSource(QObject, MediaSourceInterface, metaclass=A):
    def __init__(self, parent):
        super(MediaSource, self).__init__(parent)

        self.last_state = MediaState(MediaConnectionState.NOT_CONNECTED)
        self.media_sync_offset = 0

    def state(self) -> MediaConnectionState:
        return self.last_state.connectionState

    def pre_update(self, last_state, state):
        return state

    def set_state(self, report: MediaStatusReport):
        # report = self.pre_update(self.last_state, report)
        new_state = copy.copy(self.last_state)

        # only support playback rate == 1
        if report.playbackRate != 1:
            if report.connectionState.is_playing():
                report.connectionState = MediaConnectionState.CONNECTED_AND_PAUSED

        # initial connect
        if self.last_state.connectionState == MediaConnectionState.NOT_CONNECTED:
            if report.connectionState.is_connected():
                logger.info('connected')
                new_state.connectionState = report.connectionState
                new_state.filePath = report.filePath
                _anchor(new_state, report)

                if report.connectionState.is_playing():
                    logger.info('play-on-connect')
                    _anchor(new_state, report)

        # any disconnect
        elif not report.connectionState.is_connected():
            logger.info('disconnected')
            new_state.connectionState = MediaConnectionState.NOT_CONNECTED
            new_state.filePath = ''

        elif self.last_state.connectionState == MediaConnectionState.CONNECTED_BUT_NO_FILE_LOADED:
            if report.connectionState.is_file_loaded():
                logger.info('file loaded')
                new_state.filePath = report.filePath
                new_state.connectionState = report.connectionState
                _anchor(new_state, report)

                if report.connectionState.is_playing():
                    logger.info('play-on-load')

        elif self.last_state.connectionState == MediaConnectionState.CONNECTED_AND_PAUSED:
            if self.last_state.connectionState.is_file_loaded():
                if report.connectionState.is_file_loaded():
                    if self.last_state.filePath != report.filePath:
                        logger.info('loaded file changed')
                        new_state.filePath = report.filePath

            if report.connectionState.is_playing():
                logger.info('play')
                new_state.connectionState = report.connectionState
                _anchor(new_state, report)
            elif not report.connectionState.is_file_loaded():
                logger.info('file unload')
                new_state.connectionState = report.connectionState
                new_state.filePath = ''
            else:
                # seek
                new_state.cursor = report.claimed_media_position

        elif self.last_state.connectionState == MediaConnectionState.CONNECTED_AND_PLAYING:
            if self.last_state.connectionState.is_file_loaded():
                if report.connectionState.is_file_loaded():
                    if self.last_state.filePath != report.filePath:
                        logger.info('loaded file changed')
                        new_state.filePath = report.filePath

            # play to unloaded
            if report.connectionState == MediaConnectionState.CONNECTED_BUT_NO_FILE_LOADED:
                logger.info('file unload')
                new_state.connectionState = report.connectionState
                new_state.filePath = ''
            # play to pause
            elif report.connectionState == MediaConnectionState.CONNECTED_AND_PAUSED:
                new_state.connectionState = MediaConnectionState.CONNECTED_AND_PAUSED
                new_state.cursor = report.claimed_media_position
                logger.info('pause')
            # still playing
            else:
                # Where our extrapolated clock says we are, versus where the
                # player says it is. Positive error = we are running behind.
                dt = report.timestamp - self.last_state.media_play_timestamp
                predicted = self.last_state.cursor + dt * self.last_state.rate
                error = report.claimed_media_position - predicted

                if abs(error) > _SEEK_THRESHOLD_S:
                    # Not drift — a seek, or the player lost the plot. Snap.
                    logger.info(f'drift too much ({error}), re-sync')
                    new_state.connectionState = MediaConnectionState.CONNECTED_AND_PLAYING
                    _anchor(new_state, report)
                elif (report.timestamp - self.last_state.play_wall_timestamp) < _SETTLE_S:
                    # Start-up transient: follow the player exactly instead of
                    # freezing whatever error it is reporting right now.
                    new_state.cursor = report.claimed_media_position
                    new_state.media_play_timestamp = report.timestamp
                else:
                    # PI loop. Re-anchoring every poll keeps the extrapolation
                    # window one interval long, so `rate` is the only thing
                    # carrying state forward.
                    step = min(max(dt, _MIN_POLL_INTERVAL_S), _MAX_POLL_INTERVAL_S)
                    limit = _MAX_SLEW_S_PER_S * step
                    # Cap the per-poll fraction so a slow poller (kodi polls
                    # every 2 s) cannot reach deadbeat gain and ring.
                    gain = min(_PHASE_GAIN * step, _MAX_PHASE_FRACTION)
                    correction = min(max(gain * error, -limit), limit)

                    new_state.rate = min(max(self.last_state.rate + _RATE_GAIN * error * step,
                                             _RATE_MIN), _RATE_MAX)
                    new_state.cursor = predicted + correction
                    new_state.media_play_timestamp = report.timestamp

        prev_state = self.last_state
        self.last_state = new_state
        if (prev_state.connectionState != new_state.connectionState) or \
                (prev_state.filePath != new_state.filePath):
            self.connectionStatusChanged.emit()

    def map_timestamp(self, timestamp):
        if self.is_playing():
            adj_timestamp = (self.last_state.cursor
                             + (timestamp - self.last_state.media_play_timestamp)
                             * self.last_state.rate
                             - self.media_sync_offset)
            return adj_timestamp
        else:
            return timestamp - timestamp + self.last_state.cursor - self.media_sync_offset

    def media_path(self) -> str:
        return self.last_state.filePath

    def set_media_sync_offset(self, offset_in_seconds):
        self.media_sync_offset = offset_in_seconds


