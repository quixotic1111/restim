import time

from PySide6.QtCore import QObject

from net.media_source.interface import MediaSourceInterface, MediaConnectionState


class _InternalMeta(type(QObject), type(MediaSourceInterface)):
    pass


class Internal(QObject, MediaSourceInterface, metaclass=_InternalMeta):
    # Internal has no real media file. Report PLAYING only while TCode (or
    # other external) traffic has been writing axes recently — otherwise the
    # mute guard in audio_gen/continuous.py keeps the carrier silent. Without
    # this, selecting Internal + pressing Start with nothing driving produced
    # a steady carrier tone on the output.
    #
    # The window must be long enough to survive normal gaps in a LIVE T-code
    # drive (a paused transport, a re-render, a UI hitch). At 2.0 s any such
    # gap flipped the source to "not playing", which muted the carrier and
    # tore down the FOC-stim signal/device — the connection flapped roughly
    # once a second. Since _last_activity_time starts at 0.0, a cold Internal
    # start with nothing ever driving is still `now - 0 > timeout` → muted, so
    # the steady-tone fix is preserved; we just stay live for the whole session
    # once T-code actually starts arriving.
    ACTIVITY_TIMEOUT_S = 3600.0

    def __init__(self):
        super(Internal, self).__init__()
        self._enabled = False
        self._last_activity_time = 0.0

    def notify_activity(self, *_):
        """Mark recent TCode / API activity. Accepts and ignores any args so
        it can be hooked directly to Qt signals that carry payloads."""
        self._last_activity_time = time.time()

    def is_internal(self) -> bool:
        return True

    def enable(self):
        self._enabled = True
        self.connectionStatusChanged.emit()

    def disable(self):
        self._enabled = False

    def state(self) -> MediaConnectionState:
        # NOTE (2026-06-28): the activity-gated variant below muted the
        # FOC-stim *pulse* output (audio_gen/pulse_based.py zeroes the pulse
        # envelope when media.is_playing() is False) during a live UDP T-code
        # drive — notify_activity wasn't registering as "playing" for the
        # streamed path, so a real bench/on-body stream came through silent
        # (axes/spin-boxes updated, but position + electrodes frozen). Revert
        # to restim's original always-playing behavior so Internal + a live
        # stream actually generates. Re-introduces the idle steady-tone (only
        # when Internal is selected with NOTHING driving); that's the lesser
        # problem and irrelevant while streaming. Proper fix later: drive
        # notify_activity from where commands are actually applied
        # (TCodeCommandRouter.route_command), not the transport signal wiring.
        return MediaConnectionState.CONNECTED_AND_PLAYING
        # --- activity-gated (disabled, see above) ---
        # if time.time() - self._last_activity_time < self.ACTIVITY_TIMEOUT_S:
        #     return MediaConnectionState.CONNECTED_AND_PLAYING
        # return MediaConnectionState.CONNECTED_BUT_NO_FILE_LOADED

    def is_enabled(self) -> bool:
        return True

    def map_timestamp(self, timestamp):
        # this should never be called because internal doesn't support any funscripts
        return timestamp

    def media_path(self) -> str:
        return ''
