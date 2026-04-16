"""
Minimal RemoteGenerationAlgorithm subclass used by the Signal Lab tab to drive
FOC-Stim hardware directly, independent of the main stim pipeline.

Reads envelope parameters from the Signal Lab widget (plain floats updated by
slider signals on the main thread - atomic under the GIL) and reads pulse
parameters live from the main window's pulse settings tab axes. Amplitude is
soft-started from 0 over 1 second on play to avoid hardware startle.

All four electrodes are driven equally at intensity 1.0 - Signal Lab is for
envelope experimentation, not for position steering. Calibration axes are
hardcoded to neutral (center=0, per-electrode gain=1).
"""

import time

import numpy as np

from device.focstim.constants_pb2 import AxisType
from stim_math import limits
from stim_math.amplitude_modulation import SineModulation
from stim_math.audio_gen.base_classes import RemoteGenerationAlgorithm
from stim_math.audio_gen.params import SafetyParamsFOC


# 1-second linear ramp from 0 to slider-value on Play. Hard starts on stim
# hardware are unpleasant; this cost is "1 second of delay" and the benefit
# is "no startle". See feedback_stim_safety memory.
SOFT_START_SECONDS = 1.0

# Nerve time constant used in frequency derating (matches restim's default for
# FOC-Stim mode). See fourphase_algorithm.frequency_derating_factor.
DEFAULT_TAU_MICROSECONDS = 355.0


class SignalLabFOCAlgorithm(RemoteGenerationAlgorithm):
    """
    FOC-Stim 4-phase algorithm driven by SignalLabWidget state.

    Parameters
    ----------
    widget : SignalLabWidget
        Source of envelope parameters. Read as plain float attributes
        (_carrier_freq, _mod_freq, _strength, _rise_fall, _dwell, _volume).
    pulse_axes : tuple
        (axis_pulse_frequency, axis_pulse_width, axis_pulse_rise_time,
         axis_pulse_interval_random) from main_window.tab_pulse_settings.
        Must be AbstractAxis instances supporting .interpolate(t).
    waveform_amplitude_amps : float
        Hard amplitude cap in Amperes for this session. Clamped into
        [limits.WaveformAmpltiudeFOC.min, limits.WaveformAmpltiudeFOC.max]
        before storage.
    """

    def __init__(self, widget, pulse_axes, waveform_amplitude_amps):
        super().__init__()
        self.widget = widget
        (
            self.axis_pulse_frequency,
            self.axis_pulse_width,
            self.axis_pulse_rise_time,
            self.axis_pulse_interval_random,
        ) = pulse_axes

        # Clamp the amplitude argument into the hard safety range before
        # storing - defense in depth against a UI bug or a caller passing
        # an out-of-range value.
        clamped_amps = float(np.clip(
            float(waveform_amplitude_amps),
            limits.WaveformAmpltiudeFOC.min,
            limits.WaveformAmpltiudeFOC.max,
        ))
        self.safety_limits = SafetyParamsFOC(
            minimum_carrier_frequency=float(limits.CarrierFrequencyFOC.min),
            maximum_carrier_frequency=float(limits.CarrierFrequencyFOC.max),
            waveform_amplitude_amps=clamped_amps,
        )

        # Sensor routing hook matching fourphase_algorithm.py. Set by
        # main_window after the device connects. Not used here (Signal Lab
        # does not inspect electrode sensor feedback), but mainwindow's
        # signal_start code path assigns .sensor_node so the attribute must
        # exist to avoid an AttributeError.
        self.sensor_node = None

        # Set by mark_start_time() just before connecting the device, so the
        # soft-start ramp counts from the actual moment Play was pressed.
        self._start_time = None

    def outputs(self) -> int:
        # proto_device.py reads this (not via the ABC) to decide OutputMode.
        return 4

    def mark_start_time(self):
        """Call immediately before start_serial/start_tcp so the 1-second
        soft-start ramp begins at t=0."""
        self._start_time = time.time()

    def parameter_dict(self) -> dict:
        t_now = time.time()
        if self._start_time is None:
            t_elapsed = 0.0
        else:
            t_elapsed = t_now - self._start_time

        # 1-second linear soft-start ramp, clamped into [0, 1]
        ramp = float(np.clip(t_elapsed / SOFT_START_SECONDS, 0.0, 1.0))

        # Snapshot widget state. Single-attribute float assignments are atomic
        # under the GIL, so reading these without a lock is safe.
        carrier_freq = float(self.widget._carrier_freq)
        mod_freq = float(self.widget._mod_freq)
        strength = float(self.widget._strength)
        rise_fall = float(self.widget._rise_fall)
        dwell = float(self.widget._dwell)
        volume_slider = float(self.widget._volume)

        # Clamp carrier frequency to the FOC-Stim safety range. The audio-mode
        # slider allows 100-2000 Hz, but FOC-Stim minimum is 300 Hz.
        carrier_freq = float(np.clip(
            carrier_freq,
            self.safety_limits.minimum_carrier_frequency,
            self.safety_limits.maximum_carrier_frequency,
        ))

        # Envelope value at the current modulation phase.
        if mod_freq > 0:
            mod_phase = (2.0 * np.pi * mod_freq * t_elapsed) % (2.0 * np.pi)
            envelope_value = float(SineModulation(
                np.array([mod_phase]),
                strength,
                rise_fall,
                dwell,
            ).envelope()[0])
        else:
            # DC: constant matching SineModulation's mean output with
            # strength = self._strength (same formula used by signal_lab_widget
            # in _audio_callback when mod_freq == 0).
            envelope_value = 1.0 - strength / 2.0

        # Frequency derating: at higher carrier frequencies, reduce amplitude
        # so the subjective intensity stays roughly constant. Same formula as
        # fourphase_algorithm.frequency_derating_factor.
        tau = DEFAULT_TAU_MICROSECONDS * 1e-6
        max_freq = self.safety_limits.maximum_carrier_frequency
        derating = (carrier_freq * tau + 0.5) / (max_freq * tau + 0.5)
        derating = float(np.clip(derating, 0.0, 1.0))

        # Final amplitude fraction. Every factor is individually clamped
        # into [0, 1] before multiplying by the safety-limited amps ceiling.
        amplitude_fraction = (
            np.clip(envelope_value, 0.0, 1.0)
            * np.clip(volume_slider, 0.0, 1.0)
            * ramp
            * derating
        )
        amplitude_amps = float(amplitude_fraction) * self.safety_limits.waveform_amplitude_amps

        return {
            # All four electrodes equal at full per-channel power (decision #3:
            # "all four equal, intensity 1.0"). The overall amplitude is
            # controlled entirely by AXIS_WAVEFORM_AMPLITUDE_AMPS below.
            AxisType.AXIS_ELECTRODE_1_POWER: 1.0,
            AxisType.AXIS_ELECTRODE_2_POWER: 1.0,
            AxisType.AXIS_ELECTRODE_3_POWER: 1.0,
            AxisType.AXIS_ELECTRODE_4_POWER: 1.0,

            AxisType.AXIS_WAVEFORM_AMPLITUDE_AMPS: amplitude_amps,
            AxisType.AXIS_CARRIER_FREQUENCY_HZ: carrier_freq,

            # Pulse parameters read live from the main window's pulse settings
            # tab (decision #4: "read live from existing pulse settings tab").
            AxisType.AXIS_PULSE_FREQUENCY_HZ: self.axis_pulse_frequency.interpolate(t_now),
            AxisType.AXIS_PULSE_WIDTH_IN_CYCLES: self.axis_pulse_width.interpolate(t_now),
            AxisType.AXIS_PULSE_RISE_TIME_CYCLES: self.axis_pulse_rise_time.interpolate(t_now),
            AxisType.AXIS_PULSE_INTERVAL_RANDOM_PERCENT: self.axis_pulse_interval_random.interpolate(t_now),

            # Calibration axes: neutral values. Signal Lab is not the place
            # to experiment with per-electrode calibration - that belongs in
            # the main calibration UI.
            AxisType.AXIS_CALIBRATION_4_CENTER: 0.0,
            AxisType.AXIS_CALIBRATION_4_A: 1.0,
            AxisType.AXIS_CALIBRATION_4_B: 1.0,
            AxisType.AXIS_CALIBRATION_4_C: 1.0,
            AxisType.AXIS_CALIBRATION_4_D: 1.0,
        }
