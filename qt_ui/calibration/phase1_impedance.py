"""
Phase 1: impedance measurement.

Leads with a consent screen ("you will feel a brief gentle signal"),
then drives all electrodes balanced at 18% (above the 12% noise floor
from Test 3, below normal-use levels) for ~5 seconds. Per-electrode
complex impedance is averaged with a trimmed-mean (drop min + max) and
written into the session as the baseline.

Phase 4 (perception sweep) will later use the user's normal algorithm;
this phase uses the calibration algorithm to guarantee balanced drive.

★ The balanced drive is load-bearing, not just tidy. The device normalizes
the commanded electrode vector so its maximum is 1, which distorts any
NON-uniform command — a commanded (1,0,0,0) arrives as roughly
(1, 0.37, 0.36, 0.50), measured on a phantom 2026-08-30. A uniform command
is the one case the normalization leaves alone: ALL = (0.25,0.25,0.25,0.25)
is delivered as (1,1,1,1), all four electrodes genuinely equal. That is why
the impedance ratios here — and the gain_trims computed from them — are
sound, while Phase 3 and the tilt page (which drive SINGLE_A..D) cannot
isolate an electrode.

So: keep this drive UNIFORM. Switching it to per-electrode or pair drives
to "measure each electrode properly" would silently break the trims, since
the drive would no longer be balanced at the body no matter what is
commanded. Only `level` attenuates; the vector's magnitudes do not.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QWizardPage,
)

from stim_math.calibration.device_protocol import (
    CurrentReading,
    ElectrodePair,
    SkinResistanceReading,
)

logger = logging.getLogger('restim.calibration.phase1')

CALIBRATION_DRIVE_LEVEL = 0.18  # 12% noise floor + safety margin (Test 3)
MEASUREMENT_DURATION_MS = 8000  # ~16 readings expected at 2 Hz; gives margin for slow firmware
MIN_READINGS = 3                # below this we cannot produce a meaningful average


class ImpedancePage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle('Impedance measurement')
        self.setSubTitle(
            'A brief, gentle test signal — you will feel it.'
        )

        self._readings: list[SkinResistanceReading] = []
        # Measured per-electrode current captured alongside impedance (optional —
        # empty when the firmware/telemetry doesn't report NotificationCurrents).
        self._current_readings: list[CurrentReading] = []
        self._complete = False
        self._finalized = False
        self._callback = self._on_reading
        self._current_callback = self._on_current

        layout = QVBoxLayout(self)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)

        self._stack.addWidget(self._build_consent_panel())   # index 0
        self._stack.addWidget(self._build_measure_panel())   # index 1
        self._stack.addWidget(self._build_result_panel())    # index 2

        self._measure_timer = QTimer(self)
        self._measure_timer.setSingleShot(True)
        self._measure_timer.timeout.connect(self._finalize)

    # --- UI construction ---

    def _build_consent_panel(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)
        info = QLabel(
            'This step measures the impedance of each electrode. We will '
            'drive all four electrodes together at about 18% strength for '
            'roughly 8 seconds.\n\n'
            'You will feel a steady, gentle signal during this step. This '
            'is normal — the signal must be perceptible to produce stable '
            'readings.\n\n'
            'You can stop at any time by clicking Cancel.'
        )
        info.setWordWrap(True)
        v.addWidget(info)
        v.addStretch()

        self._consent_button = QPushButton('Begin measurement')
        self._consent_button.clicked.connect(self._start_measurement)
        v.addWidget(self._consent_button, alignment=Qt.AlignmentFlag.AlignRight)
        return panel

    def _build_measure_panel(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)
        self._measure_status = QLabel('Driving signal…')
        self._measure_status.setWordWrap(True)
        v.addWidget(self._measure_status)
        self._measure_progress = QProgressBar()
        self._measure_progress.setRange(0, MEASUREMENT_DURATION_MS)
        self._measure_progress.setValue(0)
        v.addWidget(self._measure_progress)
        self._measure_count = QLabel('Readings: 0')
        v.addWidget(self._measure_count)
        v.addStretch()
        return panel

    def _build_result_panel(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)
        self._result_status = QLabel('')
        self._result_status.setWordWrap(True)
        v.addWidget(self._result_status)
        self._result_detail = QLabel('')
        self._result_detail.setWordWrap(True)
        self._result_detail.setTextFormat(Qt.TextFormat.PlainText)
        v.addWidget(self._result_detail)

        # Retry button — visible only after a failed measurement so the user
        # can re-run without using the Back button.
        retry_row = QHBoxLayout()
        self._retry_button = QPushButton('Retry measurement')
        self._retry_button.clicked.connect(self._start_measurement)
        self._retry_button.setVisible(False)
        retry_row.addWidget(self._retry_button)
        retry_row.addStretch()
        v.addLayout(retry_row)

        v.addStretch()
        return panel

    # --- QWizardPage lifecycle ---

    def initializePage(self) -> None:
        self._readings = []
        self._current_readings = []
        self._complete = False
        self._finalized = False
        self._stack.setCurrentIndex(0)  # consent first
        self._consent_button.setEnabled(True)

    def cleanupPage(self) -> None:
        self._measure_timer.stop()
        adapter = self.wizard().adapter
        try:
            adapter.unsubscribe(self._callback)
        except Exception:
            logger.exception('unsubscribe during cleanup raised')
        try:
            adapter.unsubscribe_current(self._current_callback)
        except Exception:
            logger.exception('unsubscribe_current during cleanup raised')
        # Silence drive whether we finished or bailed
        try:
            adapter.set_calibration_waveform(ElectrodePair.ALL, 0.0, 100)
        except Exception:
            logger.exception('silence during cleanup raised')

    def isComplete(self) -> bool:
        return self._complete

    # --- Measurement flow ---

    def _start_measurement(self) -> None:
        # Reset measurement state so this method is safe to re-run via Retry.
        self._readings = []
        self._current_readings = []
        self._finalized = False
        self._complete = False
        self._retry_button.setVisible(False)

        self._consent_button.setEnabled(False)
        self._stack.setCurrentIndex(1)
        self._measure_progress.setValue(0)
        self._measure_count.setText('Readings: 0')
        self._measure_status.setText(
            f'Driving balanced signal at {int(CALIBRATION_DRIVE_LEVEL * 100)}% '
            f'for {MEASUREMENT_DURATION_MS // 1000} seconds…'
        )

        adapter = self.wizard().adapter
        adapter.subscribe(self._callback)
        # Also capture measured current during the same drive (best-effort —
        # silently yields nothing on backends without current telemetry).
        try:
            adapter.subscribe_current(self._current_callback)
        except Exception:
            logger.exception('subscribe_current raised')
        adapter.set_calibration_waveform(
            ElectrodePair.ALL,
            CALIBRATION_DRIVE_LEVEL,
            MEASUREMENT_DURATION_MS,
        )

        self._measure_start_ms = self._now_ms()
        self._measure_timer.start(MEASUREMENT_DURATION_MS)
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(100)
        self._tick_timer.timeout.connect(self._tick_progress)
        self._tick_timer.start()

    def _tick_progress(self) -> None:
        elapsed = self._now_ms() - self._measure_start_ms
        self._measure_progress.setValue(min(elapsed, MEASUREMENT_DURATION_MS))
        self._measure_count.setText(f'Readings: {len(self._readings)}')

    def _on_reading(self, reading: SkinResistanceReading) -> None:
        self._readings.append(reading)

    def _on_current(self, reading: CurrentReading) -> None:
        # Only the balanced whole-array drive gives a clean per-electrode
        # current comparison; ignore anything captured under another drive.
        if reading.drive_pair == ElectrodePair.ALL:
            self._current_readings.append(reading)

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True

        self._tick_timer.stop()
        adapter = self.wizard().adapter
        try:
            adapter.unsubscribe(self._callback)
        except Exception:
            logger.exception('unsubscribe during finalize raised')
        try:
            adapter.unsubscribe_current(self._current_callback)
        except Exception:
            logger.exception('unsubscribe_current during finalize raised')
        adapter.set_calibration_waveform(ElectrodePair.ALL, 0.0, 100)

        self._stack.setCurrentIndex(2)

        if len(self._readings) < MIN_READINGS:
            self._result_status.setText(
                f'Only {len(self._readings)} reading(s) received — need at '
                f'least {MIN_READINGS}. Click Retry to run the measurement '
                f'again. If retries keep failing, check the device connection.'
            )
            self._result_detail.setText('')
            self._retry_button.setVisible(True)
            self._complete = False
            self.completeChanged.emit()
            return

        impedances = self._trimmed_mean_per_electrode()

        # Hand to session — also auto-computes gain_trims
        warnings = self.wizard().session.record_impedances(impedances)

        # Best-effort: record measured current for Phase 3's opt-in balance.
        currents = self._mean_current_per_electrode()
        if currents:
            self.wizard().session.record_measured_currents(currents)

        # Surface result
        rows = []
        for name in sorted(impedances):
            z = impedances[name]
            trim = self.wizard().session.gain_trims.get(name, 1.0)
            rows.append(
                f'{name}: |Z| = {abs(z):.0f} Ω '
                f'(R={z.real:.0f}, X={z.imag:+.0f}), '
                f'gain_trim = {trim:.2f}'
            )
        detail = '\n'.join(rows)
        if warnings:
            detail += '\n\nWarnings:\n' + '\n'.join(f'  • {w}' for w in warnings)

        self._result_status.setText(
            f'Measurement complete ({len(self._readings)} readings). '
            f'Click Next to continue.'
        )
        self._result_detail.setText(detail)
        self._complete = True
        self.completeChanged.emit()

    def _trimmed_mean_per_electrode(self) -> dict[str, complex]:
        """Per-electrode mean. With 5+ samples, drops min/max by |Z|
        (trimmed mean); with fewer samples, plain mean — preserving
        accuracy when readings are scarce."""
        result: dict[str, complex] = {}
        for name, attr in (('E1', 'Z_a'), ('E2', 'Z_b'),
                           ('E3', 'Z_c'), ('E4', 'Z_d')):
            values = [getattr(r, attr) for r in self._readings
                      if getattr(r, attr) is not None]
            if not values:
                continue
            if len(values) >= 5:
                values.sort(key=abs)
                values = values[1:-1]
            result[name] = sum(values) / len(values)
        return result

    def _mean_current_per_electrode(self) -> dict[str, float]:
        """Per-electrode mean RMS current (amps) over the balanced-drive
        readings. Empty dict when no current telemetry was received, so the
        caller can leave Phase 3's measured-current balance disabled."""
        result: dict[str, float] = {}
        for name, attr in (('E1', 'I_a'), ('E2', 'I_b'),
                           ('E3', 'I_c'), ('E4', 'I_d')):
            values = [getattr(r, attr) for r in self._current_readings
                      if getattr(r, attr) is not None]
            if values:
                result[name] = sum(values) / len(values)
        return result

    @staticmethod
    def _now_ms() -> int:
        import time
        return int(time.time() * 1000)
