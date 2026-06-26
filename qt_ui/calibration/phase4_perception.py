"""
Phase 4: perception sweep.

Slowly ramps output from 0% to ~85% over ~60 seconds. The user taps the
"Mark current level" button three times during the ramp, capturing:
1. "Just feel" — first noticeable sensation
2. "Comfortably firm" — preferred target
3. "Max" — top of comfortable range

After all three marks, the ramp stops, drive is silenced, and the
landmarks are recorded in the session. build_from_landmarks() will
convert these into a perception curve + safe envelope at finalize time.

v1 simplification: uses the calibration algorithm with balanced drive,
not the user's normal pattern. Trade-off documented in the page text —
Phase 6 (preview) lets the user verify against their real pattern.
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
    QSizePolicy,
    QVBoxLayout,
    QWizardPage,
)

from stim_math.calibration.device_protocol import ElectrodePair

logger = logging.getLogger('restim.calibration.phase4')

RAMP_DURATION_MS = 60000       # 60-second sweep
RAMP_TARGET_LEVEL = 0.85       # final output level at end of ramp
RAMP_TICK_MS = 100             # update every 100ms

_PROMPTS = (
    'Mark the moment you JUST start to feel the signal.',
    'Now mark when it feels COMFORTABLY FIRM — your preferred target.',
    'Finally, mark MAX — the highest level you would want during use.',
)
_LANDMARK_NAMES = ('Just feel', 'Comfortable', 'Max')


class PerceptionPage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle('Perception calibration')
        self.setSubTitle(
            'A slow 60-second ramp from zero to a moderate level. Tap "Mark" '
            'three times as you reach each landmark below. The ramp stops '
            'automatically after the third mark.'
        )

        self._landmarks: list[float | None] = [None, None, None]
        self._current_step = 0
        self._current_level = 0.0
        self._start_time_ms: int = 0
        self._ramp_running = False

        layout = QVBoxLayout(self)

        # Ramp display
        self._prompt_label = QLabel(_PROMPTS[0])
        self._prompt_label.setWordWrap(True)
        font = self._prompt_label.font()
        font.setPointSize(font.pointSize() + 1)
        font.setBold(True)
        self._prompt_label.setFont(font)
        layout.addWidget(self._prompt_label)

        self._level_label = QLabel('Output: 0%')
        layout.addWidget(self._level_label)

        self._progress = QProgressBar()
        self._progress.setRange(0, RAMP_DURATION_MS)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        layout.addWidget(self._progress)

        # Big mark button
        self._mark_button = QPushButton('Mark current level')
        self._mark_button.setMinimumHeight(48)
        self._mark_button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed,
        )
        self._mark_button.clicked.connect(self._on_mark_clicked)
        layout.addWidget(self._mark_button)

        # Captured landmarks display
        captured_layout = QHBoxLayout()
        self._captured_labels: list[QLabel] = []
        for label_text in _LANDMARK_NAMES:
            l = QLabel(f'{label_text}:\n—')
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            l.setStyleSheet('border: 1px solid gray; padding: 6px;')
            captured_layout.addWidget(l)
            self._captured_labels.append(l)
        layout.addLayout(captured_layout)

        # Restart button
        restart_row = QHBoxLayout()
        restart_row.addStretch()
        self._restart_button = QPushButton('Restart sweep')
        self._restart_button.clicked.connect(self._restart_sweep)
        restart_row.addWidget(self._restart_button)
        layout.addLayout(restart_row)

        # Status / error line
        self._status_label = QLabel('')
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._ramp_timer = QTimer(self)
        self._ramp_timer.setInterval(RAMP_TICK_MS)
        self._ramp_timer.timeout.connect(self._tick_ramp)

    # --- QWizardPage lifecycle ---

    def initializePage(self) -> None:
        self._reset_state()
        self._start_ramp()

    def cleanupPage(self) -> None:
        self._stop_ramp()
        try:
            self.wizard().adapter.set_calibration_waveform(
                ElectrodePair.ALL, 0.0, 100,
            )
        except Exception:
            logger.exception('silence during cleanup raised')

    def isComplete(self) -> bool:
        return all(m is not None for m in self._landmarks)

    def validatePage(self) -> bool:
        if not self.isComplete():
            self._status_label.setText('All three landmarks must be marked.')
            return False
        jf, cf, mx = self._landmarks
        if not (0.0 < jf < cf < mx <= 1.0):
            self._status_label.setText(
                f'Landmarks must be in strict ascending order. Got '
                f'{jf:.3f} / {cf:.3f} / {mx:.3f}. Click "Restart sweep" '
                f'and try again.'
            )
            return False
        try:
            self.wizard().session.record_landmarks(jf, cf, mx)
        except ValueError as e:
            self._status_label.setText(f'Cannot save landmarks: {e}')
            return False
        logger.info(f'phase 4 landmarks: just_feel={jf}, comfortable={cf}, max={mx}')
        return True

    # --- Ramp control ---

    def _reset_state(self) -> None:
        self._landmarks = [None, None, None]
        self._current_step = 0
        self._current_level = 0.0
        self._level_label.setText('Output: 0%')
        self._progress.setValue(0)
        self._prompt_label.setText(_PROMPTS[0])
        for i, label_text in enumerate(_LANDMARK_NAMES):
            self._captured_labels[i].setText(f'{label_text}:\n—')
        self._mark_button.setEnabled(True)
        self._status_label.setText('')

    def _start_ramp(self) -> None:
        self._start_time_ms = self._now_ms()
        self._ramp_running = True
        # Set initial zero drive; the adapter holds this until we update it.
        self.wizard().adapter.set_calibration_waveform(
            ElectrodePair.ALL, 0.0, RAMP_DURATION_MS,
        )
        self._ramp_timer.start()

    def _stop_ramp(self) -> None:
        if self._ramp_running:
            self._ramp_timer.stop()
            self._ramp_running = False

    def _tick_ramp(self) -> None:
        elapsed = self._now_ms() - self._start_time_ms
        if elapsed >= RAMP_DURATION_MS:
            self._on_ramp_timeout()
            return

        level = (elapsed / RAMP_DURATION_MS) * RAMP_TARGET_LEVEL
        self._current_level = level
        try:
            self.wizard().adapter.set_calibration_waveform(
                ElectrodePair.ALL, level, RAMP_DURATION_MS,
            )
        except Exception:
            logger.exception('drive update during ramp raised')

        self._level_label.setText(f'Output: {int(level * 100)}%')
        self._progress.setValue(int(elapsed))

    def _on_ramp_timeout(self) -> None:
        """Ramp reached the end before all landmarks were marked."""
        self._stop_ramp()
        try:
            self.wizard().adapter.set_calibration_waveform(
                ElectrodePair.ALL, 0.0, 100,
            )
        except Exception:
            logger.exception('silence at ramp timeout raised')
        self._mark_button.setEnabled(False)
        missing = [_LANDMARK_NAMES[i]
                   for i, m in enumerate(self._landmarks) if m is None]
        self._status_label.setText(
            f'Ramp finished without marking: {", ".join(missing)}. '
            f'Click "Restart sweep" to try again.'
        )

    def _restart_sweep(self) -> None:
        self._stop_ramp()
        try:
            self.wizard().adapter.set_calibration_waveform(
                ElectrodePair.ALL, 0.0, 100,
            )
        except Exception:
            logger.exception('silence at restart raised')
        self._reset_state()
        self._start_ramp()
        self.completeChanged.emit()

    # --- Mark handling ---

    def _on_mark_clicked(self) -> None:
        if self._current_step >= 3:
            return
        level = self._current_level
        self._landmarks[self._current_step] = level
        self._captured_labels[self._current_step].setText(
            f'{_LANDMARK_NAMES[self._current_step]}:\n{int(level * 100)}%'
        )
        self._current_step += 1

        if self._current_step >= 3:
            self._finalize_after_marks()
        else:
            self._prompt_label.setText(_PROMPTS[self._current_step])

    def _finalize_after_marks(self) -> None:
        self._stop_ramp()
        try:
            self.wizard().adapter.set_calibration_waveform(
                ElectrodePair.ALL, 0.0, 100,
            )
        except Exception:
            logger.exception('silence at landmark finalize raised')
        self._mark_button.setEnabled(False)
        self._prompt_label.setText(
            'All three landmarks marked. Click Next to continue.'
        )
        self.completeChanged.emit()

    # --- Helpers ---

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)
