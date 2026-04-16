import functools
import os
import pathlib

from PySide6.QtCore import Qt

from funscript.collect_funscripts import Resource
from net.media_source.vlc import VLC
from net.media_source.kodi import Kodi
from qt_ui.additional_search_paths_dialog import AdditionalSearchPathsDialog
from qt_ui.device_wizard.axes import AxisEnum

from PySide6 import QtCore, QtWidgets
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QLabel, QWidget

from funscript.collect_funscripts import detect_variant_folders

from net.media_source.heresphere import HereSphere
from net.media_source.interface import MediaConnectionState
from qt_ui.file_dialog import FileDialog
from qt_ui.media_settings_widget_ui import Ui_MediaSettingsWidget

from net.media_source.internal import Internal
from net.media_source.mpc import MPC
from qt_ui.models.script_mapping import FunscriptTreeItem, ScriptMappingModel
from qt_ui.widgets.table_view_with_combobox import ComboBoxDelegate, ButtonDelegate
from qt_ui.models import funscript_kit, additional_search_paths
from qt_ui.models.funscript_kit import FunscriptKitItem
from qt_ui import settings


class _MediaSettingsWidget(type(QtWidgets.QWidget), type(Ui_MediaSettingsWidget)):
    pass


class MediaSettingsWidget(QtWidgets.QWidget, Ui_MediaSettingsWidget, metaclass=_MediaSettingsWidget):
    def __init__(self):
        QtWidgets.QWidget.__init__(self)
        self.setupUi(self)

        self.media_sync = [
            Internal(),
            MPC(self),
            HereSphere(self),
            VLC(self),
            Kodi(self),
        ]
        self.media_sync[0].connectionStatusChanged.connect(functools.partial(self.connection_status_changed, 0))
        self.media_sync[1].connectionStatusChanged.connect(functools.partial(self.connection_status_changed, 1))
        self.media_sync[2].connectionStatusChanged.connect(functools.partial(self.connection_status_changed, 2))
        self.media_sync[3].connectionStatusChanged.connect(functools.partial(self.connection_status_changed, 3))
        self.media_sync[4].connectionStatusChanged.connect(functools.partial(self.connection_status_changed, 4))

        self.comboBox.addItem("Internal")
        self.comboBox.addItem(QIcon(":/restim/media_players/mpc-hc.png"), "MPC-HC")
        self.comboBox.addItem(QIcon(":/restim/media_players/heresphere.png"), "HereSphere")
        self.comboBox.addItem(QIcon(":/restim/media_players/vlc.svg"), "VLC")
        self.comboBox.addItem(QIcon(":/restim/media_players/kodi.png"), "Kodi")
        self.comboBox.currentIndexChanged.connect(self.media_index_changed)

        self.loaded_media_path = None
        self.current_index = 0

        self.model = ScriptMappingModel()
        self.model.dataChanged.connect(self.on_data_changed)
        # An axis change moves an item between categories, which is a model
        # reset rather than a data change. Re-expand and re-propagate the
        # funscript mapping change so the algorithm factory picks up the new
        # axis assignment.
        self.model.modelReset.connect(self._on_model_reset)
        self.treeView.setModel(self.model)
        # Categories always stay expanded — no collapse affordance.
        self.treeView.setItemsExpandable(False)
        self.treeView.setRootIsDecorated(False)
        self.treeView.expandAll()

        combobox_items = []
        combobox_items.append(('(none)', AxisEnum.NONE))
        for item in funscript_kit.FunscriptKitModel.load_from_settings().children:
            item: FunscriptKitItem
            if item.allow_funscript_control:
                combobox_items.append((item.axis.display_name(), item.axis))
        self.treeView.setItemDelegateForColumn(1, ComboBoxDelegate(combobox_items, self))
        self.treeView.setEditTriggers(
            # QAbstractItemView.AllEditTriggers
            QAbstractItemView.CurrentChanged |
            QAbstractItemView.SelectedClicked |
            QAbstractItemView.DoubleClicked |
            QAbstractItemView.EditKeyPressed |
            QAbstractItemView.AnyKeyPressed
        )
        self.treeView.setItemDelegateForColumn(2, ButtonDelegate(self))
        self.treeView.setMouseTracking(True)

        self.comboBox.setCurrentIndex(self.comboBox.findText(settings.media_sync_default_source.get()))
        self.stop_audio_automatically_checkbox.setChecked(settings.media_sync_stop_audio_automatically.get())

        header = self.treeView.header()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Fixed)
        header.resizeSection(1, 160)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Fixed)
        header.resizeSection(2, 50)

        self.add_funscript_button.clicked.connect(self.open_add_funscripts_dialog)
        self.additional_search_paths_button.clicked.connect(self.open_search_paths_dialog)
        self.reload_scripts_button.clicked.connect(self.reload_scripts)

        # Variant selector: shown only when a sibling <scene>_variants/ folder exists.
        self.available_variants: list[tuple[str, str]] = []
        self.active_variant: str | None = None
        self.variant_widget = QWidget(self.widget_3)
        variant_layout = QHBoxLayout(self.variant_widget)
        variant_layout.setContentsMargins(0, 0, 0, 0)
        self.variant_label = QLabel("Variant:", self.variant_widget)
        self.variant_combobox = QComboBox(self.variant_widget)
        variant_layout.addWidget(self.variant_label)
        variant_layout.addWidget(self.variant_combobox)
        self.horizontalLayout_2.insertWidget(self.horizontalLayout_2.count() - 1, self.variant_widget)
        self.variant_widget.setVisible(False)
        self.variant_combobox.currentIndexChanged.connect(self.on_variant_changed)

        self.media_index_changed()

        self.media_offset_spinbox.valueChanged.connect(self.refresh_media_offset)
        self.media_offset_spinbox.setValue(settings.media_sync_time_offset_ms.get() / 1000.0)

    def open_add_funscripts_dialog(self):
        self.dialogOpened.emit()  # trigger stop audio

        dlg = FileDialog()
        dlg.setFileMode(QFileDialog.ExistingFiles)
        dlg.setNameFilters(["*.funscript"])

        if dlg.exec():
            self.model.beginResetModel()
            filenames = dlg.selectedFiles()
            kit = funscript_kit.FunscriptKitModel.load_from_settings()
            for filename in filenames:
                item = FunscriptTreeItem(Resource(pathlib.Path(filename)))
                self.model.auto_link_funscript(kit, item)
                self.model.add_funscript_resource_manual(item)
            self.model.rebuild()
            self.model.endResetModel()
            self.treeView.expandAll()
            self.funscriptMappingChanged.emit()

    def open_search_paths_dialog(self):
        self.dialogOpened.emit()  # trigger stop audio
        dlg = AdditionalSearchPathsDialog()
        if dlg.exec():
            # Attempt to re-load funscripts
            self.detect_resources_for_media_file(self.loaded_media_path)

    def reload_scripts(self):
        self.detect_resources_for_media_file(self.loaded_media_path)

    def on_data_changed(self, topleft, bottomright, roles):
        if Qt.EditRole in roles or Qt.CheckStateRole in roles:
            # print('on data changed', roles, topleft.row(), bottomright.row())
            self.funscriptMappingChanged.emit()

    def _on_model_reset(self):
        self.treeView.expandAll()
        self.funscriptMappingChanged.emit()

    def media_index_changed(self):
        self.dialogOpened.emit()  # stop audio before possibly modifying important vars.

        self.model.beginResetModel()
        any_funscripts_removed = self.model.clear_auto_detected_funscripts()
        self.model.rebuild()
        self.model.endResetModel()
        old_interface = self.media_sync[self.current_index]
        new_interface = self.media_sync[self.comboBox.currentIndex()]
        self.current_index = self.comboBox.currentIndex()
        old_interface.disable()
        new_interface.enable()
        self.refresh_connection_status()
        self.refresh_media_offset()

        # emit because there is a chance we switched to/from internal
        # media, which does not support funscripts
        self.funscriptMappingChanged.emit()

    def connection_status_changed(self, index: int):
        if index == self.current_index:
            self.refresh_connection_status()

    def refresh_connection_status(self):
        self.connectionStatusChanged.emit(self.media_sync[self.current_index].state())
        connector = self.media_sync[self.current_index]
        if connector.is_internal():
            self.connection_status.setText('Connected')
            self.lineEdit.clear()
        elif connector.is_media_loaded():
            self.connection_status.setText('Connected')
            a, b = os.path.split(connector.media_path())
            self.lineEdit.setText(b)
        elif connector.is_connected():
            self.connection_status.setText('Connected, no file loaded.')
            self.lineEdit.clear()
        else:
            self.model.beginResetModel()
            self.model.clear_auto_detected_funscripts()
            self.model.rebuild()
            self.model.endResetModel()
            self.connection_status.setText('Attempting to connect...')
            self.lineEdit.clear()

        new_path = connector.media_path()

        if self.loaded_media_path != new_path:
            self.detect_resources_for_media_file(new_path)

    def detect_resources_for_media_file(self, new_path):
        self.loaded_media_path = new_path

        variants = detect_variant_folders(new_path) if new_path else []
        self._update_variant_selector(variants)

        dirty = False
        if not new_path:
            # path is empty string.
            self.model.beginResetModel()
            dirty |= self.model.clear_auto_detected_funscripts()
            self.model.rebuild()
            self.model.endResetModel()
        else:
            # path is something
            dirname = os.path.dirname(new_path)
            basename = os.path.basename(new_path)
            extra_paths = settings.additional_search_paths.get()

            variant_path = dict(variants).get(self.active_variant) if self.active_variant else None
            if variant_path:
                # Scope to the selected variant so sibling scripts in the scene
                # folder do not leak in. collect_funscripts stops at the first
                # directory that yields matches anyway, but being explicit here
                # makes the behavior obvious to the user.
                search_paths = [variant_path]
            else:
                search_paths = [dirname] + extra_paths

            self.model.beginResetModel()
            dirty |= self.model.clear_auto_detected_funscripts()
            dirty |= self.model.detect_funscripts_from_path(search_paths, basename)
            # Auto-link must happen before rebuild so items land in the
            # correct axis category on first render.
            self.model.auto_link_funscripts(funscript_kit.FunscriptKitModel.load_from_settings())
            self.model.rebuild()
            self.model.endResetModel()

        self.treeView.expandAll()
        self.funscriptMappingChanged.emit()

    def _update_variant_selector(self, variants: list[tuple[str, str]]):
        self.available_variants = variants
        self.variant_combobox.blockSignals(True)
        self.variant_combobox.clear()
        if variants:
            letters = [letter for letter, _ in variants]
            for letter in letters:
                self.variant_combobox.addItem(letter)
            if self.active_variant in letters:
                idx = letters.index(self.active_variant)
            else:
                idx = 0
                self.active_variant = letters[0]
            self.variant_combobox.setCurrentIndex(idx)
            self.variant_widget.setVisible(True)
        else:
            self.active_variant = None
            self.variant_widget.setVisible(False)
        self.variant_combobox.blockSignals(False)

    def on_variant_changed(self, index: int):
        if index < 0 or index >= len(self.available_variants):
            return
        new_variant = self.available_variants[index][0]
        if new_variant == self.active_variant:
            return
        self.active_variant = new_variant
        self.dialogOpened.emit()  # stop audio before rebuilding the axis set
        self.detect_resources_for_media_file(self.loaded_media_path)

    def select_variant_by_letter(self, letter: str) -> bool:
        """Select variant by letter (e.g. 'A'). Returns True on success."""
        letters = [l for l, _ in self.available_variants]
        if letter not in letters:
            return False
        self.variant_combobox.setCurrentIndex(letters.index(letter))
        return True

    def has_media_file_loaded(self):
        return bool(self.loaded_media_path)

    def autostart_enabled(self):
        return not self.stop_audio_automatically_checkbox.isChecked()

    def is_connected(self):
        media = self.media_sync[self.current_index]
        return media.is_connected()

    def is_playing(self):
        media = self.media_sync[self.current_index]
        return media.is_playing()

    def is_internal(self):
        media = self.media_sync[self.current_index]
        return media.is_internal()

    def current_media_sync(self):
        return self.media_sync[self.current_index]

    def refresh_media_offset(self):
        offset = self.media_offset_spinbox.value()
        media = self.media_sync[self.current_index]
        media.set_media_sync_offset(offset)

    def save_settings(self):
        settings.media_sync_time_offset_ms.set(self.media_offset_spinbox.value() * 1000)

    dialogOpened = QtCore.Signal()  # emitted whenever a dialog is opened which promps audio stop.
    connectionStatusChanged = QtCore.Signal(MediaConnectionState)  # emitted whenever video player connection status changes.
    funscriptMappingChanged = QtCore.Signal()  # emitted whenever new funscript files are added, removed or modified