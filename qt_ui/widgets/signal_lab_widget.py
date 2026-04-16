"""
Signal Lab tab: envelope-shaping experimentation tool with two output modes.

Audio mode (default): plays a carrier sine wave multiplied by an amplitude-
modulation envelope through the system's default audio output. Independent of
the main stim pipeline; safe for experimentation without hardware.

FOC-Stim mode: drives a connected FOC-Stim 4-phase device with the same
envelope math, via a SignalLabFOCAlgorithm injected into the device's normal
transport. Refuses to start if the main pipeline is already running; requires
the user to have FOC-Stim 4-phase selected as their device type. 1-second
amplitude soft-start on Play. All four electrodes driven equally.

Reuses stim_math.amplitude_modulation.SineModulation for the envelope math and
stim_math.sine_generator.AngleGenerator for phase-continuous carrier/modulation
generation across audio buffer boundaries.
"""

import logging

import numpy as np
import pyqtgraph as pg
import sounddevice as sd
from PySide6 import QtCore
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from device.focstim.signal_lab_algorithm import SignalLabFOCAlgorithm
from stim_math import limits
from stim_math.amplitude_modulation import SineModulation
from stim_math.sine_generator import AngleGenerator

logger = logging.getLogger('restim.signal_lab')


# Slider ranges. Carrier range is audio-friendly (well below Nyquist for any
# standard sample rate). Modulation range is clamped to the restim limit.
CARRIER_MIN = 100.0
CARRIER_MAX = 2000.0
CARRIER_DEFAULT = 800.0

MOD_MIN = 0.0
MOD_MAX = float(limits.ModulationFrequency.max)
MOD_DEFAULT = 5.0

AUDIO_BLOCKSIZE = 256
FALLBACK_SAMPLERATE = 48000


class LabeledSlider(QWidget):
    """A horizontal slider + double spinbox pair, linked bidirectionally."""

    valueChanged = QtCore.Signal(float)

    def __init__(self, label, minimum, maximum, default, decimals=2, tooltip=None, parent=None):
        super().__init__(parent)
        self._scale = 10 ** decimals

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._label = QLabel(label)
        self._label.setMinimumWidth(140)
        if tooltip:
            self._label.setToolTip(tooltip)
        layout.addWidget(self._label)

        self._slider = QSlider(Qt.Horizontal)
        self._slider.setMinimum(int(round(minimum * self._scale)))
        self._slider.setMaximum(int(round(maximum * self._scale)))
        self._slider.setValue(int(round(default * self._scale)))
        if tooltip:
            self._slider.setToolTip(tooltip)
        layout.addWidget(self._slider, stretch=1)

        self._spin = QDoubleSpinBox()
        self._spin.setDecimals(decimals)
        self._spin.setRange(minimum, maximum)
        self._spin.setValue(default)
        self._spin.setSingleStep(10 ** (-decimals))
        self._spin.setMinimumWidth(90)
        if tooltip:
            self._spin.setToolTip(tooltip)
        layout.addWidget(self._spin)

        self._value = float(default)
        self._slider.valueChanged.connect(self._on_slider_changed)
        self._spin.valueChanged.connect(self._on_spin_changed)

    @Slot(int)
    def _on_slider_changed(self, int_value):
        value = int_value / self._scale
        if abs(value - self._spin.value()) > 1e-9:
            self._spin.blockSignals(True)
            self._spin.setValue(value)
            self._spin.blockSignals(False)
        self._value = value
        self.valueChanged.emit(value)

    @Slot(float)
    def _on_spin_changed(self, value):
        int_value = int(round(value * self._scale))
        if int_value != self._slider.value():
            self._slider.blockSignals(True)
            self._slider.setValue(int_value)
            self._slider.blockSignals(False)
        self._value = float(value)
        self.valueChanged.emit(self._value)

    def value(self):
        return self._value


MODE_AUDIO = 'audio'
MODE_FOCSTIM = 'focstim'

# Default amplitude limit for FOC-Stim mode - deliberately conservative.
# The absolute safety ceiling is limits.WaveformAmpltiudeFOC.max (0.15 A).
FOC_AMPLITUDE_DEFAULT = 0.05


class SignalLabWidget(QWidget):
    """
    Signal Lab tab widget with two output modes (audio / FOC-Stim).

    Audio mode owns a sounddevice OutputStream. FOC-Stim mode delegates to
    main_window.signal_start_signallab() with a SignalLabFOCAlgorithm that
    reads this widget's slider state. Only one mode can be playing at a time;
    switching modes is blocked while playing.

    The audio callback reads plain-float parameter snapshots from self (updated
    by slider signals on the main thread) so there is no direct Qt widget access
    from the audio thread. The FOC-Stim algorithm reads the same attributes.
    """

    def __init__(self, main_window=None, parent=None):
        super().__init__(parent)

        # Reference to the containing MainWindow. Used in FOC-Stim mode to
        # check main-pipeline playstate, look up pulse-settings axes, and
        # drive the device lifecycle. May be None in offscreen smoke tests
        # that instantiate the widget directly.
        self._main_window = main_window

        # Parameter snapshot read by the audio callback and the FOC-Stim
        # algorithm. CPython single-attribute float assignments are atomic
        # under the GIL, so no lock is needed.
        self._carrier_freq = CARRIER_DEFAULT
        self._mod_freq = MOD_DEFAULT
        self._strength = 0.5
        self._rise_fall = 0.0
        self._dwell = 0.0
        self._volume = 0.2
        self._foc_amplitude_limit = FOC_AMPLITUDE_DEFAULT

        self._mode = MODE_AUDIO
        self._active_mode = None  # which mode is currently playing (None if stopped)

        self._stream = None
        self._samplerate = FALLBACK_SAMPLERATE
        self._carrier_gen = AngleGenerator()
        self._mod_gen = AngleGenerator()

        self._build_ui()
        self._update_notice()
        self._update_plots()

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._stop_audio)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QVBoxLayout(self)

        self._notice = QLabel()
        self._notice.setWordWrap(True)
        self._notice.setStyleSheet("QLabel { color: gray; font-style: italic; }")
        root.addWidget(self._notice)

        # Output mode selector. Radio buttons so only one is active; grouped
        # so mutual exclusion is automatic. Disabled while Play is active.
        mode_group = QGroupBox("Output mode")
        mode_layout = QHBoxLayout(mode_group)
        self._mode_audio_radio = QRadioButton("Audio (computer speakers)")
        self._mode_audio_radio.setChecked(True)
        self._mode_audio_radio.setToolTip(
            "Play the signal through your computer's default audio output.\n"
            "Safe for experimentation without hardware. Does NOT drive any stim device."
        )
        self._mode_focstim_radio = QRadioButton("FOC-Stim (hardware)")
        self._mode_focstim_radio.setToolTip(
            "Drive a connected FOC-Stim 4-phase device.\n"
            "Requires: main playback stopped, FOC-Stim 4-phase device selected.\n"
            "Starts at amplitude 0 and ramps up over 1 second on Play.\n"
            "All four electrodes are driven equally at full per-channel power.\n"
            "Pulse parameters are read live from the Pulse Settings tab."
        )
        self._mode_button_group = QButtonGroup(self)
        self._mode_button_group.addButton(self._mode_audio_radio)
        self._mode_button_group.addButton(self._mode_focstim_radio)
        self._mode_audio_radio.toggled.connect(self._on_mode_changed)
        mode_layout.addWidget(self._mode_audio_radio)
        mode_layout.addWidget(self._mode_focstim_radio)
        mode_layout.addStretch(1)
        root.addWidget(mode_group)

        controls_group = QGroupBox("Signal parameters")
        controls_layout = QVBoxLayout(controls_group)

        self.carrier_slider = LabeledSlider(
            "Carrier (Hz)", CARRIER_MIN, CARRIER_MAX, CARRIER_DEFAULT,
            decimals=0,
            tooltip="Frequency of the sine carrier wave.",
        )
        self.mod_slider = LabeledSlider(
            "Modulation (Hz)", MOD_MIN, MOD_MAX, MOD_DEFAULT,
            decimals=1,
            tooltip="Frequency of the amplitude-modulation envelope.",
        )
        self.strength_slider = LabeledSlider(
            "Depth", 0.0, 1.0, 0.5,
            decimals=2,
            tooltip="How deep the modulation envelope dips. 0 = no modulation, 1 = full on/off.",
        )
        self.rise_fall_slider = LabeledSlider(
            "Rise / Fall bias", -1.0, 1.0, 0.0,
            decimals=2,
            tooltip=(
                "Envelope rise vs. fall time asymmetry.\n"
                "Negative: slow rise, sharp fall.\n"
                "Zero: symmetric.\n"
                "Positive: sharp rise, slow fall.\n"
                "(Note: restim internally calls this 'left_right_bias' but it has "
                "nothing to do with stereo channels - it is a sawtooth/attack-release knob.)"
            ),
        )
        self.dwell_slider = LabeledSlider(
            "High / Low dwell", -1.0, 1.0, 0.0,
            decimals=2,
            tooltip=(
                "Dwell time at peak vs. trough of the envelope.\n"
                "Negative: mostly at trough with brief peaks (pulse-like).\n"
                "Zero: no flat sections (pure sinusoidal transitions).\n"
                "Positive: mostly at peak with brief dips."
            ),
        )
        self.volume_slider = LabeledSlider(
            "Volume", 0.0, 1.0, 0.2,
            decimals=2,
            tooltip="Master output volume (fraction of maximum). Start low.",
        )

        for slider, attr in [
            (self.carrier_slider, '_carrier_freq'),
            (self.mod_slider, '_mod_freq'),
            (self.strength_slider, '_strength'),
            (self.rise_fall_slider, '_rise_fall'),
            (self.dwell_slider, '_dwell'),
            (self.volume_slider, '_volume'),
        ]:
            slider.valueChanged.connect(self._make_setter(attr))
            slider.valueChanged.connect(self._update_plots)
            controls_layout.addWidget(slider)

        root.addWidget(controls_group)

        # FOC-Stim specific controls. Only enabled when FOC-Stim mode is
        # selected; hidden in a separate group box to keep the UI hierarchy
        # readable and to make it obvious what belongs to which mode.
        self._foc_group = QGroupBox("FOC-Stim output limit")
        foc_layout = QVBoxLayout(self._foc_group)
        self.foc_amplitude_slider = LabeledSlider(
            "Amplitude limit (A)",
            float(limits.WaveformAmpltiudeFOC.min),
            float(limits.WaveformAmpltiudeFOC.max),
            FOC_AMPLITUDE_DEFAULT,
            decimals=3,
            tooltip=(
                "Hard upper limit on FOC-Stim output current in Amperes.\n"
                "The Volume slider above scales the envelope as a fraction of this limit.\n"
                "Start low. The absolute safety ceiling is "
                f"{limits.WaveformAmpltiudeFOC.max} A; Signal Lab's default is "
                f"{FOC_AMPLITUDE_DEFAULT} A."
            ),
        )
        self.foc_amplitude_slider.valueChanged.connect(self._make_setter('_foc_amplitude_limit'))
        foc_layout.addWidget(self.foc_amplitude_slider)
        self._foc_group.setEnabled(False)  # disabled until FOC-Stim mode is selected
        root.addWidget(self._foc_group)

        # Play button row
        button_row = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.setCheckable(True)
        self.play_button.toggled.connect(self._on_play_toggled)
        button_row.addWidget(self.play_button)

        self.status_label = QLabel("Stopped")
        button_row.addWidget(self.status_label)
        button_row.addStretch(1)
        root.addLayout(button_row)

        # Plots
        plots_group = QGroupBox("Visualization")
        plots_layout = QVBoxLayout(plots_group)

        self.envelope_plot = pg.PlotWidget(title="Envelope shape (one modulation period)")
        self.envelope_plot.setLabel('left', 'Amplitude')
        self.envelope_plot.setLabel('bottom', 'Phase (rad)')
        self.envelope_plot.setYRange(0, 1.05)
        self.envelope_plot.showGrid(x=True, y=True, alpha=0.3)
        self.envelope_curve = self.envelope_plot.plot(pen=pg.mkPen('c', width=2))
        plots_layout.addWidget(self.envelope_plot)

        self.waveform_plot = pg.PlotWidget(title="Audio waveform (20 ms slice)")
        self.waveform_plot.setLabel('left', 'Amplitude')
        self.waveform_plot.setLabel('bottom', 'Time (s)')
        self.waveform_plot.setYRange(-1.05, 1.05)
        self.waveform_plot.showGrid(x=True, y=True, alpha=0.3)
        self.waveform_curve = self.waveform_plot.plot(pen=pg.mkPen('y', width=1))
        plots_layout.addWidget(self.waveform_plot)

        root.addWidget(plots_group, stretch=1)

    def _make_setter(self, attr):
        def setter(value):
            setattr(self, attr, float(value))
        return setter

    # --------------------------------------------------------------- Math

    def _compute_envelope(self, theta):
        return SineModulation(
            theta,
            self._strength,
            self._rise_fall,
            self._dwell,
        ).envelope()

    @Slot()
    def _update_plots(self):
        # Envelope shape: one full modulation period
        theta = np.linspace(0, 2 * np.pi, 500, endpoint=True)
        envelope = self._compute_envelope(theta)
        self.envelope_curve.setData(theta, envelope)

        # 20 ms audio preview slice
        slice_duration = 0.020
        n_samples = max(16, int(self._samplerate * slice_duration))
        t = np.linspace(0, slice_duration, n_samples, endpoint=False)
        carrier_theta = 2 * np.pi * self._carrier_freq * t
        mod_theta = 2 * np.pi * self._mod_freq * t
        carrier = np.sin(carrier_theta)
        envelope = self._compute_envelope(mod_theta)
        signal = (carrier * envelope * self._volume).astype(np.float32)
        self.waveform_curve.setData(t, signal)

    # ---------------------------------------------------------- Audio I/O

    def _audio_callback(self, outdata, frames, time_info, status):
        if status:
            logger.warning("sounddevice status: %s", status)

        carrier_freq = self._carrier_freq
        mod_freq = self._mod_freq
        volume = self._volume

        carrier_theta = self._carrier_gen.generate(frames, carrier_freq, self._samplerate)
        carrier = np.sin(carrier_theta).astype(np.float32)

        if mod_freq > 0:
            mod_theta = self._mod_gen.generate(frames, mod_freq, self._samplerate)
            envelope = self._compute_envelope(mod_theta).astype(np.float32)
        else:
            # DC modulation: constant (1 - depth/2) to match SineModulation's scaling
            envelope = np.full(frames, 1.0 - self._strength / 2, dtype=np.float32)

        signal = carrier * envelope * volume

        outdata[:, 0] = signal
        if outdata.shape[1] > 1:
            outdata[:, 1] = signal

    @Slot(bool)
    def _on_play_toggled(self, checked):
        if checked:
            if self._mode == MODE_AUDIO:
                self._start_audio()
            else:
                self._start_focstim()
        else:
            # Stop whichever mode is currently active. Using _active_mode
            # (rather than _mode) protects against the user switching the
            # radio button after stop is requested but before the handler runs.
            if self._active_mode == MODE_FOCSTIM:
                self._stop_focstim()
            else:
                self._stop_audio()

    @Slot(bool)
    def _on_mode_changed(self, audio_checked):
        """Radio button toggled. audio_checked=True when Audio is selected."""
        # Should never fire while playing (we disable the radios), but guard
        # anyway so the widget can never get into a mode-mismatched state.
        if self._active_mode is not None:
            return
        self._mode = MODE_AUDIO if audio_checked else MODE_FOCSTIM
        self._foc_group.setEnabled(self._mode == MODE_FOCSTIM)
        self._update_notice()

    def _update_notice(self):
        if self._mode == MODE_AUDIO:
            self._notice.setText(
                "Audio mode: plays through your computer's default audio output. "
                "Does NOT drive connected stim hardware. Safe for experimentation."
            )
        else:
            self._notice.setText(
                "FOC-Stim mode: drives a connected FOC-Stim 4-phase device. "
                "Main playback must be stopped. All four electrodes are driven "
                "equally; amplitude soft-starts over 1 second on Play. "
                "Start with a low Amplitude limit and low Volume."
            )

    def _set_mode_controls_enabled(self, enabled):
        """Enable or disable the mode radio buttons. Called to lock mode
        switching while Play is active."""
        self._mode_audio_radio.setEnabled(enabled)
        self._mode_focstim_radio.setEnabled(enabled)

    def _start_audio(self):
        try:
            default_output = sd.query_devices(kind='output')
            self._samplerate = int(default_output['default_samplerate'])
        except Exception:
            logger.exception("could not query default output device, using fallback samplerate")
            self._samplerate = FALLBACK_SAMPLERATE

        self._carrier_gen = AngleGenerator()
        self._mod_gen = AngleGenerator()

        try:
            self._stream = sd.OutputStream(
                samplerate=self._samplerate,
                channels=2,
                dtype='float32',
                callback=self._audio_callback,
                blocksize=AUDIO_BLOCKSIZE,
            )
            self._stream.start()
        except Exception as exc:
            logger.exception("failed to open audio output stream")
            self._stream = None
            self.play_button.blockSignals(True)
            self.play_button.setChecked(False)
            self.play_button.blockSignals(False)
            self.play_button.setText("Play")
            self.status_label.setText(f"Error: {exc}")
            return

        self._active_mode = MODE_AUDIO
        self._set_mode_controls_enabled(False)
        self.play_button.setText("Stop")
        self.status_label.setText(f"Playing (audio) @ {self._samplerate} Hz")
        self._update_plots()

    @Slot()
    def _stop_audio(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                logger.exception("error stopping audio stream")
            self._stream = None

        if self.play_button.isChecked():
            self.play_button.blockSignals(True)
            self.play_button.setChecked(False)
            self.play_button.blockSignals(False)
        self.play_button.setText("Play")
        self.status_label.setText("Stopped")
        if self._active_mode == MODE_AUDIO:
            self._active_mode = None
            self._set_mode_controls_enabled(True)

    # ------------------------------------------------------------- FOC-Stim

    def _start_focstim(self):
        """Begin driving the connected FOC-Stim 4-phase device with the Signal
        Lab envelope. Validates main-pipeline state and device type, shows a
        QMessageBox on any failure, and leaves the widget in the stopped state
        if anything goes wrong."""
        if self._main_window is None:
            self._focstim_error("Signal Lab was constructed without a main-window reference; "
                                "FOC-Stim mode is not available in this context.")
            return

        pulse_tab = getattr(self._main_window, 'tab_pulse_settings', None)
        if pulse_tab is None:
            self._focstim_error("Could not find the Pulse Settings tab to read pulse parameters from.")
            return

        try:
            pulse_axes = (
                pulse_tab.axis_pulse_frequency,
                pulse_tab.axis_pulse_width,
                pulse_tab.axis_pulse_rise_time,
                pulse_tab.axis_pulse_interval_random,
            )
        except AttributeError as exc:
            self._focstim_error(f"Pulse Settings tab is missing expected axis attributes: {exc}")
            return

        algorithm = SignalLabFOCAlgorithm(
            widget=self,
            pulse_axes=pulse_axes,
            waveform_amplitude_amps=self._foc_amplitude_limit,
        )

        try:
            ok, message = self._main_window.signal_start_signallab(algorithm)
        except Exception as exc:
            logger.exception("signal_start_signallab raised")
            self._focstim_error(f"Unexpected error starting FOC-Stim output: {exc}")
            return

        if not ok:
            self._focstim_error(message or "Failed to start FOC-Stim output.")
            return

        self._active_mode = MODE_FOCSTIM
        self._set_mode_controls_enabled(False)
        self.play_button.setText("Stop")
        self.status_label.setText(
            f"Playing (FOC-Stim, limit {self._foc_amplitude_limit:.3f} A, 1s soft-start)"
        )

    def _stop_focstim(self):
        """Stop the FOC-Stim device if we started it. Safe to call more than
        once or when nothing is running."""
        # Delegate the device teardown to main_window.signal_stop - same path
        # the main Start/Stop toolbar button uses. signal_stop is idempotent
        # for our purposes because it checks self.output_device for None.
        if self._main_window is not None and self._active_mode == MODE_FOCSTIM:
            try:
                from qt_ui.mainwindow import PlayState
                self._main_window.signal_stop(PlayState.STOPPED)
            except Exception:
                logger.exception("error stopping FOC-Stim device from Signal Lab")

        if self.play_button.isChecked():
            self.play_button.blockSignals(True)
            self.play_button.setChecked(False)
            self.play_button.blockSignals(False)
        self.play_button.setText("Play")
        self.status_label.setText("Stopped")
        if self._active_mode == MODE_FOCSTIM:
            self._active_mode = None
            self._set_mode_controls_enabled(True)

    def _focstim_error(self, message):
        """Show a modal error, reset the Play button, and do not leave
        Signal Lab in a half-started state."""
        logger.warning("Signal Lab FOC-Stim error: %s", message)
        if self.play_button.isChecked():
            self.play_button.blockSignals(True)
            self.play_button.setChecked(False)
            self.play_button.blockSignals(False)
        self.play_button.setText("Play")
        self.status_label.setText("Stopped (error)")
        QMessageBox.warning(self, "Signal Lab - FOC-Stim mode", message)

    def set_external_play_state(self, playstate):
        """Called by MainWindow.refresh_play_button_icon whenever playstate
        changes, so Signal Lab's Play button stays in sync if the user stops
        the main pipeline from the toolbar while Signal Lab is running in
        FOC-Stim mode. playstate is a PlayState enum but we only care whether
        it is STOPPED - we compare by .name to avoid importing PlayState at
        module load time (would create a circular import)."""
        is_stopped = getattr(playstate, 'name', None) == 'STOPPED'
        if is_stopped and self._active_mode == MODE_FOCSTIM:
            # The main window already tore down self.output_device. All that
            # is left for us to do is reset our button and re-enable the mode
            # radios. Do NOT call signal_stop again - it is already stopped.
            if self.play_button.isChecked():
                self.play_button.blockSignals(True)
                self.play_button.setChecked(False)
                self.play_button.blockSignals(False)
            self.play_button.setText("Play")
            self.status_label.setText("Stopped (by main window)")
            self._active_mode = None
            self._set_mode_controls_enabled(True)

    def closeEvent(self, event):
        self._stop_audio()
        self._stop_focstim()
        super().closeEvent(event)
