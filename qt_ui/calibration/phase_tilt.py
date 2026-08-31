"""
Phase 6 (TILT): per-electrode frequency-response calibration.

Each electrode is tested with two amplitude-modulation patterns:
  - Slow AM (~2 Hz): mimics slow funscript envelope content
  - Fast AM (~15 Hz): mimics fast transient content

If an electrode feels weaker on the fast test than the slow test, it
has an HF rolloff — the slider should be pushed positive (HF boost) to
compensate. If fast feels stronger than slow, push negative.

The slider sets tilt_db directly in [-12, +12] dB. 0 = no tilt (identity).
The page is fully skippable — clicking Next without adjusting any slider
keeps the default zero tilt (no frequency-response correction).

⚠ "Each electrode is tested" overstates what the hardware allows. Like
Phase 3, this page drives SINGLE_A..D, and the device may not put one lane
above the sum of the other three: a commanded (1,0,0,0) is delivered as
roughly (1, 0.37, 0.36, 0.50) — measured on a phantom 2026-08-30. The
named electrode leads, the other three sit at about a third.

This page is less damaged by that than Phase 3, because the judgement it
asks for is WITHIN one electrode (slow AM vs fast AM, same drive vector
both times) rather than between electrodes. The wash is identical in both
halves of the comparison, so an HF rolloff on the leading electrode still
shows up as a slow/fast difference. What it does mean is that a rolloff on
one of the three background electrodes leaks into every other electrode's
test, so tilt values are not fully independent of each other.
"""

from __future__ import annotations

import logging
import math
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWizardPage,
)

from stim_math.calibration.device_protocol import ElectrodePair

logger = logging.getLogger('restim.calibration.phase_tilt')

SLOW_HZ = 2.0          # AM frequency for the "slow" test
FAST_HZ = 15.0         # AM frequency for the "fast" test
TEST_DURATION_MS = 6000
TICK_MS = 50           # 20 Hz update → smooth AM at 15 Hz needs <33 ms

DEFAULT_DRIVE_LEVEL = 0.15   # fallback if session has no preferred_target yet

SLIDER_MIN = -120      # 0.1 dB steps → -12.0 dB
SLIDER_MAX = 120       # +12.0 dB
SLIDER_DEFAULT = 0     # 0 dB (identity)

_ELECTRODES = (
    ('E1', 'A', ElectrodePair.SINGLE_A),
    ('E2', 'B', ElectrodePair.SINGLE_B),
    ('E3', 'C', ElectrodePair.SINGLE_C),
    ('E4', 'D', ElectrodePair.SINGLE_D),
)


class TiltPage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle('Frequency-response calibration')
        self.setSubTitle(
            'Click "Slow" or "Fast" next to each electrode to feel its '
            'response to slow envelope vs. fast transient content. If fast '
            'feels weaker than slow, push the slider right (+). If fast '
            'feels stronger than slow, push left (−). Skip if all feel '
            'balanced, or if you prefer not to apply tilt compensation.'
        )

        self._test_running_for: str | None = None
        self._test_hz: float = SLOW_HZ
        self._test_start_ms: int = 0
        self._drive_level: float = DEFAULT_DRIVE_LEVEL
        self._active_pair: ElectrodePair = ElectrodePair.ALL
        self._sliders: dict[str, QSlider] = {}
        self._slow_buttons: dict[str, QPushButton] = {}
        self._fast_buttons: dict[str, QPushButton] = {}
        self._value_labels: dict[str, QLabel] = {}

        layout = QVBoxLayout(self)

        grid = QGridLayout()
        grid.setColumnStretch(4, 1)  # slider column expands
        layout.addLayout(grid)

        hint_style = 'color: #888; font-size: 11px;'

        for row, (name, display, pair) in enumerate(_ELECTRODES):
            grid.addWidget(QLabel(f'Electrode {display}:'), row, 0)

            slow_btn = QPushButton('Slow')
            slow_btn.setToolTip(f'Test electrode {display} with slow ({SLOW_HZ:.0f} Hz) content')
            slow_btn.clicked.connect(
                lambda _, n=name, p=pair: self._start_test(n, p, SLOW_HZ))
            grid.addWidget(slow_btn, row, 1)
            self._slow_buttons[name] = slow_btn

            fast_btn = QPushButton('Fast')
            fast_btn.setToolTip(f'Test electrode {display} with fast ({FAST_HZ:.0f} Hz) content')
            fast_btn.clicked.connect(
                lambda _, n=name, p=pair: self._start_test(n, p, FAST_HZ))
            grid.addWidget(fast_btn, row, 2)
            self._fast_buttons[name] = fast_btn

            lf_label = QLabel('LF−')
            lf_label.setStyleSheet(hint_style)
            lf_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(lf_label, row, 3)

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(SLIDER_MIN, SLIDER_MAX)
            slider.setValue(SLIDER_DEFAULT)
            slider.setTickPosition(QSlider.TickPosition.TicksBelow)
            slider.setTickInterval(30)  # tick every 3 dB
            slider.valueChanged.connect(
                lambda v, n=name: self._on_slider_changed(n, v))
            grid.addWidget(slider, row, 4)
            self._sliders[name] = slider

            hf_label = QLabel('+HF')
            hf_label.setStyleSheet(hint_style)
            hf_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(hf_label, row, 5)

            val_label = QLabel('0.0 dB')
            val_label.setMinimumWidth(55)
            val_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(val_label, row, 6)
            self._value_labels[name] = val_label

        layout.addStretch()

        reset_btn = QPushButton('Reset all to 0 dB')
        reset_btn.clicked.connect(self._reset_all)
        layout.addWidget(reset_btn)

        self._status_label = QLabel('')
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._test_timer = QTimer(self)
        self._test_timer.setInterval(TICK_MS)
        self._test_timer.timeout.connect(self._tick)

        self._stop_timer = QTimer(self)
        self._stop_timer.setSingleShot(True)
        self._stop_timer.timeout.connect(self._stop_test)

    # --- QWizardPage lifecycle ---

    def initializePage(self) -> None:
        session = self.wizard().session
        # Use the session's preferred_target so the test runs at a
        # comfortable level the user already calibrated in Phase 4/5.
        if session.landmark_comfortable is not None:
            self._drive_level = float(session.landmark_comfortable)
        else:
            self._drive_level = DEFAULT_DRIVE_LEVEL

        # Restore sliders from any previous visit or partial session
        tilt_db = getattr(session, 'tilt_db', [0.0, 0.0, 0.0, 0.0])
        for i, (name, _, _) in enumerate(_ELECTRODES):
            db = tilt_db[i] if i < len(tilt_db) else 0.0
            slider_val = int(round(db * 10.0))
            slider_val = max(SLIDER_MIN, min(SLIDER_MAX, slider_val))
            self._sliders[name].blockSignals(True)
            self._sliders[name].setValue(slider_val)
            self._sliders[name].blockSignals(False)
            self._value_labels[name].setText(f'{db:+.1f} dB' if db != 0 else '0.0 dB')

        self._status_label.setText(
            'Use Slow / Fast to test each electrode, then adjust its slider.'
        )

    def cleanupPage(self) -> None:
        self._stop_test()

    def validatePage(self) -> bool:
        tilt_db = [
            self._sliders[name].value() / 10.0
            for name, _, _ in _ELECTRODES
        ]
        try:
            self.wizard().session.record_tilt(tilt_db)
        except Exception as e:
            self._status_label.setText(f'Cannot save tilt: {e}')
            return False
        logger.info(f'phase tilt: tilt_db={tilt_db}')
        return True

    def isComplete(self) -> bool:
        return True  # always skippable

    # --- Per-electrode test ---

    def _start_test(self, name: str, pair: ElectrodePair, hz: float) -> None:
        if self._test_running_for is not None:
            return
        adapter = self.wizard().adapter
        if not adapter.is_connected():
            self._status_label.setText('Device not connected.')
            return

        self._test_running_for = name
        self._test_hz = hz
        self._test_start_ms = self._now_ms()
        self._active_pair = pair
        self._set_all_buttons_enabled(False)

        freq_label = 'slow' if hz == SLOW_HZ else 'fast'
        display = {'1': 'A', '2': 'B', '3': 'C', '4': 'D'}.get(name[1], name[1])
        # The comparison this page can actually support is slow-vs-fast on the
        # SAME drive, not this electrode against the others — the other three
        # are present at ~1/3 in every test (see the module docstring).
        self._status_label.setText(
            f'Electrode {display} leading (others ~⅓) — {freq_label} '
            f'({hz:.0f} Hz AM). Adjust the slider if this electrode feels '
            f'weaker on fast than on slow.'
        )

        self._test_timer.start()
        self._stop_timer.start(TEST_DURATION_MS)

    def _tick(self) -> None:
        elapsed_s = (self._now_ms() - self._test_start_ms) / 1000.0
        am = 0.5 + 0.5 * math.sin(2.0 * math.pi * self._test_hz * elapsed_s)
        level = self._drive_level * am
        try:
            self.wizard().adapter.set_calibration_waveform(
                self._active_pair, level, TICK_MS * 2,
            )
        except Exception:
            logger.exception('AM drive tick raised')

    def _stop_test(self) -> None:
        self._stop_timer.stop()
        self._test_timer.stop()
        try:
            self.wizard().adapter.set_calibration_waveform(
                ElectrodePair.ALL, 0.0, 100,
            )
        except Exception:
            logger.exception('silence at test end raised')
        self._test_running_for = None
        self._set_all_buttons_enabled(True)
        self._status_label.setText('Test complete. Adjust slider then test the next electrode.')

    def _set_all_buttons_enabled(self, enabled: bool) -> None:
        for btn in self._slow_buttons.values():
            btn.setEnabled(enabled)
        for btn in self._fast_buttons.values():
            btn.setEnabled(enabled)

    def _on_slider_changed(self, name: str, value: int) -> None:
        db = value / 10.0
        self._value_labels[name].setText(f'{db:+.1f} dB' if db != 0 else '0.0 dB')

    def _reset_all(self) -> None:
        for name, _, _ in _ELECTRODES:
            self._sliders[name].setValue(SLIDER_DEFAULT)

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)
