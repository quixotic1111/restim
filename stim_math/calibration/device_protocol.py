"""
Device protocol abstraction for the calibration wizard.

The math layer never touches Qt or protobuf directly. Hardware backends
implement CalibrationDeviceProtocol so the wizard can be tested against
a fake device.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol


class ElectrodePair(Enum):
    AB = "AB"
    CD = "CD"
    AC = "AC"
    BD = "BD"
    ALL = "ALL"        # whole-array drive (normal restim signal path)
    # Single-electrode drive for Phase 3 per-electrode isolation tests
    SINGLE_A = "SINGLE_A"
    SINGLE_B = "SINGLE_B"
    SINGLE_C = "SINGLE_C"
    SINGLE_D = "SINGLE_D"


@dataclass(frozen=True)
class SkinResistanceReading:
    """One push from the device, with the active drive context attached."""
    timestamp: float
    Z_a: complex
    Z_b: complex
    Z_c: complex
    Z_d: complex | None      # None for 3-phase devices
    drive_level: float       # 0..1, what we asked for at the moment of capture
    drive_pair: ElectrodePair


@dataclass(frozen=True)
class CurrentReading:
    """One per-electrode RMS current push (amps), with drive context attached.

    Parallel to SkinResistanceReading but for the device's measured output
    current (NotificationCurrents). I_d is None on 3-phase devices."""
    timestamp: float
    I_a: float
    I_b: float
    I_c: float
    I_d: float | None
    drive_level: float
    drive_pair: ElectrodePair


@dataclass(frozen=True)
class CalibrationCapabilities:
    n_electrodes: int        # 3 or 4
    max_safe_drive: float    # absolute device cap, in 0..1
    firmware_version: str


ReadingCallback = Callable[[SkinResistanceReading], None]
CurrentCallback = Callable[[CurrentReading], None]


class CalibrationDeviceProtocol(Protocol):
    """Hardware-backend interface used by the calibration wizard.

    Implementations live alongside each device backend (e.g.
    device/focstim/calibration_adapter.py).
    """

    def subscribe(self, callback: ReadingCallback) -> None:
        """Register a callback for each SkinResistanceReading."""
        ...

    def unsubscribe(self, callback: ReadingCallback) -> None:
        """Remove a previously-registered callback."""
        ...

    def subscribe_current(self, callback: CurrentCallback) -> None:
        """Register a callback for each measured CurrentReading.

        Optional capability — backends without current telemetry need not emit
        anything; subscribers simply receive no readings. The calibration
        wizard's measured-current balance degrades gracefully when silent.
        """
        ...

    def unsubscribe_current(self, callback: CurrentCallback) -> None:
        """Remove a previously-registered current callback."""
        ...

    def is_resistance_stream_active(self) -> bool:
        """True when the device is currently emitting readings."""
        ...

    def set_output_enabled(self, enabled: bool) -> None:
        """Start or stop the signal pipeline."""
        ...

    def set_calibration_waveform(
        self,
        electrode_pair: ElectrodePair,
        level: float,
        duration_ms: int,
    ) -> None:
        """Drive a known waveform on the given pair at the given level.

        The adapter is responsible for hardware-level clamping. Calling
        with a level above the device's safe ceiling silently clamps and
        logs — never raises. Wizard math should not need to know hardware
        limits.
        """
        ...

    def set_calibration_trims(
        self,
        a: float,
        b: float,
        c: float,
        d: float,
    ) -> None:
        """Apply per-electrode calibration trim multipliers (in × units).

        Used by Phase 3 so the user can feel their slider adjustments live
        while running per-electrode tests. Other phases should call
        reset_calibration_trims() instead to ensure 1.0 baselines.
        """
        ...

    def reset_calibration_trims(self) -> None:
        """Restore neutral 1.0× trims on every electrode."""
        ...

    def stop_all_output(self) -> None:
        """Hard kill bypassing the normal output queue. Thread-safe."""
        ...

    def capabilities(self) -> CalibrationCapabilities:
        ...

    def is_connected(self) -> bool:
        ...
