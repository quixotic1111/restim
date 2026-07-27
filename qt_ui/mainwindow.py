import json
import os
import sys
from enum import Enum

from PySide6 import QtGui
from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtHttpServer import QHttpServerRequest
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSizePolicy, QFrame, QStyleFactory, QVBoxLayout, QHBoxLayout, QLCDNumber
)
import logging

from net.media_source.interface import MediaConnectionState
from qt_ui.algorithm_factory import AlgorithmFactory
from qt_ui.audio_write_dialog import AudioWriteDialog
from qt_ui.main_window_ui import Ui_MainWindow
import qt_ui.patterns.threephase_patterns
import qt_ui.patterns.fourphase_patterns
from device.audio.audio_stim_device import AudioStimDevice
import net.websocketserver
import net.tcpudpserver
import net.http_server
import qt_ui.funscript_conversion_dialog
import qt_ui.simfile_conversion_dialog
import qt_ui.focstim_flash_dialog
import qt_ui.funscript_decomposition_dialog
import qt_ui.preferences_dialog
import qt_ui.about_dialog
import qt_ui.settings
import net.serialproxy
import net.buttplug_wsdm_client
from qt_ui import resources
from qt_ui.models.funscript_kit import FunscriptKitModel
from device.focstim.proto_device import FOCStimProtoDevice, LSM6DSOX_SAMPLERATE_HZ
from device.neostim.neostim_device import NeoStim
from qt_ui.widgets.battery_progress_bar import BatteryProgressBar
from qt_ui.widgets.icon_with_connection_status import IconWithConnectionStatus
from stim_math.axis import create_temporal_axis


import sounddevice as sd

from qt_ui.device_wizard.wizard import DeviceSelectionWizard
from qt_ui.device_wizard.enums import DeviceConfiguration, DeviceType, WaveformType

from qt_ui.tcode_command_router import TCodeCommandRouter

from device.focstim.calibration_adapter import FOCStimCalibrationAdapter
from device.focstim.calibration_algorithm import CalibrationFourphaseAlgorithm
from qt_ui.calibration.wizard import CalibrationWizard
from stim_math.audio_gen.switching_algorithm import SwitchingAlgorithm
from stim_math.calibration.io import load as load_calibration_profile
from stim_math.calibration.profile import CalibrationProfile
from stim_math.calibration.session import CalibrationSession
from version import VERSION as RESTIM_VERSION
from PySide6.QtGui import QAction
import math as _math

logger = logging.getLogger('restim.main')


class PlayState(Enum):
    STOPPED = 0
    PLAYING = 1
    WAITING_ON_LOAD = 2  # the audio is stopped, but is ready to be auto-started once funscripts are loaded.


class Window(QMainWindow, Ui_MainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)

        self.playstate = PlayState.STOPPED
        self.tab_volume.set_play_state(self.playstate)
        self.refresh_play_button_icon()

        # set the first tab as active tab, in case we forgot to set it in designer
        self.tabWidget.setCurrentIndex(0)

        # TODO: credit https://glyphs.fyi/ for icons
        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(resources.favicon), QtGui.QIcon.Normal, QtGui.QIcon.Off)
        self.setWindowIcon(icon)

        # setup left toolbar
        spacer = QWidget()
        spacer.sizePolicy()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.battery_bar = BatteryProgressBar(self)
        self.battery_bar.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.device_volume_display = QLCDNumber(self, digitCount=3, segmentStyle=QLCDNumber.SegmentStyle.Filled)
        self.device_volume_display.setToolTip("Device volume\r\nAdjust with knob on device")
        self.device_volume_display.setFixedHeight(30)
        self.device_volume_display.display(0)
        self.last_device_volume = None

        self.frame = QWidget()
        frame_layout = QVBoxLayout(self.toolBar)
        frame_layout.addWidget(spacer)
        frame_layout.addWidget(self.device_volume_display)
        frame_layout.addWidget(self.battery_bar)
        self.frame.setLayout(frame_layout)
        self.toolBar.insertWidget(self.actionStart, self.frame)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        self.toolBar.insertWidget(self.actionStart, line)



        self.doubleSpinBox_volume.setValue(qt_ui.settings.volume_default_level.get())
        self.tab_volume.link_volume_controls(self.doubleSpinBox_volume, self.progressBar_volume)

        # default alpha/beta axis. Used by:
        # pattern generator
        # network stuff (intiface, tcode)
        self.alpha = create_temporal_axis(0.0)
        self.beta = create_temporal_axis(0.0)

        self.intensity_a = create_temporal_axis(0.0)
        self.intensity_b = create_temporal_axis(0.0)
        self.intensity_c = create_temporal_axis(0.0)
        self.intensity_d = create_temporal_axis(0.0)

        self.sensor_suppression = create_temporal_axis(0.0)

        self.tcode_command_router = TCodeCommandRouter(
            self.alpha,
            self.beta,

            self.tab_volume.axis_api_volume,
            self.tab_volume.axis_external_volume,

            self.tab_carrier.axis_carrier,  # this gets set to the device-specific axis later

            self.tab_pulse_settings.axis_pulse_frequency,
            self.tab_pulse_settings.axis_pulse_width,
            self.tab_pulse_settings.axis_pulse_interval_random,
            self.tab_pulse_settings.axis_pulse_rise_time,

            self.tab_vibrate.vibration_1.frequency,
            self.tab_vibrate.vibration_1.strength,
            self.tab_vibrate.vibration_1.left_right_bias,
            self.tab_vibrate.vibration_1.high_low_bias,
            self.tab_vibrate.vibration_1.random,

            self.tab_vibrate.vibration_2.frequency,
            self.tab_vibrate.vibration_2.strength,
            self.tab_vibrate.vibration_2.left_right_bias,
            self.tab_vibrate.vibration_2.high_low_bias,
            self.tab_vibrate.vibration_2.random,

            self.intensity_a,
            self.intensity_b,
            self.intensity_c,
            self.intensity_d,

            self.sensor_suppression,

            # TODO: neostim
        )

        # threephase view
        self.motion_3 = qt_ui.patterns.threephase_patterns.ThreephaseMotionGenerator(self, self.alpha, self.beta)
        self.graphicsView_threephase.set_transform_params(self.tab_threephase.transform_params)
        self.graphicsView_threephase.mousePositionChanged.connect(self.motion_3.mouse_event)
        self.motion_3.position_updated.connect(self.graphicsView_threephase.set_cursor_position_ab)
        self.motion_3.path_updated.connect(self.graphicsView_threephase.set_path)
        self.graphicsView_threephase.set_sensor_widget(self.page_sensors)

        # fourphase view
        self.motion_4 = qt_ui.patterns.fourphase_patterns.FourphaseMotionGenerator(
            self, self.intensity_a, self.intensity_b, self.intensity_c, self.intensity_d)
        self.graphicsView_fourphase.mouse_update_all.connect(self.motion_4.mouse_event)
        self.graphicsView_fourphase.mouse_update_e1.connect(self.motion_4.mouse_event_e1)
        self.graphicsView_fourphase.mouse_update_e2.connect(self.motion_4.mouse_event_e2)
        self.graphicsView_fourphase.mouse_update_e3.connect(self.motion_4.mouse_event_e3)
        self.graphicsView_fourphase.mouse_update_e4.connect(self.motion_4.mouse_event_e4)
        self.motion_4.position_updated.connect(self.graphicsView_fourphase.set_electrode_intensities)
        self.graphicsView_fourphase.set_sensor_widget(self.page_sensors)

        # TODO: implement details for 4-phase
        self.tab_details.set_axis(
            self.alpha,
            self.beta,
            self.tab_threephase.calibrate_params,
            self.tab_threephase.transform_params,
        )
        # self.tab_details.set_config_manager(self.threephase_parameters)

        self.comboBox_patternSelect.currentIndexChanged.connect(self.pattern_selection_changed)
        self.motion_3.set_pattern(self.comboBox_patternSelect.currentText())
        self.doubleSpinBox.valueChanged.connect(self.motion_3.set_velocity)
        self.doubleSpinBox.valueChanged.connect(self.motion_4.set_velocity)
        self.motion_3.set_velocity(self.doubleSpinBox.value())

        self.output_device = None

        # Internal media source needs to know when TCode traffic is live so it
        # can flip its reported state to PLAYING (so the audio_gen mute guard
        # unmutes the carrier). page_media is created during setupUi above.
        _internal_source = next(
            (s for s in self.page_media.media_sync if s.is_internal()),
            None,
        )

        # The internal Live-Control pattern generator counts as activity too, so
        # selecting Internal + Start drives output without an external T-code
        # source (e.g. to calibrate electrodes). Only mark activity when the
        # generated position actually CHANGES, so the mute guard still silences a
        # static / at-rest pattern (its original purpose).
        self._internal_source = _internal_source
        self._last_pattern_pos = None
        self.motion_3.position_updated.connect(self._note_pattern_activity)
        self.motion_4.position_updated.connect(self._note_pattern_activity)

        self.websocket_server = net.websocketserver.WebSocketServer(self)
        self.websocket_server.new_tcode_command.connect(self.tcode_command_router.route_command)
        if _internal_source is not None:
            self.websocket_server.new_tcode_command.connect(_internal_source.notify_activity)

        self.websocket_server.incoming_as5311_data.connect(self.page_sensors.new_as5311_sensor_data_from_network)
        self.websocket_server.incoming_imu_data.connect(self.page_sensors.new_imu_sensor_data_from_network)
        self.websocket_server.incoming_pressure_data.connect(self.page_sensors.new_pressure_sensor_data_from_network)

        self.http_server = net.http_server.HttpServer(self)
        self.http_server.route('/v1/status', self.api_status)
        self.http_server.add_action('start', self.api_start)
        self.http_server.add_action('stop', self.api_stop)

        self.tcpudp_server = net.tcpudpserver.TcpUdpServer(self)
        self.tcpudp_server.new_tcode_command.connect(self.tcode_command_router.route_command)
        if _internal_source is not None:
            self.tcpudp_server.new_tcode_command.connect(_internal_source.notify_activity)

        self.serial_proxy = net.serialproxy.SerialProxy(self)
        self.serial_proxy.new_tcode_command.connect(self.tcode_command_router.route_command)
        if _internal_source is not None:
            self.serial_proxy.new_tcode_command.connect(_internal_source.notify_activity)

        self.buttplug_wsdm_client = net.buttplug_wsdm_client.ButtplugWsdmClient(self)
        self.buttplug_wsdm_client.new_tcode_command.connect(self.tcode_command_router.route_command)
        if _internal_source is not None:
            self.buttplug_wsdm_client.new_tcode_command.connect(_internal_source.notify_activity)

        self.tab_volume.set_monitor_axis([
            self.alpha,
            self.beta,
            self.intensity_a,
            self.intensity_b,
            self.intensity_c,
            self.intensity_d,
        ])

        # stop audio when user modifies settings in media tab
        self.page_media.dialogOpened.connect(self.signal_stop)
        self.page_media.funscriptMappingChanged.connect(self.funscript_mapping_changed)
        self.page_media.connectionStatusChanged.connect(self.media_connection_status_changed)
        self.page_media.bake_audio_button.clicked.connect(self.open_write_audio_dialog)

        # trigger updates.... maybe not all needed?
        # self.tab_carrier.settings_changed()
        self.tab_pulse_settings.settings_changed()
        self.tab_threephase.settings_changed()
        self.tab_volume.refresh_master_volume()
        self.tab_vibrate.settings_changed()

        self.wizard = DeviceSelectionWizard(self)
        self.actionDevice_selection_wizard.triggered.connect(self.open_setup_wizard)

        self.dialog = qt_ui.funscript_conversion_dialog.FunscriptConversionDialog()
        self.actionFunscript_conversion.triggered.connect(self.open_funscript_conversion_dialog)

        self.simfile_conversion_dialog = qt_ui.simfile_conversion_dialog.SimfileConversionDialog()
        self.actionSimfile_conversion.triggered.connect(self.open_simfile_conversion_dialog)

        self.focstim_flash_dialog = qt_ui.focstim_flash_dialog.FocStimFlashDialog()
        self.actionFirmware_updater.triggered.connect(self.open_focstim_flash_dialog)

        self.funscript_decomposition_dialog = qt_ui.funscript_decomposition_dialog.FunscriptDecompositionDialog()
        self.actionFunscript_decomposition.triggered.connect(self.open_funscript_decomposition_dialog)

        self.settings_dialog = qt_ui.preferences_dialog.PreferencesDialog()
        self.actionPreferences.triggered.connect(self.open_preferences_dialog)

        self.about_dialog = qt_ui.about_dialog.AboutDialog(self)
        self.actionAbout.triggered.connect(self.open_about_dialog)

        # Calibration wizard menu entry (added programmatically, not in .ui).
        # Always enabled — the wizard handles its own signal_start internally,
        # with master volume forced to 0 so the hardware knob can't deliver
        # surprise signal during entry. Device-type check happens at click time.
        self._switching_algorithm: SwitchingAlgorithm | None = None
        self._user_algorithm = None
        self.actionCalibration_wizard = QAction('Calibration wizard…', self)
        self.actionCalibration_wizard.setEnabled(True)
        self.actionCalibration_wizard.triggered.connect(self.open_calibration_wizard)
        self.menuTools.addAction(self.actionCalibration_wizard)

        self.iconMedia = IconWithConnectionStatus(self.actionMedia.icon(), self.toolBar.widgetForAction(self.actionMedia))
        self.actionMedia.setIcon(QIcon(self.iconMedia))
        # self.iconDevice = IconWithConnectionStatus(self.actionDevice.icon(), self.toolBar.widgetForAction(self.actionDevice))
        # self.actionDevice.setIcon(QIcon(self.iconDevice))

        self.connect_signals_slots_actionbar()

        self.refresh_device_type()

        # Auto-load any saved calibration profile and stage its gain_trims
        # as dB offsets for the 4-phase output stage (spinboxes stay
        # user-owned; the algorithm factory overlays the offsets). Runs
        # once at startup; the wizard re-applies after each save.
        self.calibration_trims_db = {}
        self._load_and_apply_saved_calibration()

        config = DeviceConfiguration.from_settings()
        if config.device_type == DeviceType.NONE:
            self.timer = QTimer()
            self.timer.setSingleShot(True)
            self.timer.timeout.connect(self.open_setup_wizard)
            self.timer.start(0)

        self.autostart_timer = QTimer()
        self.autostart_timer.setSingleShot(True)
        self.autostart_timer.timeout.connect(self.autostart_timeout)
        self.autostart_timer.setInterval(5000)

    def connect_signals_slots_actionbar(self):
        def uncheck():
            self.actionControl.setChecked(False)
            self.actionMedia.setChecked(False)
            self.actionSensors.setChecked(False)
            # self.actionDevice.setChecked(False)
            # self.actionLog.setChecked(False)

        def show_control():
            uncheck()
            self.actionControl.setChecked(True)
            self.stackedWidget.setCurrentIndex(self.stackedWidget.indexOf(self.page_control))

        def show_media():
            uncheck()
            self.actionMedia.setChecked(True)
            self.stackedWidget.setCurrentIndex(self.stackedWidget.indexOf(self.page_media))

        def show_sensors():
            uncheck()
            self.actionSensors.setChecked(True)
            self.stackedWidget.setCurrentIndex(self.stackedWidget.indexOf(self.page_sensors))

        # def show_device():
        #     uncheck()
        #     self.actionDevice.setChecked(True)
        #     self.stackedWidget.setCurrentIndex(self.stackedWidget.indexOf(self.page_device))

        # def show_log():
        #     uncheck()
        #     self.actionLog.setChecked(True)
        #     self.stackedWidget.setCurrentIndex(self.stackedWidget.indexOf(self.page_log))

        self.actionControl.triggered.connect(show_control)
        self.actionMedia.triggered.connect(show_media)
        self.actionSensors.triggered.connect(show_sensors)
        # self.actionDevice.triggered.connect(show_device)
        # self.actionLog.triggered.connect(show_log)
        self.actionStart.triggered.connect(self.signal_start_stop)

        # Alt+1..4 hot-swap between funscript variants (A/B/C/D) when a
        # `<scene>_variants/` folder is present next to the loaded media.
        # Ctrl+1..4 are already bound to the sidebar tabs.
        for i, letter in enumerate(['A', 'B', 'C', 'D']):
            shortcut = QShortcut(QKeySequence(f'Alt+{i + 1}'), self)
            shortcut.activated.connect(lambda l=letter: self._select_funscript_variant(l))

    def _select_funscript_variant(self, letter: str):
        if self.page_media.select_variant_by_letter(letter):
            logger.info(f'selected funscript variant {letter}')

    def update_device_volume(self, value):
        self.last_device_volume = value
        self.device_volume_display.display(int(round(value * 100)))

    def media_connection_status_changed(self, status: MediaConnectionState):
        """
        Called whenever the media connection status changes.
        """
        if status.is_playing():
            self.iconMedia.set_playing()
        elif status.is_connected():
            self.iconMedia.set_connected()
        else:
            self.iconMedia.set_not_connected()

    def funscript_mapping_changed(self):
        """
        Called whenever the loaded funscripts change
        """
        logger.info('funscript mapping changed, re-linking scripts.')
        if self.page_media.autostart_enabled():
            if self.playstate == PlayState.PLAYING:
                self.signal_stop(PlayState.WAITING_ON_LOAD)
                self.autostart_timer.start()
        else:
            self.signal_stop(PlayState.STOPPED)

        device = DeviceConfiguration.from_settings()
        algorithm_factory = AlgorithmFactory(
            self,
            FunscriptKitModel.load_from_settings(),
            self.page_media.model,
            self.page_media.current_media_sync(),
            self.page_media.current_media_sync(),
            load_funscripts=not self.page_media.is_internal(),
        )

        # 3-phase visualization
        self.motion_3.set_scripts(
            algorithm_factory.get_axis_alpha(),
            algorithm_factory.get_axis_beta(),
        )

        # 4-phase visualization
        self.motion_4.set_scripts(
            algorithm_factory.get_axis_intensity_a(),
            algorithm_factory.get_axis_intensity_b(),
            algorithm_factory.get_axis_intensity_c(),
            algorithm_factory.get_axis_intensity_d(),
        )

        # volume tab
        self.tab_volume.set_monitor_axis([
            algorithm_factory.get_axis_alpha(),
            algorithm_factory.get_axis_beta(),
        ])
        self.tab_volume.axis_funscript_volume = algorithm_factory.get_axis_volume_api()

        # continuous tab
        self.tab_carrier.carrier_controller.link_axis(algorithm_factory.get_axis_continuous_carrier_frequency())

        # pulse tab
        self.tab_pulse_settings.carrier_controller.link_axis(algorithm_factory.get_axis_pulse_carrier_frequency())
        self.tab_pulse_settings.pulse_frequency_controller.link_axis(algorithm_factory.get_axis_pulse_frequency())
        self.tab_pulse_settings.pulse_width_controller.link_axis(algorithm_factory.get_axis_pulse_width())
        self.tab_pulse_settings.pulse_interval_random_controller.link_axis(algorithm_factory.get_axis_pulse_interval_random())
        self.tab_pulse_settings.pulse_rise_time_controller.link_axis(algorithm_factory.get_axis_pulse_rise_time())

        # vibration tab
        self.tab_vibrate.vib1_enabled_controller.link_axis(algorithm_factory.get_axis_vib1_enabled())
        self.tab_vibrate.vib1_freq_controller.link_axis(algorithm_factory.get_axis_vib1_frequency())
        self.tab_vibrate.vib1_strength_controller.link_axis(algorithm_factory.get_axis_vib1_strength())
        self.tab_vibrate.vib1_left_right_bias_controller.link_axis(algorithm_factory.get_axis_vib1_left_right_bias())
        self.tab_vibrate.vib1_high_low_bias_controller.link_axis(algorithm_factory.get_axis_vib1_high_low_bias())
        self.tab_vibrate.vib1_random_controller.link_axis(algorithm_factory.get_axis_vib1_random())
        self.tab_vibrate.vib2_enabled_controller.link_axis(algorithm_factory.get_axis_vib2_enabled())
        self.tab_vibrate.vib2_freq_controller.link_axis(algorithm_factory.get_axis_vib2_frequency())
        self.tab_vibrate.vib2_strength_controller.link_axis(algorithm_factory.get_axis_vib2_strength())
        self.tab_vibrate.vib2_left_right_bias_controller.link_axis(algorithm_factory.get_axis_vib2_left_right_bias())
        self.tab_vibrate.vib2_high_low_bias_controller.link_axis(algorithm_factory.get_axis_vib2_high_low_bias())
        self.tab_vibrate.vib2_random_controller.link_axis(algorithm_factory.get_axis_vib2_random())

        # neostim tab
        # TODO

        # sensors tab
        self.page_sensors.set_sensor_suppression_axis(algorithm_factory.get_axis_sensor_suppression())

        if all((not self.page_media.is_internal(),
                self.page_media.has_media_file_loaded(),
                self.page_media.autostart_enabled(),
                self.playstate == PlayState.WAITING_ON_LOAD)):
            logger.info("autostart audio")
            self.signal_start()

    def refresh_device_type(self):
        def set_visible(widget, state):
            self.tabWidget.setTabVisible(self.tabWidget.indexOf(widget), state)
            self.tabWidget.setTabEnabled(self.tabWidget.indexOf(widget), state)

        all_tabs = {self.tab_threephase,
                    self.tab_fourphase,
                    self.tab_pulse_settings,
                    self.tab_carrier,
                    self.tab_volume,
                    self.tab_vibrate,
                    self.tab_details,
                    self.tab_a_b_testing,
                    self.tab_neostim}

        visible = {self.tab_threephase, self.tab_volume, self.tab_vibrate, self.tab_details}

        all_widgets = {self.device_volume_display, self.battery_bar, self.foc_device_stats}
        visible_widgets = set()

        config = DeviceConfiguration.from_settings()

        # determine tab visibility
        if config.device_type == DeviceType.AUDIO_THREE_PHASE:
            if config.waveform_type == WaveformType.CONTINUOUS:
                visible |= {self.tab_carrier}
            if config.waveform_type == WaveformType.PULSE_BASED:
                visible |= {self.tab_pulse_settings}
            if config.waveform_type == WaveformType.A_B_TESTING:
                visible |= {self.tab_a_b_testing}
        if config.device_type == DeviceType.FOCSTIM_THREE_PHASE:
            if config.waveform_type == WaveformType.A_B_TESTING:
                visible |= {self.tab_a_b_testing}
                visible -= {self.tab_vibrate}
            else:
                visible |= {self.tab_pulse_settings}
                visible -= {self.tab_vibrate}
            visible_widgets |= {self.device_volume_display, self.battery_bar, self.foc_device_stats}
        if config.device_type == DeviceType.FOCSTIM_FOUR_PHASE:
            visible |= {self.tab_pulse_settings, self.tab_fourphase}
            visible -= {self.tab_vibrate, self.tab_threephase, self.tab_details}
            visible_widgets |= {self.device_volume_display, self.battery_bar, self.foc_device_stats}
        if config.device_type == DeviceType.NEOSTIM_THREE_PHASE:
            visible |= {self.tab_neostim}
            visible -= {self.tab_vibrate, self.tab_details}

        for tab in all_tabs:
            set_visible(tab, tab in visible)

        for widget in all_widgets:
            widget.setVisible(widget in visible_widgets)
            widget.setEnabled(widget in visible_widgets)

        # set safety limits
        self.tab_carrier.set_safety_limits(config.min_frequency, config.max_frequency)
        self.tab_pulse_settings.set_safety_limits(config.min_frequency, config.max_frequency)
        self.tab_a_b_testing.set_safety_limits(config.min_frequency, config.max_frequency)

        # configure tcode router
        if config.waveform_type == WaveformType.CONTINUOUS:
            self.tcode_command_router.set_carrier_axis(self.tab_carrier.axis_carrier)
        if config.waveform_type == WaveformType.PULSE_BASED:
            self.tcode_command_router.set_carrier_axis(self.tab_pulse_settings.axis_carrier_frequency)

        # populate motion generator and patterns combobox
        if config.device_type in (DeviceType.AUDIO_THREE_PHASE, DeviceType.NEOSTIM_THREE_PHASE, DeviceType.FOCSTIM_THREE_PHASE):
            self.motion_3.set_enable(True)
            self.motion_4.set_enable(False)
            self.stackedWidget_visual.setCurrentIndex(
                self.stackedWidget_visual.indexOf(self.page_threephase)
            )

        if config.device_type == DeviceType.FOCSTIM_FOUR_PHASE:
            self.motion_3.set_enable(False)
            self.motion_4.set_enable(True)
            self.stackedWidget_visual.setCurrentIndex(
                self.stackedWidget_visual.indexOf(self.page_fourphase)
            )

        if config.device_type == DeviceType.AUDIO_THREE_PHASE:
            self.graphicsView_threephase.set_background(stereo=True)
            self.tab_threephase.phase_widget_calibration.set_background(stereo=True)
        else:
            self.graphicsView_threephase.set_background(foc=True)
            self.tab_threephase.phase_widget_calibration.set_background(foc=True)

        self.refresh_pattern_combobox()
        self.foc_device_stats.reset_utilization()

    def _note_pattern_activity(self, *position):
        """Mark internal-source activity when the pattern generator's output
        position changes — keeps the carrier unmuted while a pattern is moving,
        without an external T-code source. A static position marks no activity,
        so the mute guard still silences an at-rest pattern."""
        key = tuple(round(float(v), 4) for v in position)
        if key != self._last_pattern_pos:
            self._last_pattern_pos = key
            src = getattr(self, "_internal_source", None)
            if src is not None:
                src.notify_activity()

    def pattern_selection_changed(self, index):
        pattern = self.comboBox_patternSelect.currentData()
        self.motion_3.set_pattern(pattern)
        self.motion_4.set_pattern(pattern)

    def signal_start_stop(self):
        if self.playstate == PlayState.STOPPED:
            self.signal_start()
        else:
            self.signal_stop(PlayState.STOPPED)

    def signal_start(self):
        assert self.output_device is None

        self.autostart_timer.stop()
        device = DeviceConfiguration.from_settings()
        algorithm_factory = AlgorithmFactory(
            self,
            FunscriptKitModel.load_from_settings(),
            self.page_media.model,
            self.page_media.current_media_sync(),
            self.page_media.current_media_sync(),
            load_funscripts=not self.page_media.is_internal(),
        )
        algorithm = algorithm_factory.create_algorithm(device)
        user_algorithm = algorithm  # keep reference to the unwrapped user algorithm

        # Wrap in SwitchingAlgorithm for FOC-stim 4-phase so the calibration
        # wizard can hot-swap between user mode and calibration mode without
        # restarting the device. Default mode is MODE_USER (transparent).
        if device.device_type == DeviceType.FOCSTIM_FOUR_PHASE:
            calibration_algorithm = CalibrationFourphaseAlgorithm(
                media=self.page_media.current_media_sync(),
                max_amplitude_amps=device.waveform_amplitude_amps,
                # Sensible defaults — Phase 1 measurement doesn't depend on
                # exact carrier/pulse values, just on having a stable signal.
                carrier_frequency_hz=800.0,
                pulse_frequency_hz=50.0,
                pulse_width_cycles=4.0,
                pulse_rise_time_cycles=2.0,
            )
            algorithm = SwitchingAlgorithm(user_algorithm, calibration_algorithm)
            self._switching_algorithm = algorithm
            self._user_algorithm = user_algorithm

        if device.device_type in [
            DeviceType.AUDIO_THREE_PHASE,
        ]: # is audio device
            api_name = qt_ui.settings.audio_api.get() or sd.query_hostapis(sd.default.hostapi)['name']
            output_device_name = qt_ui.settings.audio_output_device.get() or sd.query_devices(sd.default.device[1])['name']
            latency = qt_ui.settings.audio_latency.get() or 'high'
            try:
                latency = float(latency)
            except ValueError:
                pass

            output_device = AudioStimDevice(None)
            mapping_parameters = output_device.auto_detect_channel_mapping_parameters(algorithm)
            output_device.start(api_name, output_device_name, latency, algorithm, mapping_parameters)
            if output_device.is_connected_and_running():
                self.output_device = output_device
                self.playstate = PlayState.PLAYING
                self.tab_volume.set_play_state(self.playstate)
                self.refresh_play_button_icon()
        elif device.device_type in (DeviceType.FOCSTIM_THREE_PHASE, DeviceType.FOCSTIM_FOUR_PHASE):
            output_device = FOCStimProtoDevice()
            use_teleplot = qt_ui.settings.focstim_use_teleplot.get()
            dump_notifications = qt_ui.settings.focstim_dump_notifications_to_file.get()
            comms_wifi = qt_ui.settings.focstim_communication_wifi.get()
            if not comms_wifi:
                serial_port_name = qt_ui.settings.focstim_serial_port.get()
                output_device.start_serial(serial_port_name, use_teleplot, dump_notifications, algorithm)
            else:
                ip = qt_ui.settings.focstim_ip.get()
                output_device.start_tcp(ip, 55533, use_teleplot, dump_notifications, algorithm)

            if output_device.is_connected_and_running():
                self.output_device = output_device
                self.playstate = PlayState.PLAYING
                self.tab_volume.set_play_state(self.playstate)
                self.refresh_play_button_icon()
                self.output_device.disconnected.connect(self.signal_stop)

                output_device.new_as5311_sensor_data.connect(self.page_sensors.new_as5311_sensor_data_from_device)
                output_device.new_imu_sensor_data.connect(self.page_sensors.new_imu_sensor_data_from_device)
                output_device.new_pressure_sensor_data.connect(self.page_sensors.new_pressure_sensor_data_from_device)
                # sensor_node lives on the user's algorithm (the unwrapped one),
                # not the SwitchingAlgorithm proxy.
                user_algorithm.sensor_node = self.page_sensors

                output_device.new_as5311_sensor_data.connect(self.websocket_server.transmit_as5311_data)
                output_device.new_imu_sensor_data.connect(self.websocket_server.transmit_imu_data)
                output_device.new_pressure_sensor_data.connect(self.websocket_server.transmit_pressure_data)

                output_device.new_battery_data.connect(self.battery_bar.setValue)
                output_device.new_device_volume_data.connect(self.update_device_volume)
                output_device.new_utilization_data.connect(self.foc_device_stats.update_utilization)
                output_device.new_resistance_data.connect(self.foc_device_stats.update_resistance)


        elif device.device_type == DeviceType.NEOSTIM_THREE_PHASE:
            output_device = NeoStim()
            serial_port_name = qt_ui.settings.neostim_serial_port.get()
            output_device.start(serial_port_name, algorithm)
            if output_device.is_connected_and_running():
                self.output_device = output_device
                self.playstate = PlayState.PLAYING
                self.tab_volume.set_play_state(self.playstate)
                self.refresh_play_button_icon()
        else:
            raise RuntimeError("Unknown device type")

    def signal_stop(self, new_playstate: PlayState = PlayState.STOPPED):
        if self.output_device is not None:
            self.output_device.stop()
            self.output_device = None
        self.playstate = new_playstate
        self.tab_volume.set_play_state(self.playstate)
        self.refresh_play_button_icon()
        # Tear down calibration-wizard plumbing — references go stale once the
        # device is stopped. The menu action stays enabled (wizard handles
        # its own signal_start when launched fresh).
        self._switching_algorithm = None
        self._user_algorithm = None

    def open_calibration_wizard(self):
        """Launch the FOC-stim calibration wizard.

        Handles signal_start internally with master volume forced to 0 so the
        device's hardware volume knob cannot deliver surprise signal during
        the session. Master volume is restored to its pre-wizard level when
        the wizard closes (device is stopped; click Start to resume).
        """
        from PySide6.QtWidgets import QMessageBox

        # Device-type check — wizard only supports 4-phase FOC-stim
        config = DeviceConfiguration.from_settings()
        if config.device_type != DeviceType.FOCSTIM_FOUR_PHASE:
            QMessageBox.information(
                self,
                'Calibration wizard',
                'The calibration wizard supports 4-phase FOC-stim devices '
                'only. Configure your device via the Setup menu first.',
            )
            return

        # Save volume, then zero it before starting signal. The wizard uses
        # calibration mode (which bypasses master volume) so it doesn't need
        # the master at any particular value — zeroing it just makes the
        # hardware knob safe during the session.
        _pre_wizard_volume = self.tab_volume.doubleSpinBox_volume.value()
        self.tab_volume.doubleSpinBox_volume.setValue(0)

        # Start signal if not already running. signal_start sets up the
        # SwitchingAlgorithm for 4-phase FOC-stim, which the wizard needs.
        if self.output_device is None:
            self.signal_start()

        if self._switching_algorithm is None or not isinstance(
            self.output_device, FOCStimProtoDevice,
        ):
            QMessageBox.warning(
                self,
                'Calibration wizard',
                'Could not start the FOC-stim device for calibration. '
                'Verify the device is connected and try again.',
            )
            self.tab_volume.doubleSpinBox_volume.setValue(_pre_wizard_volume)
            return

        adapter = FOCStimCalibrationAdapter(
            device=self.output_device,
            switching_algorithm=self._switching_algorithm,
            firmware_version=RESTIM_VERSION,
            max_safe_drive=1.0,
        )
        session = CalibrationSession(
            restim_version=RESTIM_VERSION,
            device_name='FOC-stim',
        )
        wizard = CalibrationWizard(adapter, session, parent=self)
        wizard.wizard_finished.connect(self.signal_stop)
        wizard.exec()
        self._load_and_apply_saved_calibration()
        # Restore master volume then restart signal so the device is immediately
        # ready for T-code streaming without requiring the user to manually click
        # Start. signal_stop was called via wizard_finished before exec() returned,
        # so output_device is guaranteed None here and signal_start() is safe.
        self.tab_volume.doubleSpinBox_volume.setValue(_pre_wizard_volume)
        self.signal_start()

    def _load_and_apply_saved_calibration(self) -> None:
        """Read ~/.restim/calibration.json (if present) and apply gain_trims.

        Called once at startup and again after each wizard exit. Silently
        no-ops if no profile exists or if the profile fails validation —
        the wizard remains the only way to produce a profile, so a missing
        one is expected on first launch.
        """
        try:
            profile, result = load_calibration_profile()
        except Exception:
            logger.exception('failed to load calibration profile')
            return
        if profile is None:
            # Normal case before any wizard run; not an error.
            logger.debug(f'no calibration profile to apply: {"; ".join(result.errors)}')
            return
        if not result.ok:
            logger.warning(
                f'calibration profile has issues, skipping apply: {result.errors}'
            )
            return
        self._apply_calibration_profile(profile)

    def _apply_calibration_profile(self, profile: CalibrationProfile) -> None:
        """Stage the profile's gain_trims for the 4-phase output stage.

        History: this used to be log-only, on the assumption that Funscript
        Tools bakes the trims into its rendered electrode funscripts. It
        doesn't — FT's bake_gain is opt-in and default-off ("current
        balancing is a device concern"), so the trims were applied by
        NOBODY and the wizard's measurements were inert (found 2026-07-08
        chasing persistent E3/E4 heaviness).

        The trims are still NOT pushed into the A/B/C/D spinboxes — those
        stay user-owned. Instead they're staged here as dB offsets and the
        algorithm factory overlays them (OffsetAxis) on the calibrate axes
        at signal build. The double-apply escape hatch is the
        calibration/apply_gain_trims setting: turn it off if you enable
        FT's bake_gain.
        """
        self.calibration_trims_db = {}
        if not qt_ui.settings.calibration_apply_gain_trims.get():
            logger.info('calibration gain_trims present but '
                        'calibration/apply_gain_trims=false — not applied '
                        '(assumed baked upstream)')
            return
        staged: list[str] = []
        for name, electrode in profile.electrodes.items():
            # attenuation-only by construction (wizard normalizes), but
            # clamp defensively: never boost, never -inf
            gain = min(max(electrode.gain_trim, 1e-3), 1.0)
            db = 20.0 * _math.log10(gain)
            # Safety floor: trims are for BALANCING, and the felt dynamic
            # range is only ~6-10 dB — a trim past -9 dB doesn't balance an
            # electrode, it deletes it (threshold), and almost certainly
            # encodes a corrupted measurement or a runaway wizard slider
            # (2026-07-08: a saved 0.065 trim made E4 vanish entirely).
            if db < -9.0:
                logger.warning(
                    f'calibration: {name} trim {db:+.1f}dB is beyond the '
                    f'-9dB balance floor — clamped. Re-run the wizard; if '
                    f'it persists, the imbalance is physical.')
                db = -9.0
            self.calibration_trims_db[name] = db
            if abs(db) > 0.01:
                staged.append(f'{name}={db:+.2f}dB (gain={gain:.3f})')
        if staged:
            logger.info('calibration gain_trims staged for 4-phase output: '
                        + ", ".join(staged))

    def autostart_timeout(self):
        print('autostart timeout')
        if self.playstate == PlayState.WAITING_ON_LOAD:
            logger.info("autostart timeout reached. No longer starting audio on file load")
            self.signal_stop(PlayState.STOPPED)

    def refresh_play_button_icon(self):
        if self.playstate in (PlayState.PLAYING, PlayState.WAITING_ON_LOAD):
            self.actionStart.setIcon(QtGui.QIcon(":/restim/stop-sign_poly.svg"))
            self.actionStart.setText("Stop")
        else:
            self.actionStart.setIcon(QtGui.QIcon(":/restim/play_poly.svg"))
            self.actionStart.setText("Start")

    def open_setup_wizard(self):
        self.signal_stop(PlayState.STOPPED)
        self.wizard.exec()
        self.refresh_device_type()
        self.reload_settings()

    def open_funscript_conversion_dialog(self):
        self.signal_stop(PlayState.STOPPED)
        self.dialog.exec()

    def open_simfile_conversion_dialog(self):
        self.signal_stop(PlayState.STOPPED)
        self.simfile_conversion_dialog.exec()

    def open_focstim_flash_dialog(self):
        self.signal_stop(PlayState.STOPPED)
        self.focstim_flash_dialog.exec()

    def open_funscript_decomposition_dialog(self):
        self.signal_stop(PlayState.STOPPED)
        self.funscript_decomposition_dialog.exec()

    def open_preferences_dialog(self):
        self.signal_stop(PlayState.STOPPED)
        self.settings_dialog.exec()
        self.reload_settings()

    def open_about_dialog(self):
        self.signal_stop(PlayState.STOPPED)
        self.about_dialog.exec()

    def open_write_audio_dialog(self):
        device = DeviceConfiguration.from_settings()
        kit = FunscriptKitModel.load_from_settings()
        filename = self.page_media.loaded_media_path
        dialog = AudioWriteDialog(self, kit, self.page_media.model, device, filename)
        dialog.exec()

    def reload_settings(self):
        """
        Reload everything that is stored in settings and may be changed
        by the preferences dialog
        """
        self.tcode_command_router.reload_kit()
        self.tab_volume.refreshSettings()
        self.buttplug_wsdm_client.refreshSettings()
        self.funscript_mapping_changed()  # reload funscript axis
        self.tab_a_b_testing.refreshSettings()
        self.motion_3.refreshSettings()
        self.motion_4.refreshSettings()
        self.refresh_pattern_combobox()

    def refresh_pattern_combobox(self):
        config = DeviceConfiguration.from_settings()
        currently_selected_text = self.comboBox_patternSelect.currentText()

        if config.device_type in (DeviceType.AUDIO_THREE_PHASE, DeviceType.NEOSTIM_THREE_PHASE, DeviceType.FOCSTIM_THREE_PHASE):
            self.comboBox_patternSelect.clear()
            for pattern in self.motion_3.patterns:
                self.comboBox_patternSelect.addItem(pattern.name(), pattern)
        else:
            self.comboBox_patternSelect.clear()
            for pattern in self.motion_4.patterns:
                self.comboBox_patternSelect.addItem(pattern.name(), pattern)

        # try to select pattern with similar name as was previously selected
        index = self.comboBox_patternSelect.findText(currently_selected_text)
        if index == -1:
            index = 0
        self.comboBox_patternSelect.setCurrentIndex(index)


    def save_settings(self):
        """
        Save everything that is stored in settings but isn't immediately saved
        for performance reasons.
        """
        self.tab_threephase.save_settings()
        self.tab_fourphase.save_settings()
        self.tab_carrier.save_settings()
        self.tab_vibrate.save_settings()
        self.tab_pulse_settings.save_settings()
        self.tab_volume.save_settings()
        self.page_media.save_settings()
        self.page_sensors.save_settings()

    def closeEvent(self, event):
        logger.warning('Shutting down')
        if self.output_device is not None:
            self.output_device.stop()
        self.save_settings()
        event.accept()

    def api_status(self, request: QHttpServerRequest):
        params = {
            "playing": self.playstate == PlayState.PLAYING,
            "volume": {
                "ui": self.doubleSpinBox_volume.value() / 100,
            },
        }
        if self.last_device_volume is not None:
            params["volume"]["device"] = self.last_device_volume
        return json.dumps(params)

    def api_start(self, request: QHttpServerRequest):
        if self.output_device is None:
            self.signal_start()
        return "{}"

    def api_stop(self, request: QHttpServerRequest):
        self.signal_stop()
        return "{}"


def run():
    log_path = os.environ.get('RESTIM_CONFIG_DIR') or os.getcwd()
    logging.basicConfig(filename=os.path.join(log_path, 'restim.log'), filemode='w')
    logging.getLogger().addHandler(logging.StreamHandler())
    logger = logging.getLogger('restim')
    logger.setLevel(logging.DEBUG)
    logging.getLogger('matplotlib').setLevel(logging.WARN)

    def excepthook(exc_type, exc_value, exc_tb):
        exc_info = (exc_type, exc_value, exc_tb)
        logger.critical('Exception occurred', exc_info=exc_info)
        QApplication.quit()

    sys.excepthook = excepthook

    app = QApplication(sys.argv)
    wayland_app_id = os.environ.get("RESTIM_APP_ID","restim")
    app.setDesktopFileName(wayland_app_id)
    app.setApplicationName(wayland_app_id)
    win = Window()
    win.show()
    sys.exit(app.exec())