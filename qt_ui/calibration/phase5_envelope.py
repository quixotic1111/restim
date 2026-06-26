"""
Phase 5: envelope confirmation.

Phase 4 captured three landmarks (just-feel, comfortable, max). Those are
also the safe envelope (min_useful / preferred_target / max_comfortable
in the saved profile). This page surfaces them as three sliders so the
user can nudge if the auto-derivation doesn't match what they want.

Sliders enforce strict ordering (min < target < max with a minimum gap)
so the user can't enter an invalid envelope.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWizardPage,
)

logger = logging.getLogger('restim.calibration.phase5')

SLIDER_SCALE = 1000          # slider int range → 0..1.0 with 0.001 resolution
MIN_GAP = 10                 # minimum slider-int gap between adjacent sliders


class EnvelopePage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle('Comfort envelope')
        self.setSubTitle(
            'These are the levels you marked in the previous step. Nudge if '
            'any feels off, or accept the defaults and click Next.'
        )

        self._auto_values: tuple[float, float, float] = (0.0, 0.5, 1.0)

        layout = QVBoxLayout(self)
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        self._sliders: dict[str, QSlider] = {}
        self._labels: dict[str, QLabel] = {}
        self._descs = {
            'min':    'Minimum useful (below this you feel nothing)',
            'target': 'Preferred target (comfortably firm)',
            'max':    'Maximum comfortable (you don\'t want it higher)',
        }
        for row, key in enumerate(('min', 'target', 'max')):
            label = QLabel(self._descs[key])
            label.setWordWrap(True)
            grid.addWidget(label, row, 0)

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(1, SLIDER_SCALE)
            slider.setSingleStep(5)
            slider.valueChanged.connect(
                lambda _, k=key: self._on_slider_changed(k),
            )
            grid.addWidget(slider, row, 1)
            self._sliders[key] = slider

            value_label = QLabel('—')
            value_label.setMinimumWidth(60)
            grid.addWidget(value_label, row, 2)
            self._labels[key] = value_label

        layout.addStretch()

        row = QHBoxLayout()
        row.addStretch()
        self._reset_button = QPushButton('Reset to auto-derived')
        self._reset_button.clicked.connect(self._reset_to_auto)
        row.addWidget(self._reset_button)
        layout.addLayout(row)

        self._status_label = QLabel('')
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

    # --- QWizardPage lifecycle ---

    def initializePage(self) -> None:
        session = self.wizard().session
        # Defaults come from the Phase 4 landmarks
        self._auto_values = (
            session.landmark_just_feel or 0.05,
            session.landmark_comfortable or 0.5,
            session.landmark_max or 0.85,
        )
        # Initialize each slider — block signals so the ordering-enforcement
        # logic doesn't run during the bulk set.
        for key, val in zip(('min', 'target', 'max'), self._auto_values):
            s = self._sliders[key]
            s.blockSignals(True)
            s.setValue(int(round(val * SLIDER_SCALE)))
            s.blockSignals(False)
            self._update_label(key)
        self._update_ranges()
        self._status_label.setText('')

    def validatePage(self) -> bool:
        """Write back to the session as adjusted landmarks."""
        v_min = self._sliders['min'].value() / SLIDER_SCALE
        v_target = self._sliders['target'].value() / SLIDER_SCALE
        v_max = self._sliders['max'].value() / SLIDER_SCALE
        if not (0.0 < v_min < v_target < v_max <= 1.0):
            self._status_label.setText(
                f'Envelope must be strictly ascending and in (0, 1]. '
                f'Got {v_min:.3f} / {v_target:.3f} / {v_max:.3f}.'
            )
            return False
        # The landmarks and the envelope are the same data — update the
        # session's landmarks to reflect any nudges here. finalize() will
        # rebuild perception_curve + safe_envelope from these.
        self.wizard().session.record_landmarks(v_min, v_target, v_max)
        logger.info(f'phase 5 envelope: {v_min} / {v_target} / {v_max}')
        return True

    # --- Slider behavior ---

    def _on_slider_changed(self, key: str) -> None:
        self._update_label(key)
        self._update_ranges()

    def _update_ranges(self) -> None:
        """Adjust adjacent slider ranges so ordering is always preserved."""
        v_min = self._sliders['min'].value()
        v_target = self._sliders['target'].value()
        v_max = self._sliders['max'].value()
        # min must be < target - MIN_GAP
        self._sliders['min'].setMaximum(max(1, v_target - MIN_GAP))
        # target must be > min + MIN_GAP AND < max - MIN_GAP
        self._sliders['target'].setMinimum(min(SLIDER_SCALE, v_min + MIN_GAP))
        self._sliders['target'].setMaximum(max(1, v_max - MIN_GAP))
        # max must be > target + MIN_GAP
        self._sliders['max'].setMinimum(min(SLIDER_SCALE, v_target + MIN_GAP))

    def _update_label(self, key: str) -> None:
        val = self._sliders[key].value() / SLIDER_SCALE
        self._labels[key].setText(f'{int(val * 100)}%')

    def _reset_to_auto(self) -> None:
        """Restore the Phase-4-derived values."""
        # Reset all sliders to their wide ranges first to allow any ordering,
        # then set values, then re-narrow ranges.
        for s in self._sliders.values():
            s.blockSignals(True)
            s.setRange(1, SLIDER_SCALE)
        for key, val in zip(('min', 'target', 'max'), self._auto_values):
            self._sliders[key].setValue(int(round(val * SLIDER_SCALE)))
            self._update_label(key)
        for s in self._sliders.values():
            s.blockSignals(False)
        self._update_ranges()
        self._status_label.setText('Reset to auto-derived values.')
