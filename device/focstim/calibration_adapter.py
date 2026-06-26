"""
FOCStimCalibrationAdapter: bridge between the calibration math layer and
the FOC-stim device backend.

Implements stim_math.calibration.CalibrationDeviceProtocol by wiring:
- the device's new_resistance_data Qt signal → SkinResistanceReading callbacks
- set_calibration_waveform() → SwitchingAlgorithm mode flip + drive update
- stop_all_output() → algorithm silence + revert to user mode

The adapter does not own the device or the algorithm — both are created
elsewhere (typically at app startup) and handed in. The adapter is created
when a wizard runs and torn down when it ends.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject

from device.focstim.calibration_algorithm import CalibrationFourphaseAlgorithm
from device.focstim.proto_device import FOCStimProtoDevice
from stim_math.audio_gen.switching_algorithm import SwitchingAlgorithm
from stim_math.calibration.device_protocol import (
    CalibrationCapabilities,
    CurrentCallback,
    CurrentReading,
    ElectrodePair,
    ReadingCallback,
    SkinResistanceReading,
)

logger = logging.getLogger('restim.calibration.adapter')

# Drive vectors per ElectrodePair as (a, b, c, d) tuples. Magnitudes here
# distribute current across the named electrodes when multiplied through
# AXIS_ELECTRODE_N_POWER. The wizard supplies the level (volume) separately.
_PAIR_TO_DRIVES = {
    ElectrodePair.AB:       (0.5,  0.5,  0.0,  0.0),
    ElectrodePair.CD:       (0.0,  0.0,  0.5,  0.5),
    ElectrodePair.AC:       (0.5,  0.0,  0.5,  0.0),
    ElectrodePair.BD:       (0.0,  0.5,  0.0,  0.5),
    ElectrodePair.ALL:      (0.25, 0.25, 0.25, 0.25),
    # Phase 3 per-electrode isolation: drive one electrode at full vector,
    # others at zero. The volume passed alongside still scales it.
    ElectrodePair.SINGLE_A: (1.0,  0.0,  0.0,  0.0),
    ElectrodePair.SINGLE_B: (0.0,  1.0,  0.0,  0.0),
    ElectrodePair.SINGLE_C: (0.0,  0.0,  1.0,  0.0),
    ElectrodePair.SINGLE_D: (0.0,  0.0,  0.0,  1.0),
}

# Cadence is ~2 Hz (510 ms median per empirical Test 1); treat the stream
# as inactive if no reading has arrived in this many seconds.
_STREAM_TIMEOUT_SEC = 2.0


class FOCStimCalibrationAdapter(QObject):
    """Implements CalibrationDeviceProtocol for FOC-stim hardware."""

    def __init__(
        self,
        device: FOCStimProtoDevice,
        switching_algorithm: SwitchingAlgorithm,
        firmware_version: str = "",
        max_safe_drive: float = 1.0,
    ):
        super().__init__()
        # The calibration child of the SwitchingAlgorithm must be a
        # CalibrationFourphaseAlgorithm — that's the only kind this adapter
        # knows how to drive.
        cal = switching_algorithm.calibration_algorithm
        if not isinstance(cal, CalibrationFourphaseAlgorithm):
            raise TypeError(
                f"adapter requires CalibrationFourphaseAlgorithm; "
                f"got {type(cal).__name__}"
            )

        self.device = device
        self.switching_algorithm = switching_algorithm
        self._calibration_algo: CalibrationFourphaseAlgorithm = cal
        self._firmware_version = firmware_version
        self._max_safe_drive = float(max_safe_drive)

        self._callbacks: list[ReadingCallback] = []
        self._current_callbacks: list[CurrentCallback] = []
        self._last_reading_time: float = 0.0
        self._current_drive_level: float = 0.0
        self._current_drive_pair: ElectrodePair = ElectrodePair.ALL

        self.device.new_resistance_data.connect(self._on_resistance_data)
        self.device.new_current_data.connect(self._on_current_data)

    # === CalibrationDeviceProtocol implementation ===

    def subscribe(self, callback: ReadingCallback) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def unsubscribe(self, callback: ReadingCallback) -> None:
        try:
            self._callbacks.remove(callback)
        except ValueError:
            pass

    def subscribe_current(self, callback: CurrentCallback) -> None:
        if callback not in self._current_callbacks:
            self._current_callbacks.append(callback)

    def unsubscribe_current(self, callback: CurrentCallback) -> None:
        try:
            self._current_callbacks.remove(callback)
        except ValueError:
            pass

    def is_resistance_stream_active(self) -> bool:
        return (time.time() - self._last_reading_time) < _STREAM_TIMEOUT_SEC

    def set_output_enabled(self, enabled: bool) -> None:
        """Enable/disable calibration drive.

        FOC-stim's signal pipeline runs continuously based on media-playback
        gating and parameter dicts; we control output by setting the
        calibration drives to zero (disabled) or letting the most recent
        set_calibration_waveform values flow (enabled). Mode switching
        happens via SwitchingAlgorithm.
        """
        if enabled:
            self.switching_algorithm.set_mode(SwitchingAlgorithm.MODE_CALIBRATION)
        else:
            self._calibration_algo.silence()
            # Stay in calibration mode but with zero drive — the wizard often
            # toggles output on/off between phases without wanting to revert
            # to user mode. Explicit revert happens via stop_all_output().

    def set_calibration_waveform(
        self,
        electrode_pair: ElectrodePair,
        level: float,
        duration_ms: int,
    ) -> None:
        """Drive a known waveform on the named pair at the given level.

        duration_ms is informational only — the caller manages timing and
        will either call this method again with new values or call
        stop_all_output() when done. Putting time management in the wizard
        rather than this adapter keeps phase durations visible at the
        wizard layer.
        """
        if not self.is_connected():
            logger.warning("set_calibration_waveform: device not connected")
            return

        clamped = max(0.0, min(self._max_safe_drive, float(level)))
        if clamped != level:
            logger.info(
                f"calibration level {level} clamped to {clamped} (device cap)"
            )

        drives = _PAIR_TO_DRIVES.get(electrode_pair)
        if drives is None:
            logger.error(f"unknown electrode pair {electrode_pair!r}")
            return

        a, b, c, d = drives
        self._calibration_algo.set_drive(a, b, c, d, clamped)
        self.switching_algorithm.set_mode(SwitchingAlgorithm.MODE_CALIBRATION)

        self._current_drive_level = clamped
        self._current_drive_pair = electrode_pair

    def set_calibration_trims(
        self,
        a: float,
        b: float,
        c: float,
        d: float,
    ) -> None:
        """Forward per-electrode trim values to the calibration algorithm."""
        try:
            self._calibration_algo.set_trims(a, b, c, d)
        except Exception:
            logger.exception("set_trims raised")

    def reset_calibration_trims(self) -> None:
        """Reset calibration algorithm trims to 1.0 (neutral)."""
        try:
            self._calibration_algo.reset_trims()
        except Exception:
            logger.exception("reset_trims raised")

    def stop_all_output(self) -> None:
        """Hard kill: silence the calibration drive.

        Critically does NOT revert SwitchingAlgorithm to MODE_USER. Reverting
        would immediately expose the user to whatever amplitude their pre-
        wizard master volume produces, which can be abruptly painful at
        100% master + 100% hardware knob. The wizard is responsible for
        signaling mainwindow to fully stop playback on exit so the user has
        to press Play again to resume — guaranteeing a deliberate ramp-up.
        """
        try:
            self._calibration_algo.silence()
        except Exception:
            logger.exception("calibration silence() raised")
        self._current_drive_level = 0.0

    def capabilities(self) -> CalibrationCapabilities:
        return CalibrationCapabilities(
            n_electrodes=4,
            max_safe_drive=self._max_safe_drive,
            firmware_version=self._firmware_version,
        )

    def is_connected(self) -> bool:
        return bool(self.device.is_connected_and_running())

    # === Internals ===

    def _on_resistance_data(
        self,
        a: complex,
        b: complex,
        c: complex,
        d: complex,
    ) -> None:
        """Qt slot for device.new_resistance_data. Forwards as a
        SkinResistanceReading to each registered subscriber."""
        reading = SkinResistanceReading(
            timestamp=time.time(),
            Z_a=a,
            Z_b=b,
            Z_c=c,
            Z_d=d,
            drive_level=self._current_drive_level,
            drive_pair=self._current_drive_pair,
        )
        self._last_reading_time = reading.timestamp
        for cb in list(self._callbacks):
            try:
                cb(reading)
            except Exception:
                logger.exception("subscriber callback raised")

    def _on_current_data(
        self,
        a: float,
        b: float,
        c: float,
        d: float,
    ) -> None:
        """Qt slot for device.new_current_data. Forwards measured per-electrode
        RMS current (amps) as a CurrentReading to each registered subscriber,
        tagged with the drive context active at capture."""
        reading = CurrentReading(
            timestamp=time.time(),
            I_a=a,
            I_b=b,
            I_c=c,
            I_d=d,
            drive_level=self._current_drive_level,
            drive_pair=self._current_drive_pair,
        )
        for cb in list(self._current_callbacks):
            try:
                cb(reading)
            except Exception:
                logger.exception("current subscriber callback raised")
