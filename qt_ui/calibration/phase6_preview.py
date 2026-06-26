"""
Phase 6: preview at preferred target.

10-second drive at the user's preferred_target to verify the calibration
feels right before saving. Always optional — user can skip and proceed
to save without playing the preview.

v1 simplification: drives the calibration algorithm with balanced ALL
drive, not the user's normal pattern, and does NOT apply per-electrode
gain_trims (the calibration algorithm keeps trims at 1.0 by design).
The preview validates that preferred_target sits in a comfortable spot;
verifying the trims and the full pattern is a future-version enhancement.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWizardPage,
)

from stim_math.calibration.device_protocol import ElectrodePair

logger = logging.getLogger('restim.calibration.phase6')

PREVIEW_DURATION_MS = 10000
PREVIEW_TICK_MS = 100


class PreviewPage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle('Preview')
        self.setSubTitle(
            'Sanity-check by feeling a 10-second sample at your preferred '
            'target level. Optional — skip to save if you\'re ready.'
        )

        self._running = False
        self._start_time_ms: int = 0

        layout = QVBoxLayout(self)

        self._target_label = QLabel('Preferred target: —')
        layout.addWidget(self._target_label)

        self._progress = QProgressBar()
        self._progress.setRange(0, PREVIEW_DURATION_MS)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        layout.addWidget(self._progress)

        button_row = QHBoxLayout()
        self._play_button = QPushButton('Play 10-second preview')
        self._play_button.clicked.connect(self._start_preview)
        button_row.addWidget(self._play_button)

        self._stop_button = QPushButton('Stop')
        self._stop_button.clicked.connect(self._stop_preview)
        self._stop_button.setEnabled(False)
        button_row.addWidget(self._stop_button)
        button_row.addStretch()
        layout.addLayout(button_row)

        layout.addStretch()

        self._note_label = QLabel(
            'Note: v1 preview uses a balanced steady signal, not your normal '
            'pattern, and does not apply the per-electrode trims you tuned in '
            'Phase 3. If everything feels comfortable here, the saved profile '
            'is good to use.'
        )
        self._note_label.setWordWrap(True)
        self._note_label.setStyleSheet('color: gray; font-style: italic;')
        layout.addWidget(self._note_label)

        self._status_label = QLabel('')
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(PREVIEW_TICK_MS)
        self._tick_timer.timeout.connect(self._tick)

    # --- QWizardPage lifecycle ---

    def initializePage(self) -> None:
        session = self.wizard().session
        target = session.landmark_comfortable or 0.0
        self._target_label.setText(
            f'Preferred target: {int(target * 100)}% '
            f'(from your earlier "comfortably firm" mark)'
        )
        self._progress.setValue(0)
        self._status_label.setText('')

    def cleanupPage(self) -> None:
        self._stop_preview()

    def isComplete(self) -> bool:
        # Preview is always optional
        return True

    # --- Preview control ---

    def _start_preview(self) -> None:
        if self._running:
            return
        adapter = self.wizard().adapter
        if not adapter.is_connected():
            self._status_label.setText('Device not connected.')
            return

        target = self.wizard().session.landmark_comfortable
        if target is None or target <= 0:
            self._status_label.setText('No preferred target set — cannot preview.')
            return

        self._running = True
        self._start_time_ms = self._now_ms()
        adapter.set_calibration_waveform(
            ElectrodePair.ALL, target, PREVIEW_DURATION_MS,
        )
        self._tick_timer.start()
        self._play_button.setEnabled(False)
        self._stop_button.setEnabled(True)
        self._status_label.setText(
            f'Playing at {int(target * 100)}% for 10 seconds…'
        )

    def _stop_preview(self) -> None:
        if not self._running:
            return
        self._running = False
        self._tick_timer.stop()
        try:
            self.wizard().adapter.set_calibration_waveform(
                ElectrodePair.ALL, 0.0, 100,
            )
        except Exception:
            logger.exception('silence at preview stop raised')
        self._play_button.setEnabled(True)
        self._stop_button.setEnabled(False)
        self._progress.setValue(0)

    def _tick(self) -> None:
        elapsed = self._now_ms() - self._start_time_ms
        if elapsed >= PREVIEW_DURATION_MS:
            self._stop_preview()
            self._status_label.setText(
                'Preview complete. Continue to save, or replay if you want '
                'to feel it again.'
            )
            return
        self._progress.setValue(elapsed)

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)
