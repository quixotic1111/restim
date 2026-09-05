"""Media clock tracking: a player's clock is not the system clock.

These pin the two failure modes the old threshold-only drift handling had.
Both were measured end-to-end (flash-to-electrode, PicoScope on a 4x1k phantom):
the same media-sync offset landed anywhere across ~170 ms between sessions, and
three of four sessions showed a within-session slope of +1.7...+2.2 ms/s.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from net.media_source.interface import MediaConnectionState          # noqa: E402
from net.media_source.mediasource import (  # noqa: E402
    MediaSource, MediaStatusReport, _MAX_SLEW_S_PER_S, _SETTLE_S)


PLAYING = MediaConnectionState.CONNECTED_AND_PLAYING
PAUSED = MediaConnectionState.CONNECTED_AND_PAUSED

POLL_S = 0.1
# The player's clock runs 0.2% slow against the system clock — a normal
# crystal difference between an audio DAC and an NTP-disciplined host.
PLAYER_RATE = 0.998
T0 = 10_000.0


def _report(wall_t, media_pos, state=PLAYING, path='/tmp/clip.mp4'):
    return MediaStatusReport(timestamp=wall_t, connectionState=state,
                             filePath=path, playbackRate=1,
                             claimed_media_position=media_pos)


def _play(src, wall_t, media_pos=0.0):
    """Drive the state machine from disconnected to playing."""
    src.set_state(_report(wall_t, media_pos, PAUSED))
    src.set_state(_report(wall_t, media_pos, PLAYING))


def _run(src, seconds, start_wall, anchor_error=0.0, anchor_error_until=0.0):
    """Feed polls for `seconds`; returns the final (wall_t, true_media_pos)."""
    n = int(seconds / POLL_S)
    wall_t = start_wall
    media_pos = 0.0
    for i in range(1, n + 1):
        wall_t = start_wall + i * POLL_S
        media_pos = (wall_t - start_wall) * PLAYER_RATE
        claimed = media_pos
        if (wall_t - start_wall) < anchor_error_until:
            claimed += anchor_error
        src.set_state(_report(wall_t, claimed))
    return wall_t, media_pos


def _src():
    """restim has no test infrastructure, so this file runs either way:
    `pytest tests/test_media_sync.py`, or `python tests/test_media_sync.py`
    under the venv that has PySide6."""
    s = MediaSource(None)
    s.set_state(_report(T0 - 1.0, 0.0, PAUSED))
    return s


def _close(actual, expected, abs_tol):
    assert abs(actual - expected) <= abs_tol, \
        f'{actual:.6f} != {expected:.6f} (tol {abs_tol}, off by ' \
        f'{(actual - expected) * 1000:.1f} ms)'


def test_rate_mismatch_does_not_accumulate():
    src = _src()
    """The 2 ms/s slope: gone, not merely under a 2 s threshold."""
    _play(src, T0)
    wall_t, media_pos = _run(src, 60.0, T0)

    _close(src.map_timestamp(wall_t), media_pos, 0.005)

    # What the old code would have produced: rate pinned to 1.0 for 60 s.
    naive = wall_t - T0
    assert abs(naive - media_pos) > 0.1     # ~120 ms of slip it used to keep


def test_startup_anchor_error_is_not_frozen():
    src = _src()
    """The +-90 ms session-to-session spread: anchoring during the transient."""
    _play(src, T0)
    # Player reports 90 ms ahead of its audible output while the output primes.
    wall_t, media_pos = _run(src, 30.0, T0,
                             anchor_error=0.09, anchor_error_until=0.5)

    _close(src.map_timestamp(wall_t), media_pos, 0.005)


def test_offset_is_still_applied_once():
    src = _src()
    _play(src, T0)
    wall_t, media_pos = _run(src, 20.0, T0)

    src.set_media_sync_offset(0.25)
    _close(src.map_timestamp(wall_t), media_pos - 0.25, 0.005)


def test_seek_still_snaps():
    src = _src()
    _play(src, T0)
    wall_t, _ = _run(src, 20.0, T0)

    wall_t += POLL_S
    src.set_state(_report(wall_t, 500.0))
    _close(src.map_timestamp(wall_t), 500.0, 0.001)


def test_clock_is_smooth_once_playing():
    """Past the settle window a correction the body could feel is a bug.

    Inside it, following the player exactly is the point: that is where the
    90 ms is taken out, and restim's own volume ramp means nothing is audible
    for ~1.5 s after Play.
    """
    _play(src := _src(), T0)
    prev = None
    for i in range(1, 301):
        wall_t = T0 + i * POLL_S
        media_pos = (wall_t - T0) * PLAYER_RATE
        claimed = media_pos + (0.09 if (wall_t - T0) < 0.5 else 0.0)
        src.set_state(_report(wall_t, claimed))
        now = src.map_timestamp(wall_t)
        if (wall_t - T0) <= _SETTLE_S:
            prev = now
            continue
        step = now - prev
        assert step > 0, 'media clock ran backwards'
        # one poll of real time, plus at most one poll of allowed slew
        assert step < POLL_S + _MAX_SLEW_S_PER_S * POLL_S * 1.01, \
            f'clock jumped {step * 1000:.3f} ms in one poll'
        prev = now


def test_the_whole_startup_correction_lands_inside_the_soft_start():
    """restim ramps volume over ~1.5 s from stopped; the re-anchor must fit."""
    _play(src := _src(), T0)
    settled = None
    for i in range(1, 101):
        wall_t = T0 + i * POLL_S
        media_pos = (wall_t - T0) * PLAYER_RATE
        claimed = media_pos + (0.09 if (wall_t - T0) < 0.5 else 0.0)
        src.set_state(_report(wall_t, claimed))
        if settled is None and (wall_t - T0) > _SETTLE_S:
            settled = abs(src.map_timestamp(wall_t) - media_pos)
    assert settled is not None
    assert settled < 0.005, f'{settled * 1000:.1f} ms still uncorrected at settle'


def test_pause_holds_the_cursor():
    src = _src()
    _play(src, T0)
    wall_t, media_pos = _run(src, 10.0, T0)

    wall_t += POLL_S
    src.set_state(_report(wall_t, media_pos, PAUSED))
    _close(src.map_timestamp(wall_t + 5.0), media_pos, 0.001)


def test_a_slow_poller_still_converges():
    """kodi polls every 2 s, heresphere every 1 s — the loop is dt-aware."""
    slow_poll = 2.0
    _play(src := _src(), T0)
    wall_t = T0
    for i in range(1, 61):
        wall_t = T0 + i * slow_poll
        media_pos = (wall_t - T0) * PLAYER_RATE
        src.set_state(_report(wall_t, media_pos))
    media_pos = (wall_t - T0) * PLAYER_RATE
    _close(src.map_timestamp(wall_t), media_pos, 0.02)


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'PASS {name}')
            except AssertionError as exc:
                failures += 1
                print(f'FAIL {name}: {exc}')
    print(f'\n{failures} failure(s)')
    sys.exit(1 if failures else 0)
