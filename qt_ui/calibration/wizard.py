"""
CalibrationWizard: orchestrates the multi-phase calibration flow.

Owns nothing about the device or signal pipeline directly — those are
passed in via a CalibrationDeviceProtocol adapter. The wizard owns the
CalibrationSession (the in-progress state) and the page sequence.

Lifecycle:
- Caller constructs with (adapter, session) and exec()s it
- On Finish, the wizard saves a CalibrationProfile via stim_math.calibration.save()
- On Cancel/close, the wizard silences the adapter and discards the session
"""

from __future__ import annotations

import logging
from enum import IntEnum

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWizard

from qt_ui.calibration.phase0_preflight import PreflightPage
from qt_ui.calibration.phase1_impedance import ImpedancePage
from qt_ui.calibration.phase2_layout import LayoutPage
from qt_ui.calibration.phase3_balance import BalancePage
from qt_ui.calibration.phase4_perception import PerceptionPage
from qt_ui.calibration.phase5_envelope import EnvelopePage
from qt_ui.calibration.phase6_preview import PreviewPage
from qt_ui.calibration.phase7_save import SavePage
from qt_ui.calibration.phase_tilt import TiltPage
from qt_ui.calibration.welcome import WelcomePage
from stim_math.calibration.device_protocol import CalibrationDeviceProtocol
from stim_math.calibration.session import CalibrationSession

logger = logging.getLogger('restim.calibration.wizard')


class WizardPageId(IntEnum):
    WELCOME = 0
    PREFLIGHT = 1
    IMPEDANCE = 2
    LAYOUT = 3
    BALANCE = 4
    PERCEPTION = 5
    ENVELOPE = 6
    TILT = 9          # frequency-response calibration (skippable)
    PREVIEW = 7
    SAVE = 8


class CalibrationWizard(QWizard):
    """Top-level wizard for the calibration flow.

    `adapter` and `session` are accessible to pages via self.wizard().adapter
    and self.wizard().session. Pages drive the adapter and update the session.

    `wizard_finished` is emitted whenever the wizard exits (Finish, Cancel,
    or window-close). Connect this to mainwindow.signal_stop so playback is
    fully stopped on exit — preventing an abrupt return of the user's pre-
    wizard signal level when the SwitchingAlgorithm is no longer suppressing
    it.
    """

    wizard_finished = Signal()

    def __init__(
        self,
        adapter: CalibrationDeviceProtocol,
        session: CalibrationSession,
        parent=None,
    ):
        super().__init__(parent)
        self.adapter = adapter
        self.session = session

        self.setWindowTitle('FOC-stim Calibration')
        self.setWizardStyle(QWizard.ClassicStyle)
        # Resizable rather than fixed — different phases have different
        # vertical content density, and macOS title-bar height varies.
        self.resize(640, 620)
        self.setMinimumSize(560, 520)
        # Cancel button stays — calling reject() goes through stop_all_output
        # so any in-flight drive gets silenced on close.

        self.setPage(WizardPageId.WELCOME, WelcomePage())
        self.setPage(WizardPageId.PREFLIGHT, PreflightPage())
        self.setPage(WizardPageId.IMPEDANCE, ImpedancePage())
        self.setPage(WizardPageId.LAYOUT, LayoutPage())
        self.setPage(WizardPageId.BALANCE, BalancePage())
        self.setPage(WizardPageId.PERCEPTION, PerceptionPage())
        self.setPage(WizardPageId.ENVELOPE, EnvelopePage())
        self.setPage(WizardPageId.TILT, TiltPage())
        self.setPage(WizardPageId.PREVIEW, PreviewPage())
        self.setPage(WizardPageId.SAVE, SavePage())
        self.setStartId(WizardPageId.WELCOME)

    def nextId(self) -> int:
        cur = self.currentId()
        if cur == WizardPageId.WELCOME:
            return WizardPageId.PREFLIGHT
        if cur == WizardPageId.PREFLIGHT:
            return WizardPageId.IMPEDANCE
        if cur == WizardPageId.IMPEDANCE:
            return WizardPageId.LAYOUT
        if cur == WizardPageId.LAYOUT:
            return WizardPageId.BALANCE
        if cur == WizardPageId.BALANCE:
            return WizardPageId.PERCEPTION
        if cur == WizardPageId.PERCEPTION:
            return WizardPageId.ENVELOPE
        if cur == WizardPageId.ENVELOPE:
            return WizardPageId.TILT
        if cur == WizardPageId.TILT:
            return WizardPageId.PREVIEW
        if cur == WizardPageId.PREVIEW:
            return WizardPageId.SAVE
        return -1  # SAVE is the final page

    def done(self, result: int) -> None:
        """Single exit point — handles Finish, Cancel, Esc, and window-close.

        QDialog.reject() internally calls done(Rejected), so overriding only
        done() catches every exit path without firing _silence_and_notify
        twice.
        """
        logger.info(f'calibration wizard done (result={result}) — silencing output')
        self._silence_and_notify()
        super().done(result)

    def _silence_and_notify(self) -> None:
        """Silence the calibration drive and emit wizard_finished.

        wizard_finished triggers mainwindow.signal_stop, which fully tears
        down output_device. This prevents the user's pre-wizard master
        volume from snapping back into effect (potentially painfully) the
        moment the SwitchingAlgorithm is no longer suppressing it.
        """
        try:
            self.adapter.stop_all_output()
        except Exception:
            logger.exception('stop_all_output() raised during wizard exit')
        try:
            self.wizard_finished.emit()
        except Exception:
            logger.exception('wizard_finished signal emit raised')
