"""mpv media source: the position it reports must reach restim unscaled.

The failure this guards against is the one the HTTP sources are prone to and
that cost a bench session: a source that reconstructs position from a fraction
and a guessed length hands restim a number that advances at the wrong rate.
mpv's `time-pos` is absolute seconds, and the point of this source is that it
stays that way — no duration multiplied in anywhere.

Runs under pytest, or as a plain script with the venv that has PySide6:
    python tests/test_mpv_media_source.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication            # noqa: E402
from PySide6.QtNetwork import QLocalSocket             # noqa: E402

from net.media_source.interface import MediaConnectionState  # noqa: E402
from net.media_source.mpv import Mpv                   # noqa: E402

_app = QCoreApplication.instance() or QCoreApplication([])


class _FakeSocket:
    """Stands in for QLocalSocket so state can be driven from a test."""

    def __init__(self, state=QLocalSocket.LocalSocketState.ConnectedState):
        self._state = state
        self.written = []

    def state(self):
        return self._state

    def write(self, data):
        self.written.append(bytes(data))

    def abort(self):
        self._state = QLocalSocket.LocalSocketState.UnconnectedState


def _src(connected=True):
    m = Mpv(None)
    m.socket = _FakeSocket(
        QLocalSocket.LocalSocketState.ConnectedState if connected
        else QLocalSocket.LocalSocketState.UnconnectedState)
    return m


def _prop(m, name, data):
    m.handle_message({'event': 'property-change', 'name': name, 'data': data})


def _playing(m, path='/clips/a.mp4', pos=0.0):
    _prop(m, 'working-directory', '/clips')
    _prop(m, 'path', path)
    _prop(m, 'idle-active', False)
    _prop(m, 'pause', False)
    _prop(m, 'time-pos', pos)
    return m


def _close(actual, expected, tol=1e-9):
    assert abs(actual - expected) <= tol, f'{actual!r} != {expected!r}'


# --------------------------------------------------------------------------


def test_time_pos_reaches_restim_unscaled():
    """The whole reason this source exists: no duration is multiplied in."""
    m = _playing(_src(), pos=3.5)
    _close(m.last_state.cursor, 3.5)
    # and a later position is still itself, not a fraction of anything
    _prop(m, 'time-pos', 7.25)
    assert m.is_playing()
    _close(m.map_timestamp(m.last_state.media_play_timestamp), 7.25)


def test_a_long_position_is_not_wrapped_or_rescaled():
    """A 2-hour film: nothing here should fold position into 0..1."""
    m = _playing(_src(), pos=5400.0)
    _close(m.last_state.cursor, 5400.0)


def test_state_machine_idle_loaded_playing_paused():
    m = _src()
    _prop(m, 'idle-active', True)
    assert m.state() == MediaConnectionState.CONNECTED_BUT_NO_FILE_LOADED

    _prop(m, 'working-directory', '/clips')
    _prop(m, 'path', '/clips/a.mp4')
    _prop(m, 'idle-active', False)
    _prop(m, 'pause', False)
    _prop(m, 'time-pos', 1.0)
    assert m.state() == MediaConnectionState.CONNECTED_AND_PLAYING

    _prop(m, 'pause', True)
    assert m.state() == MediaConnectionState.CONNECTED_AND_PAUSED


def test_disconnected_socket_is_never_reported_as_playing():
    m = _playing(_src(connected=False))
    assert m.state() == MediaConnectionState.NOT_CONNECTED


def test_relative_path_is_resolved_against_working_directory():
    """Funscripts are looked for NEXT TO the media file — a relative path
    would silently find none."""
    m = _playing(_src(), path='a.mp4')
    assert m.media_path() == '/clips/a.mp4'


def test_absolute_path_is_left_alone():
    m = _playing(_src(), path='/elsewhere/b.mp4')
    assert m.media_path() == '/elsewhere/b.mp4'


def test_speed_is_reported_so_restim_can_refuse_it():
    """restim only supports rate 1; it needs to be told when that is false."""
    m = _playing(_src())
    _prop(m, 'speed', 2.0)
    assert m.state() == MediaConnectionState.CONNECTED_AND_PAUSED


def test_garbage_and_unrelated_events_are_ignored():
    m = _playing(_src(), pos=2.0)
    before = m.last_state.cursor
    m.on_ready_read = lambda: None                       # not under test here
    m.handle_message({'event': 'seek'})
    m.handle_message({'event': 'property-change', 'name': 'volume', 'data': 50})
    m.handle_message({'error': 'success', 'request_id': 1})
    _close(m.last_state.cursor, before)


def test_observe_requests_are_sent_on_connect():
    m = _src()
    m.on_connected()
    sent = b''.join(m.socket.written).decode()
    for prop in ('time-pos', 'pause', 'path', 'speed',
                 'idle-active', 'working-directory'):
        assert f'"{prop}"' in sent, f'never observed {prop}'


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
