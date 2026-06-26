"""
Welcome page: pre-flight instructions, volume guidance, and a test-signal
button.

Surfaced before the impedance check so the user can:
1. Read what the wizard will do
2. Understand which volume controls matter (the FOC-stim physical knob; NOT
   restim's master volume, which is bypassed during calibration)
3. Feel the actual 18% calibration drive briefly and adjust their physical
   knob until comfortable — before any measurement phase starts.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWizardPage,
)

from stim_math.calibration.device_protocol import ElectrodePair

logger = logging.getLogger('restim.calibration.welcome')

TEST_DRIVE_LEVEL = 0.18           # matches Phase 1's calibration drive
TEST_DURATION_MS = 5000
TICK_MS = 100


class WelcomePage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle('Calibration wizard')
        self.setSubTitle(
            'Dial in your FOC-stim\'s physical volume knob using the Test '
            'button below, then click Next.'
        )

        self._running = False
        self._start_time_ms: int = 0

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # --- Volume guidance (the most important part — make it short) ---
        volume_block = QLabel(
            '<b>Volume control:</b><br>'
            'During calibration, restim\'s master volume slider is <b>ignored</b>. '
            'Only the <b>physical knob on your FOC-stim</b> affects what you feel. '
            'You can adjust the knob any time during the wizard.'
        )
        volume_block.setTextFormat(Qt.TextFormat.RichText)
        volume_block.setWordWrap(True)
        layout.addWidget(volume_block)

        # --- Test signal ---
        test_block = QLabel(
            '<b>Set your knob now:</b><br>'
            'Click below to feel a 5-second test signal at 18% '
            '(the same level used for measurement). Adjust your knob until '
            'the signal feels comfortably firm. Repeat as needed.'
        )
        test_block.setTextFormat(Qt.TextFormat.RichText)
        test_block.setWordWrap(True)
        layout.addWidget(test_block)

        button_row = QHBoxLayout()
        self._test_button = QPushButton('Test calibration signal (5 sec)')
        self._test_button.clicked.connect(self._start_test)
        button_row.addWidget(self._test_button)

        self._stop_button = QPushButton('Stop')
        self._stop_button.clicked.connect(self._stop_test)
        self._stop_button.setEnabled(False)
        button_row.addWidget(self._stop_button)
        button_row.addStretch()
        layout.addLayout(button_row)

        self._progress = QProgressBar()
        self._progress.setRange(0, TEST_DURATION_MS)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        layout.addWidget(self._progress)

        self._status_label = QLabel('')
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        layout.addStretch()

        # --- Footer: what the wizard will do + safety note ---
        footer = QLabel(
            '<i>The wizard runs 7 short steps (about 5 minutes). Steps 1, 3, '
            '4, and 6 deliver a gentle signal. Cancel always silences the '
            'device. If contact is lost mid-wizard, power-cycle the FOC-stim '
            'and restart.</i>'
        )
        footer.setTextFormat(Qt.TextFormat.RichText)
        footer.setWordWrap(True)
        footer.setStyleSheet('color: #888;')
        layout.addWidget(footer)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(TICK_MS)
        self._tick_timer.timeout.connect(self._tick)

    # --- QWizardPage lifecycle ---

    def cleanupPage(self) -> None:
        self._stop_test()

    # --- Test signal control ---

    def _start_test(self) -> None:
        if self._running:
            return
        adapter = self.wizard().adapter
        if not adapter.is_connected():
            self._status_label.setText('Device not connected.')
            return

        self._running = True
        self._start_time_ms = self._now_ms()
        adapter.set_calibration_waveform(
            ElectrodePair.ALL, TEST_DRIVE_LEVEL, TEST_DURATION_MS,
        )
        self._tick_timer.start()
        self._test_button.setEnabled(False)
        self._stop_button.setEnabled(True)
        self._status_label.setText(
            f'Driving at {int(TEST_DRIVE_LEVEL * 100)}% for '
            f'{TEST_DURATION_MS // 1000} seconds — adjust your knob now.'
        )

    def _stop_test(self) -> None:
        if not self._running:
            return
        self._running = False
        self._tick_timer.stop()
        try:
            self.wizard().adapter.set_calibration_waveform(
                ElectrodePair.ALL, 0.0, 100,
            )
        except Exception:
            logger.exception('silence at test stop raised')
        self._test_button.setEnabled(True)
        self._stop_button.setEnabled(False)
        self._progress.setValue(0)
        self._status_label.setText(
            'Test ended. Repeat if needed, or click Next.'
        )

    def _tick(self) -> None:
        elapsed = self._now_ms() - self._start_time_ms
        if elapsed >= TEST_DURATION_MS:
            self._stop_test()
            return
        self._progress.setValue(elapsed)

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)
