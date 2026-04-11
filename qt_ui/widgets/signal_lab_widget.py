"""
Signal Lab tab: standalone envelope-shaping experimentation tool.

Plays a carrier sine wave multiplied by an amplitude-modulation envelope through
the system's default audio output. Independent of the main stim audio pipeline
and does not drive connected stim hardware (FOC-Stim, NeoStim, etc.) - it's a
laptop-audio experimentation tool for understanding the vibrate envelope shape
parameters in isolation.

Reuses stim_math.amplitude_modulation.SineModulation for the envelope math and
stim_math.sine_generator.AngleGenerator for phase-continuous carrier/modulation
generation across buffer boundaries.
"""

import logging

import numpy as np
import pyqtgraph as pg
import sounddevice as sd
from PySide6 import QtCore
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

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


class SignalLabWidget(QWidget):
    """
    Signal Lab tab widget.

    Owns its own sounddevice OutputStream that plays a carrier sine wave shaped
    by a SineModulation envelope. The audio callback reads plain-float parameter
    snapshots from self (updated by slider signals on the main thread), so there
    is no direct Qt widget access from the audio thread.
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        # Parameter snapshot read by the audio callback. CPython single-attribute
        # float assignments are atomic under the GIL, so no lock is needed.
        self._carrier_freq = CARRIER_DEFAULT
        self._mod_freq = MOD_DEFAULT
        self._strength = 0.5
        self._rise_fall = 0.0
        self._dwell = 0.0
        self._volume = 0.2

        self._stream = None
        self._samplerate = FALLBACK_SAMPLERATE
        self._carrier_gen = AngleGenerator()
        self._mod_gen = AngleGenerator()

        self._build_ui()
        self._update_plots()

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._stop_audio)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QVBoxLayout(self)

        notice = QLabel(
            "This tab plays through your computer's default audio output. "
            "It does not drive connected stim hardware - it is a standalone "
            "experimentation tool for the vibrate envelope math."
        )
        notice.setWordWrap(True)
        notice.setStyleSheet("QLabel { color: gray; font-style: italic; }")
        root.addWidget(notice)

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
            tooltip="Master output volume. Start low.",
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
            self._start_audio()
        else:
            self._stop_audio()

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

        self.play_button.setText("Stop")
        self.status_label.setText(f"Playing @ {self._samplerate} Hz")
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

    def closeEvent(self, event):
        self._stop_audio()
        super().closeEvent(event)
