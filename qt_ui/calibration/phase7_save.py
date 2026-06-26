"""
Phase 7: name + save.

User picks a profile name and (optional) notes, then clicks Finish.
We finalize the session into a CalibrationProfile and persist it via
stim_math.calibration.save(). Partial-save state is cleared on success.

This is the final page of the wizard — QWizard shows a "Finish" button
instead of "Next" once the page is reached.
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QMessageBox,
    QTextEdit,
    QVBoxLayout,
    QWizardPage,
)

from stim_math.calibration.io import default_path, save

logger = logging.getLogger('restim.calibration.phase7')


class SavePage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle('Save profile')
        self.setSubTitle(
            'Name this calibration profile and click Finish. The profile is '
            'saved to ~/.restim/calibration.json.'
        )
        self.setFinalPage(True)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel('Profile name:'))
        self._name_edit = QLineEdit('default')
        self._name_edit.setPlaceholderText('default')
        layout.addWidget(self._name_edit)

        layout.addWidget(QLabel('Notes (optional):'))
        self._notes_edit = QTextEdit()
        self._notes_edit.setPlaceholderText(
            'e.g. "morning session, full sleeve, fresh lube"'
        )
        self._notes_edit.setMaximumHeight(80)
        layout.addWidget(self._notes_edit)

        layout.addStretch()

        self._status_label = QLabel('')
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

    # --- QWizardPage lifecycle ---

    def initializePage(self) -> None:
        # Prefill name from session.user_label if it's been set previously.
        if self.wizard().session.user_label:
            self._name_edit.setText(self.wizard().session.user_label)
        self._status_label.setText('')

    def validatePage(self) -> bool:
        """Finalize and save. Returns True only on successful save."""
        session = self.wizard().session
        session.user_label = self._name_edit.text().strip() or 'default'
        session.notes = self._notes_edit.toPlainText().strip()

        try:
            profile = session.finalize()
        except ValueError as e:
            self._status_label.setText(f'Cannot finalize: {e}')
            logger.error(f'finalize failed: {e}')
            return False

        try:
            save(profile)
        except (ValueError, OSError) as e:
            self._status_label.setText(f'Save failed: {e}')
            logger.error(f'save failed: {e}')
            return False

        try:
            session.clear_partial()
        except Exception:
            logger.exception('clear_partial raised')

        logger.info(f'profile saved: user_label={session.user_label}')

        # Show a confirmation dialog explaining the next step.
        # Without this, the user has no idea that wizard-exit also stops
        # playback for safety, and may think their sliders are broken.
        QMessageBox.information(
            self,
            'Calibration saved',
            f'Profile "{session.user_label}" saved to:\n'
            f'{default_path()}\n\n'
            f'The per-electrode trims have been applied to your main '
            f'calibration sliders and will be remembered across restim restarts.\n\n'
            f'Playback has been stopped for safety. '
            f'Press the Play button in restim\'s toolbar to resume.',
        )
        return True
