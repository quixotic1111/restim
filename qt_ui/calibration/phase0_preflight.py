"""
Phase 0: contact preflight.

Listens for ~3 seconds at 0% drive (no signal). The firmware emits
free-running resistance readings even at zero drive (verified
empirically — Test 2), so this phase can detect gross open-circuit
contact loss without driving anything the user would feel.

Threshold: |Z| < OPEN_CIRCUIT_THRESHOLD_OHMS (50 kΩ, from Test 5).

Outcomes:
- All electrodes below threshold → pass, Next enabled
- One or more electrodes above threshold → fail, user must reseat
- No readings in 5 seconds → fail with "device not responding"
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWizardPage,
)

from stim_math.calibration.device_protocol import ElectrodePair, SkinResistanceReading
from stim_math.calibration.layout_inference import OPEN_CIRCUIT_THRESHOLD_OHMS

logger = logging.getLogger('restim.calibration.phase0')

SAMPLE_DURATION_MS = 3000
MIN_READINGS = 4
NO_READINGS_TIMEOUT_MS = 5000


class PreflightPage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle('Checking electrode contact')
        self.setSubTitle(
            'Listening at zero output to verify all electrodes are connected. '
            'You will not feel anything during this step.'
        )

        self._readings: list[SkinResistanceReading] = []
        self._passed = False
        self._finalized = False
        self._callback = self._on_reading  # stable reference for unsubscribe

        layout = QVBoxLayout(self)

        self._status_label = QLabel('Waiting for device…')
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate while sampling
        self._progress.setTextVisible(False)
        layout.addWidget(self._progress)

        self._detail_label = QLabel('')
        self._detail_label.setWordWrap(True)
        self._detail_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._detail_label)

        layout.addStretch()

        self._sample_timer = QTimer(self)
        self._sample_timer.setSingleShot(True)
        self._sample_timer.timeout.connect(self._finalize)

        self._no_readings_timer = QTimer(self)
        self._no_readings_timer.setSingleShot(True)
        self._no_readings_timer.timeout.connect(self._fail_no_readings)

    # --- QWizardPage lifecycle ---

    def initializePage(self) -> None:
        self._readings = []
        self._passed = False
        self._finalized = False
        self._status_label.setText('Listening for resistance readings…')
        self._detail_label.setText('')
        self._progress.setRange(0, 0)

        adapter = self.wizard().adapter
        adapter.subscribe(self._callback)
        # Ensure adapter is in calibration mode with zero drive (so any
        # subsequent phase starts from a known-zero baseline).
        adapter.set_calibration_waveform(ElectrodePair.ALL, 0.0, SAMPLE_DURATION_MS)

        self._sample_timer.start(SAMPLE_DURATION_MS)
        self._no_readings_timer.start(NO_READINGS_TIMEOUT_MS)

    def cleanupPage(self) -> None:
        self._sample_timer.stop()
        self._no_readings_timer.stop()
        try:
            self.wizard().adapter.unsubscribe(self._callback)
        except Exception:
            logger.exception('unsubscribe during cleanup raised')

    def isComplete(self) -> bool:
        return self._passed

    # --- Reading collection ---

    def _on_reading(self, reading: SkinResistanceReading) -> None:
        # First reading: confirm stream is alive, cancel no-readings timeout
        if not self._readings:
            self._no_readings_timer.stop()
            self._status_label.setText('Receiving readings — checking contact…')
        self._readings.append(reading)

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True

        self._progress.setRange(0, 1)
        self._progress.setValue(1)

        if not self._readings:
            self._fail_no_readings()
            return

        # Average |Z| per electrode across the sample window
        avg_z = self._average_magnitudes()
        bad = {name: m for name, m in avg_z.items()
               if m is not None and m >= OPEN_CIRCUIT_THRESHOLD_OHMS}

        if bad:
            self._fail_open_circuit(avg_z, bad)
        else:
            self._pass(avg_z)

    def _average_magnitudes(self) -> dict[str, float | None]:
        """Per-electrode mean |Z| across the buffered readings."""
        sums = {'E1': 0.0, 'E2': 0.0, 'E3': 0.0, 'E4': 0.0}
        counts = {'E1': 0, 'E2': 0, 'E3': 0, 'E4': 0}
        for r in self._readings:
            for name, z in (('E1', r.Z_a), ('E2', r.Z_b),
                            ('E3', r.Z_c), ('E4', r.Z_d)):
                if z is None:
                    continue
                sums[name] += abs(z)
                counts[name] += 1
        return {
            name: (sums[name] / counts[name]) if counts[name] > 0 else None
            for name in sums
        }

    # --- Outcomes ---

    def _pass(self, avg_z: dict[str, float | None]) -> None:
        readings_list = ', '.join(
            f'{n}={int(v)}Ω' if v is not None else f'{n}=N/A'
            for n, v in avg_z.items()
        )
        self._status_label.setText('All electrodes connected. Click Next to continue.')
        self._detail_label.setText(f'Baseline (0% drive): {readings_list}')
        self._passed = True
        self.completeChanged.emit()

    def _fail_open_circuit(
        self,
        avg_z: dict[str, float | None],
        bad: dict[str, float],
    ) -> None:
        names = ', '.join(sorted(bad))
        self._status_label.setText(
            f'Electrode(s) appear disconnected: {names}. '
            f'Reseat and reconnect, then restart the wizard.'
        )
        detail = ', '.join(
            f'{n}={int(v)}Ω' if v is not None else f'{n}=N/A'
            for n, v in avg_z.items()
        )
        self._detail_label.setText(
            f'Threshold for "connected": <{OPEN_CIRCUIT_THRESHOLD_OHMS} Ω.\n'
            f'Measured: {detail}.'
        )
        self._passed = False
        self.completeChanged.emit()

    def _fail_no_readings(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._sample_timer.stop()
        self._status_label.setText(
            'Device is not sending resistance readings. '
            'Check the connection and restart the wizard.'
        )
        self._detail_label.setText(
            'Expected: ~2 readings per second from the FOC-stim. '
            'Got none within 5 seconds.'
        )
        self._passed = False
        self.completeChanged.emit()
