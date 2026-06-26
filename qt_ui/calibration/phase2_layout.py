"""
Phase 2: layout inference + confirmation.

Math layer's infer_layout() returns a coarse category (balanced_quad,
three_phase, etc.) with a confidence. This page surfaces that as a
default selection in a radio group; the user can confirm or override.

Phase 0 already caught the gross error cases (no_contact, partial), so
the input to this page is impedances from a fully-connected device.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWizardPage,
)

logger = logging.getLogger('restim.calibration.phase2')

# Display order + descriptions for the radio choices. Names must match
# the categories returned by stim_math.calibration.layout_inference.
_CHOICES = [
    ('balanced_quad',   'Balanced quad — 4 electrodes, equally connected'),
    ('asymmetric_quad', 'Asymmetric quad — 4 electrodes, one notably different'),
    ('three_phase',     'Three-phase — 3 electrodes (one channel unused)'),
    ('other',           'Other / I\'ll configure manually in restim'),
]


class LayoutPage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle('Electrode layout')
        self.setSubTitle(
            'We tried to detect your physical electrode arrangement from the '
            'impedance pattern. Confirm or pick another below.'
        )

        layout = QVBoxLayout(self)

        self._inferred_label = QLabel('')
        self._inferred_label.setWordWrap(True)
        self._inferred_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._inferred_label)

        self._button_group = QButtonGroup(self)
        self._buttons: dict[str, QRadioButton] = {}
        for name, desc in _CHOICES:
            btn = QRadioButton(desc)
            self._button_group.addButton(btn)
            self._buttons[name] = btn
            layout.addWidget(btn)

        layout.addStretch()

        self._note_label = QLabel('')
        self._note_label.setWordWrap(True)
        self._note_label.setStyleSheet('color: gray;')
        layout.addWidget(self._note_label)

    # --- QWizardPage lifecycle ---

    def initializePage(self) -> None:
        session = self.wizard().session
        inferred_name, inferred_confidence = session.infer_and_record_layout()
        logger.info(
            f'inferred layout: {inferred_name!r} confidence={inferred_confidence:.2f}'
        )

        self._inferred_label.setText(
            f'Detected: {self._pretty_name(inferred_name)}  '
            f'(confidence {int(inferred_confidence * 100)}%)'
        )

        # Pre-select the inferred category. If the inference returned something
        # not in our radio list (e.g. "partial"), fall back to "other".
        target = inferred_name if inferred_name in self._buttons else 'other'
        self._buttons[target].setChecked(True)

        if inferred_confidence < 0.7:
            self._note_label.setText(
                'Low-confidence detection — please confirm by selecting the '
                'arrangement that matches your physical setup.'
            )
        else:
            self._note_label.setText('')

    def validatePage(self) -> bool:
        """Record the user's selection. Always succeeds (a radio is always set)."""
        session = self.wizard().session
        inferred_confidence = session.layout_confidence
        inferred_name = session.layout

        selected = self._selected_name()
        user_picked = (selected != inferred_name)

        session.record_layout(
            name=selected,
            confidence=inferred_confidence,
            user_picked=user_picked,
        )
        logger.info(
            f'layout confirmed: {selected!r} (user_override={user_picked})'
        )
        return True

    def isComplete(self) -> bool:
        # A radio is always selected once initializePage runs
        return self._selected_name() is not None

    # --- Helpers ---

    def _selected_name(self) -> str | None:
        for name, btn in self._buttons.items():
            if btn.isChecked():
                return name
        return None

    @staticmethod
    def _pretty_name(name: str) -> str:
        return {
            'balanced_quad':   'balanced quad',
            'asymmetric_quad': 'asymmetric quad',
            'three_phase':     'three-phase',
            'partial':         'partial connection',
            'no_contact':      'no contact detected',
        }.get(name, name)
