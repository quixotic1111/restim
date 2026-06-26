"""
Calibration mode algorithms for the FOC-stim wizard.

Direct per-electrode amplitude control bypassing the position-math path
of the normal algorithms. The wizard uses these for Phase 1 (impedance
measurement at balanced drive) and Phase 3 (per-electrode isolation).
Phase 4 (perception sweep) uses the user's normal algorithm via the
SwitchingAlgorithm proxy.

Carrier frequency, pulse frequency, etc. are passed in as snapshots from
the user's current settings — we don't change those during calibration
so the felt signal matches normal use.
"""

from __future__ import annotations

import numpy as np

from device.focstim.constants_pb2 import AxisType
from stim_math.audio_gen.base_classes import RemoteGenerationAlgorithm
from stim_math.axis import AbstractMediaSync


class CalibrationFourphaseAlgorithm(RemoteGenerationAlgorithm):
    """Wizard-driven algorithm with direct per-electrode amplitude control."""

    def __init__(
        self,
        media: AbstractMediaSync,
        max_amplitude_amps: float,
        carrier_frequency_hz: float,
        pulse_frequency_hz: float,
        pulse_width_cycles: float,
        pulse_rise_time_cycles: float,
    ):
        super().__init__()
        self.media = media
        self.max_amplitude_amps = max_amplitude_amps
        self.carrier_frequency_hz = carrier_frequency_hz
        self.pulse_frequency_hz = pulse_frequency_hz
        self.pulse_width_cycles = pulse_width_cycles
        self.pulse_rise_time_cycles = pulse_rise_time_cycles

        # Wizard-controlled state. Defaults to silent with neutral trims.
        self._a = 0.0
        self._b = 0.0
        self._c = 0.0
        self._d = 0.0
        self._volume = 0.0
        self._trim_a = 1.0
        self._trim_b = 1.0
        self._trim_c = 1.0
        self._trim_d = 1.0

    def outputs(self) -> int:
        return 4

    def set_drive(
        self,
        a: float,
        b: float,
        c: float,
        d: float,
        volume: float,
    ) -> None:
        """Wizard's primary control. Values clamped to [0, 1] at the
        boundary; max_amplitude_amps is the absolute final cap."""
        self._a = float(np.clip(a, 0.0, 1.0))
        self._b = float(np.clip(b, 0.0, 1.0))
        self._c = float(np.clip(c, 0.0, 1.0))
        self._d = float(np.clip(d, 0.0, 1.0))
        self._volume = float(np.clip(volume, 0.0, 1.0))

    def set_trims(
        self,
        a: float,
        b: float,
        c: float,
        d: float,
    ) -> None:
        """Per-electrode calibration trim multipliers.

        Used by Phase 3 so the user can feel the effect of their slider
        adjustments while testing. Defaults to 1.0 (no effect) and other
        phases leave them at default so measurement isn't biased by trims.
        """
        self._trim_a = float(a)
        self._trim_b = float(b)
        self._trim_c = float(c)
        self._trim_d = float(d)

    def reset_trims(self) -> None:
        """Restore neutral trims (1.0× each). Phases that measure rather
        than adjust should call this on entry."""
        self._trim_a = 1.0
        self._trim_b = 1.0
        self._trim_c = 1.0
        self._trim_d = 1.0

    def silence(self) -> None:
        """Emergency stop — drive zero everywhere. Leaves trims untouched
        so a subsequent set_drive picks them back up unchanged."""
        self.set_drive(0.0, 0.0, 0.0, 0.0, 0.0)

    def parameter_dict(self) -> dict:
        # NOTE: no media.is_playing() gate here — the calibration wizard
        # manages drive lifecycle explicitly via set_drive() / silence(), and
        # gating on media state would force amplitude to 0 whenever a video
        # isn't actively playing in VLC/MPV/etc. The wizard's own cancel/exit
        # paths always silence the device, so this is safe.
        return {
            AxisType.AXIS_ELECTRODE_1_POWER: self._a,
            AxisType.AXIS_ELECTRODE_2_POWER: self._b,
            AxisType.AXIS_ELECTRODE_3_POWER: self._c,
            AxisType.AXIS_ELECTRODE_4_POWER: self._d,
            AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS: self._volume * self.max_amplitude_amps,
            AxisType.AXIS_CARRIER_FREQUENCY_HZ: self.carrier_frequency_hz,
            AxisType.AXIS_PULSE_FREQUENCY_HZ: self.pulse_frequency_hz,
            AxisType.AXIS_PULSE_WIDTH_IN_CYCLES: self.pulse_width_cycles,
            AxisType.AXIS_PULSE_RISE_TIME_CYCLES: self.pulse_rise_time_cycles,
            AxisType.AXIS_PULSE_INTERVAL_RANDOM_PERCENT: 0.0,  # deterministic
            # Calibration trims are wizard-controlled. Default 1.0 (no effect)
            # so measurement phases don't bias their readings; Phase 3 sets
            # these to slider values so the user feels their adjustments live.
            AxisType.AXIS_CALIBRATION_4_A: self._trim_a,
            AxisType.AXIS_CALIBRATION_4_B: self._trim_b,
            AxisType.AXIS_CALIBRATION_4_C: self._trim_c,
            AxisType.AXIS_CALIBRATION_4_D: self._trim_d,
            AxisType.AXIS_CALIBRATION_4_REDUCTION_IN_CENTER: 0.0,
        }


class CalibrationThreephaseAlgorithm(RemoteGenerationAlgorithm):
    """3-phase calibration: balanced drive via (alpha=0, beta=0).

    Per-electrode isolation for 3-phase devices requires geometric position
    targeting (driving alpha/beta to where each electrode lives in the
    threephase coordinate system). Not implemented for v1 — the 3-phase
    wizard can still do Phase 1 (impedance) and Phase 4 (perception), but
    Phase 3 (per-electrode balance) is skipped.
    """

    def __init__(
        self,
        media: AbstractMediaSync,
        max_amplitude_amps: float,
        carrier_frequency_hz: float,
        pulse_frequency_hz: float,
        pulse_width_cycles: float,
        pulse_rise_time_cycles: float,
    ):
        super().__init__()
        self.media = media
        self.max_amplitude_amps = max_amplitude_amps
        self.carrier_frequency_hz = carrier_frequency_hz
        self.pulse_frequency_hz = pulse_frequency_hz
        self.pulse_width_cycles = pulse_width_cycles
        self.pulse_rise_time_cycles = pulse_rise_time_cycles
        self._volume = 0.0

    def outputs(self) -> int:
        return 3

    def set_volume(self, volume: float) -> None:
        """3-phase calibration only supports overall volume; drives to a
        balanced position. set_drive() with per-electrode values raises."""
        self._volume = float(np.clip(volume, 0.0, 1.0))

    def silence(self) -> None:
        self._volume = 0.0

    def parameter_dict(self) -> dict:
        # See CalibrationFourphaseAlgorithm.parameter_dict — same rationale
        # for skipping the media-playback gate.
        return {
            AxisType.AXIS_POSITION_ALPHA: 0.0,
            AxisType.AXIS_POSITION_BETA: 0.0,
            AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS: self._volume * self.max_amplitude_amps,
            AxisType.AXIS_CARRIER_FREQUENCY_HZ: self.carrier_frequency_hz,
            AxisType.AXIS_PULSE_FREQUENCY_HZ: self.pulse_frequency_hz,
            AxisType.AXIS_PULSE_WIDTH_IN_CYCLES: self.pulse_width_cycles,
            AxisType.AXIS_PULSE_RISE_TIME_CYCLES: self.pulse_rise_time_cycles,
            AxisType.AXIS_PULSE_INTERVAL_RANDOM_PERCENT: 0.0,
            AxisType.AXIS_CALIBRATION_3_CENTER: 1.0,
            AxisType.AXIS_CALIBRATION_3_UP: 1.0,
            AxisType.AXIS_CALIBRATION_3_LEFT: 1.0,
        }
