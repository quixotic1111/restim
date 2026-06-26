"""
Phase 3: per-electrode balance.

Phase 1 already computed gain_trims from impedance ratios. Most users
don't need to touch them — auto values are good enough for first session.
But because measurement and perception don't always agree (e.g. lubrication
or contact area varies in ways impedance can't see), this page lets the
user drive one electrode at a time, listen/feel for differences, and
nudge any electrode that feels off.

The page is fully skippable — clicking Next without adjusting any slider
just keeps the auto-computed trims.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
    QWizardPage,
)

from stim_math.calibration.device_protocol import ElectrodePair
from stim_math.calibration.impedance import compute_gain_trims

logger = logging.getLogger('restim.calibration.phase3')

TEST_DRIVE_LEVEL = 0.18
TEST_DURATION_MS = 3000

# Slider scale: 100 = 1.0× (no change). Asymmetric range — reduction is
# more permissive than boost since attenuating a hot electrode is safer
# than amplifying one. Users with severe asymmetry (anatomy, contact
# variance) routinely need 0.10× or lower on the strongest electrode.
SLIDER_MIN = 10     # 0.10×  — maximum attenuation (near-silent)
SLIDER_MAX = 200    # 2.00×  — maximum boost
SLIDER_DEFAULT = 100

_ELECTRODES = (
    ('E1', 'A', ElectrodePair.SINGLE_A),
    ('E2', 'B', ElectrodePair.SINGLE_B),
    ('E3', 'C', ElectrodePair.SINGLE_C),
    ('E4', 'D', ElectrodePair.SINGLE_D),
)


class BalancePage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle('Per-electrode balance')
        self.setSubTitle(
            'Click Test next to each electrode to feel it on its own. If any '
            'feels noticeably stronger or weaker than the others, nudge the '
            'slider next to it. Skip if everything feels balanced. '
            'Gain is capped at 1.0× — only attenuation is applied, '
            'preventing current overload on sensitive electrodes.'
        )

        self._baseline_trims: dict[str, float] = {}
        self._test_running_for: str | None = None
        self._sliders: dict[str, QSlider] = {}
        self._test_buttons: dict[str, QPushButton] = {}
        self._current_labels: dict[str, QLabel] = {}

        layout = QVBoxLayout(self)

        grid = QGridLayout()
        grid.setColumnStretch(3, 1)  # slider column expands
        layout.addLayout(grid)

        # Subtle hint that center is the auto-balanced value
        hint_style = 'color: #888; font-size: 11px;'

        for row, (name, display, pair) in enumerate(_ELECTRODES):
            grid.addWidget(QLabel(f'Electrode {display}:'), row, 0)

            btn = QPushButton('Test')
            btn.clicked.connect(lambda _, n=name, p=pair: self._start_test(n, p))
            grid.addWidget(btn, row, 1)
            self._test_buttons[name] = btn

            weaker_label = QLabel('weaker')
            weaker_label.setStyleSheet(hint_style)
            weaker_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(weaker_label, row, 2)

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(SLIDER_MIN, SLIDER_MAX)
            slider.setValue(SLIDER_DEFAULT)
            slider.setTickPosition(QSlider.TickPosition.TicksBelow)
            slider.setTickInterval(25)  # show a tick at each quarter
            grid.addWidget(slider, row, 3)
            self._sliders[name] = slider

            stronger_label = QLabel('stronger')
            stronger_label.setStyleSheet(hint_style)
            stronger_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(stronger_label, row, 4)

            # Measured current readout (filled in initializePage if Phase 1
            # captured it; otherwise stays blank).
            current_label = QLabel('')
            current_label.setStyleSheet(hint_style)
            current_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(current_label, row, 5)
            self._current_labels[name] = current_label

        # Opt-in: set the sliders from measured current (the user can still
        # nudge afterward). Hidden when Phase 1 captured no current telemetry.
        self._autobalance_button = QPushButton('Auto-balance from measured current')
        self._autobalance_button.clicked.connect(self._auto_balance_from_current)
        self._autobalance_button.setVisible(False)
        layout.addWidget(self._autobalance_button)

        layout.addStretch()

        self._status_label = QLabel('')
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._test_timer = QTimer(self)
        self._test_timer.setSingleShot(True)
        self._test_timer.timeout.connect(self._stop_test)

    # --- QWizardPage lifecycle ---

    def initializePage(self) -> None:
        session = self.wizard().session
        # Re-compute baseline trims from impedances. This is idempotent —
        # entering Phase 3 twice (e.g. Back from Phase 4 then Next) starts
        # from the same baseline regardless of prior adjustments.
        baseline, _ = compute_gain_trims(session.impedances)
        self._baseline_trims = baseline

        # Restore slider positions to reflect current session.gain_trims
        # relative to the baseline (so a returning user sees their prior nudges).
        for name, baseline_val in baseline.items():
            current = session.gain_trims.get(name, baseline_val)
            factor = (current / baseline_val) if baseline_val > 0 else 1.0
            factor = max(SLIDER_MIN / 100.0, min(SLIDER_MAX / 100.0, factor))
            slider = self._sliders.get(name)
            if slider is not None:
                slider.blockSignals(True)
                slider.setValue(int(round(factor * 100)))
                slider.blockSignals(False)

        # Show measured current per electrode (if Phase 1 captured it) and
        # enable the opt-in current auto-balance.
        measured = getattr(session, 'measured_currents', {})
        for name, label in self._current_labels.items():
            mA = measured.get(name)
            label.setText(f'{mA * 1000:.1f} mA' if mA is not None else '')
        self._autobalance_button.setVisible(bool(measured))

        self._status_label.setText(
            'Auto-balanced from impedance measurements. '
            'Adjust any slider if an electrode still feels off.'
            + (' Or click “Auto-balance from measured current” to set them from '
               'what the device actually delivered.' if measured else '')
        )

    def _auto_balance_from_current(self) -> None:
        """Set the sliders from the MEASURED-current trims (opt-in).

        Each slider is a factor relative to the impedance baseline
        (trim = baseline × slider/100), so we back out the slider value that
        lands on the current-derived trim. The user can still nudge afterward;
        nothing is committed until Next (validatePage)."""
        session = self.wizard().session
        trims, warnings = session.trims_from_currents()
        if not trims:
            self._status_label.setText(
                'No measured current available — keeping the impedance balance.'
            )
            return
        for name, target in trims.items():
            baseline_val = self._baseline_trims.get(name, 1.0)
            factor = (target / baseline_val) if baseline_val > 0 else 1.0
            factor = max(SLIDER_MIN / 100.0, min(SLIDER_MAX / 100.0, factor))
            slider = self._sliders.get(name)
            if slider is not None:
                slider.blockSignals(True)
                slider.setValue(int(round(factor * 100)))
                slider.blockSignals(False)
        msg = ('Balanced from measured current — even delivered current across '
               'electrodes. Test any electrode to feel it, then nudge if needed.')
        if warnings:
            msg += '  (' + warnings[0] + ')'
        self._status_label.setText(msg)

    def cleanupPage(self) -> None:
        self._test_timer.stop()
        adapter = self.wizard().adapter
        if self._test_running_for is not None:
            try:
                adapter.set_calibration_waveform(
                    ElectrodePair.ALL, 0.0, 100,
                )
            except Exception:
                logger.exception('silence during cleanup raised')
            self._test_running_for = None
        # Leaving Phase 3 — reset calibration trims so subsequent measurement
        # phases (or a return visit) don't see stale trim state.
        try:
            adapter.reset_calibration_trims()
        except Exception:
            logger.exception('reset_calibration_trims during cleanup raised')

    def validatePage(self) -> bool:
        """Apply slider factors to baseline trims and write to session.

        Trims are capped at 1.0 (attenuation-only). Boosting an electrode
        above its natural drive level risks exceeding the device current limit,
        particularly on high-impedance electrodes where the impedance-ratio
        baseline may compute a multiplier >> 1.0.
        """
        session = self.wizard().session
        for name, baseline_val in self._baseline_trims.items():
            slider_val = self._sliders[name].value() / 100.0
            session.gain_trims[name] = min(1.0, baseline_val * slider_val)
        logger.info(f'phase 3 finalized trims: {session.gain_trims}')
        return True

    def isComplete(self) -> bool:
        # Always allowed to continue (no required action)
        return True

    # --- Per-electrode test ---

    def _start_test(self, name: str, pair: ElectrodePair) -> None:
        if self._test_running_for is not None:
            return  # another test is in progress
        adapter = self.wizard().adapter
        if not adapter.is_connected():
            self._status_label.setText('Device not connected.')
            return

        self._test_running_for = name
        for btn in self._test_buttons.values():
            btn.setEnabled(False)

        # Apply current slider positions as live calibration trims, so the
        # user can FEEL the effect of their adjustments during this test.
        # Cap at 1.0 — same attenuation-only constraint as validatePage().
        try:
            baseline = self._baseline_trims
            adapter.set_calibration_trims(
                min(1.0, baseline.get('E1', 1.0) * self._sliders['E1'].value() / 100.0),
                min(1.0, baseline.get('E2', 1.0) * self._sliders['E2'].value() / 100.0),
                min(1.0, baseline.get('E3', 1.0) * self._sliders['E3'].value() / 100.0),
                min(1.0, baseline.get('E4', 1.0) * self._sliders['E4'].value() / 100.0),
            )
        except Exception:
            logger.exception('set_calibration_trims at test start raised')

        adapter.set_calibration_waveform(pair, TEST_DRIVE_LEVEL, TEST_DURATION_MS)
        electrode_letter = name[1]  # 'E1' → '1'... use display letter
        display = {'1': 'A', '2': 'B', '3': 'C', '4': 'D'}.get(electrode_letter, electrode_letter)
        self._status_label.setText(
            f'Driving electrode {display} alone — adjust slider if it feels '
            f'noticeably different from the others.'
        )
        self._test_timer.start(TEST_DURATION_MS)

    def _stop_test(self) -> None:
        adapter = self.wizard().adapter
        try:
            adapter.set_calibration_waveform(ElectrodePair.ALL, 0.0, 100)
        except Exception:
            logger.exception('silence at test end raised')
        self._test_running_for = None
        for btn in self._test_buttons.values():
            btn.setEnabled(True)
        self._status_label.setText('Test complete. Test another or click Next.')

