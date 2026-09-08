# SPDX-License-Identifier: GPL-3.0-or-later
"""Contact-check result page — impedance drift against the saved profile.

The one-minute answer to "has contact drifted?". Reached only from the
Welcome page's contact-check route, which measures Phase 1 and comes
straight here, skipping layout, balance, perception, envelope, tilt and
preview.

★The drift number is shown whether or not anything is saved. Reading it is
often the entire reason to run the check — nothing has to change on disk for
the question to be answered.

⚠This page MERGES into an existing profile and refuses without one. The
Welcome page disables the route when no profile loads, so arriving here
without a base means something went wrong between the two; the guard below
is the backstop, not the primary check.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWizardPage,
)

from stim_math.calibration.io import load, save

logger = logging.getLogger(__name__)

#: Ratios outside this band are called out. Contact varies run to run; the
#: point is to separate "same as before" from "something moved", not to
#: pretend a 3% wobble is meaningful.
_DRIFT_NOTABLE = 0.10


class DriftPage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle('Contact check')
        self.setSubTitle(
            'How far each electrode has drifted since your saved profile.')

        self._base = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self._table = QLabel('')
        self._table.setTextFormat(Qt.TextFormat.RichText)
        self._table.setWordWrap(True)
        layout.addWidget(self._table)

        self._verdict = QLabel('')
        self._verdict.setTextFormat(Qt.TextFormat.RichText)
        self._verdict.setWordWrap(True)
        layout.addWidget(self._verdict)

        layout.addStretch()

        note = QLabel(
            '<i>Finishing saves the refreshed impedances into your existing '
            'profile. Gain trims, the perception curve, the safe envelope and '
            'the tilt are kept exactly as they are — this is a measurement, '
            'not a recalibration. Cancel to keep the profile untouched; the '
            'numbers above are yours either way.</i>')
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setWordWrap(True)
        note.setStyleSheet('color: #888;')
        layout.addWidget(note)

        self._status_label = QLabel('')
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

    # --- QWizardPage lifecycle ---

    def initializePage(self) -> None:
        self._status_label.setText('')
        session = self.wizard().session
        base, result = load()
        if base is None or not getattr(result, 'ok', False):
            self._base = None
            self._table.setText('')
            self._verdict.setText(
                '<b>No usable saved profile.</b> A contact check compares '
                'against one, so there is nothing to measure against. Run a '
                'full calibration instead.')
            return

        self._base = base
        drift = session.impedance_drift(base)
        if not drift:
            self._table.setText('')
            self._verdict.setText(
                '<b>No comparable electrodes.</b> The saved profile has no '
                'impedances to compare against.')
            return

        rows = ['<table cellpadding="4">'
                '<tr><td><b>Electrode</b></td><td><b>Saved</b></td>'
                '<td><b>Now</b></td><td><b>Change</b></td></tr>']
        worst = 0.0
        for name in sorted(drift):
            ratio = drift[name]
            prev = base.electrodes[name].Z_magnitude
            now = prev * ratio
            pct = (ratio - 1.0) * 100.0
            worst = max(worst, abs(ratio - 1.0))
            # Sign is meaningful: higher |Z| is worse contact, lower is better.
            colour = '#888' if abs(ratio - 1.0) < _DRIFT_NOTABLE else (
                '#c0392b' if ratio > 1.0 else '#2980b9')
            rows.append(
                f'<tr><td>{name}</td><td>{prev:.0f} &#8486;</td>'
                f'<td>{now:.0f} &#8486;</td>'
                f'<td style="color:{colour}">{pct:+.1f}%</td></tr>')
        rows.append('</table>')
        self._table.setText(''.join(rows))

        if worst < _DRIFT_NOTABLE:
            self._verdict.setText(
                '<b>Contact looks unchanged.</b> Every electrode is within '
                f'{_DRIFT_NOTABLE * 100:.0f}% of its saved value.')
        else:
            self._verdict.setText(
                f'<b>Contact has moved</b> — up to {worst * 100:.0f}% on at '
                'least one electrode. Higher impedance usually means a drier '
                'or looser pad; re-seat it and re-run the check before '
                'reading anything into the numbers.')

    def validatePage(self) -> bool:
        """Merge the fresh impedances into the saved profile and write it."""
        if self._base is None:
            self._status_label.setText(
                'Nothing to save — no usable saved profile to merge into.')
            return False
        session = self.wizard().session
        try:
            profile = session.finalize_impedance_only(self._base)
        except ValueError as e:
            self._status_label.setText(f'Cannot merge: {e}')
            logger.error(f'finalize_impedance_only failed: {e}')
            return False

        try:
            save(profile)
        except (ValueError, OSError) as e:
            self._status_label.setText(f'Save failed: {e}')
            logger.error(f'save failed: {e}')
            return False

        logger.info('impedance-only refresh saved: user_label=%s',
                    profile.user_label)
        QMessageBox.information(
            self, 'Contact check saved',
            'Impedances refreshed. Your gain trims, perception curve and '
            'safe envelope are unchanged.')
        return True
