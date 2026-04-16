from __future__ import annotations  # multiple return values

import json.decoder
import typing
import logging

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt
from PySide6.QtGui import QColor

import qt_ui.device_wizard
import qt_ui.device_wizard.axes
from qt_ui.device_wizard.axes import AxisEnum
from qt_ui.models.tree_item import TreeItem
from qt_ui.models.funscript_kit import FunscriptKitModel
from funscript.funscript import Funscript
import funscript.collect_funscripts

logger = logging.getLogger('restim.script_mapping')


CATEGORY_3_PHASE = 'Position (3-phase)'
CATEGORY_4_PHASE = 'Intensity (4-phase)'
CATEGORY_PULSE = 'Pulse & Frequency'
CATEGORY_VOLUME = 'Volume'
CATEGORY_VIBRATION = 'Vibration'
CATEGORY_UNMAPPED = 'Unmapped'

CATEGORY_ORDER = [
    CATEGORY_3_PHASE,
    CATEGORY_4_PHASE,
    CATEGORY_PULSE,
    CATEGORY_VOLUME,
    CATEGORY_VIBRATION,
    CATEGORY_UNMAPPED,
]

AXIS_TO_CATEGORY = {
    AxisEnum.POSITION_ALPHA: CATEGORY_3_PHASE,
    AxisEnum.POSITION_BETA: CATEGORY_3_PHASE,
    AxisEnum.POSITION_GAMMA: CATEGORY_3_PHASE,

    AxisEnum.INTENSITY_A: CATEGORY_4_PHASE,
    AxisEnum.INTENSITY_B: CATEGORY_4_PHASE,
    AxisEnum.INTENSITY_C: CATEGORY_4_PHASE,
    AxisEnum.INTENSITY_D: CATEGORY_4_PHASE,

    AxisEnum.CARRIER_FREQUENCY: CATEGORY_PULSE,
    AxisEnum.PULSE_FREQUENCY: CATEGORY_PULSE,
    AxisEnum.PULSE_WIDTH: CATEGORY_PULSE,
    AxisEnum.PULSE_RISE_TIME: CATEGORY_PULSE,
    AxisEnum.PULSE_INTERVAL_RANDOM: CATEGORY_PULSE,
    AxisEnum.TAU: CATEGORY_PULSE,

    AxisEnum.VOLUME_API: CATEGORY_VOLUME,
    AxisEnum.VOLUME_EXTERNAL: CATEGORY_VOLUME,
    AxisEnum.VOLUME_MASTER: CATEGORY_VOLUME,
    AxisEnum.VOLUME_INACTIVITY: CATEGORY_VOLUME,

    AxisEnum.VIBRATION_1_FREQUENCY: CATEGORY_VIBRATION,
    AxisEnum.VIBRATION_1_STRENGTH: CATEGORY_VIBRATION,
    AxisEnum.VIBRATION_1_LEFT_RIGHT_BIAS: CATEGORY_VIBRATION,
    AxisEnum.VIBRATION_1_HIGH_LOW_BIAS: CATEGORY_VIBRATION,
    AxisEnum.VIBRATION_1_RANDOM: CATEGORY_VIBRATION,
    AxisEnum.VIBRATION_2_FREQUENCY: CATEGORY_VIBRATION,
    AxisEnum.VIBRATION_2_STRENGTH: CATEGORY_VIBRATION,
    AxisEnum.VIBRATION_2_LEFT_RIGHT_BIAS: CATEGORY_VIBRATION,
    AxisEnum.VIBRATION_2_HIGH_LOW_BIAS: CATEGORY_VIBRATION,
    AxisEnum.VIBRATION_2_RANDOM: CATEGORY_VIBRATION,

    AxisEnum.NONE: CATEGORY_UNMAPPED,
}


class HeaderTreeItem(TreeItem):
    def __init__(self, parent: TreeItem = None):
        super(HeaderTreeItem, self).__init__(parent)

    def data(self, column):
        items = ['resource', 'target', 'actions']
        return items[column]


class ResourceCategory(TreeItem):
    def __init__(self, name: str, parent: TreeItem = None):
        super(ResourceCategory, self).__init__(parent)

        self.name = name

    def data(self, column):
        if column == 0:
            return self.name
        return None


class FunscriptTreeItem(TreeItem):
    def __init__(self, resource: funscript.collect_funscripts.Resource, parent: TreeItem = None):
        super(FunscriptTreeItem, self).__init__(parent)

        # self.resource = resource
        self.file_name = resource.name()
        self.funscript_type = resource.funscript_type()
        self.axis = qt_ui.device_wizard.axes.AxisEnum.NONE
        self.is_removable = False
        self.enabled = True

        # load funscript immediately. This makes the UI feel sluggish on connecting/disconnecting
        # but has the advantage of not requiring any file IO when audio starts.
        try:
            self.script = Funscript.from_file(resource.path)
        except json.decoder.JSONDecodeError as e:
            logger.error(f'Unable to parse funscript, broken? {resource.path}')
            self.script = None

    def has_broken_script(self) -> bool:
        return self.script is None

    def data(self, column):
        if column == 0:
            return self.file_name
        if column == 1:
            if self.has_broken_script():
                return "funscript loading failed"
            return self.axis.display_name()
        if column == 2:
            return self.is_removable

    def edit_data(self, column):
        if column == 0:
            return self.file_name
        if column == 1:
            return self.axis
        if column == 2:
            return self.is_removable


class TCodeTreeItem(TreeItem):
    def __init__(self, axis: str, parent: TreeItem = None):
        super(TCodeTreeItem, self).__init__(parent)

        self.axis = axis

    def data(self, column):
        if column == 0:
            return self.axis
        return 'todo'


class ScriptMappingModel(QAbstractItemModel):
    def __init__(self):
        super(ScriptMappingModel, self).__init__()

        self._root = HeaderTreeItem()
        # Flat source of truth. The visible tree is rebuilt from this list,
        # grouped into axis categories, sorted by canonical axis order.
        self._items: list[FunscriptTreeItem] = []

        # Suffixes the user has disabled (e.g. 'e1', 'alpha'). Persisted across
        # auto-detect rebuilds (e.g. variant switches) so the user's A/B channel
        # selection survives a swap.
        self._disabled_suffixes: set[str] = set()

        self._rebuild_tree()

    # ---- internal helpers ------------------------------------------------

    @staticmethod
    def _category_for_axis(axis: AxisEnum) -> str:
        return AXIS_TO_CATEGORY.get(axis, CATEGORY_UNMAPPED)

    def _rebuild_tree(self):
        """Rebuild category tree from the flat `_items` list. Caller must wrap
        in beginResetModel/endResetModel if views are observing.
        """
        self._root.children.clear()
        grouped: dict[str, list[FunscriptTreeItem]] = {}
        for item in self._items:
            cat_name = self._category_for_axis(item.axis)
            grouped.setdefault(cat_name, []).append(item)
        for cat_name in CATEGORY_ORDER:
            items = grouped.get(cat_name, [])
            if not items:
                continue
            items.sort(key=lambda i: (i.axis.value, i.file_name.lower()))
            cat = ResourceCategory(cat_name, self._root)
            for item in items:
                item.parent = cat
                cat.appendChild(item)
            self._root.appendChild(cat)

    # ---- QAbstractItemModel API -----------------------------------------

    def data(self, index: QModelIndex, role: int = ...) -> typing.Any:
        if not index.isValid():
            return None

        if role == Qt.CheckStateRole:
            if isinstance(index.internalPointer(), FunscriptTreeItem) and index.column() == 0:
                item: FunscriptTreeItem = index.internalPointer()
                return Qt.Checked if item.enabled else Qt.Unchecked
            return None
        if role == Qt.EditRole:
            item = index.internalPointer()
            return item.edit_data(index.column())
        elif role == Qt.DisplayRole:
            item = index.internalPointer()
            return item.data(index.column())
        elif role == Qt.ForegroundRole:
            if isinstance(index.internalPointer(), FunscriptTreeItem):
                if index.internalPointer().has_broken_script():
                    return QColor(255, 0, 0)
            return None
        else:
            return None

    def setData(self, index: QModelIndex, value: typing.Any, role: int = ...) -> bool:
        if role == Qt.CheckStateRole:
            if isinstance(index.internalPointer(), FunscriptTreeItem) and index.column() == 0:
                item: FunscriptTreeItem = index.internalPointer()
                new_enabled = Qt.CheckState(value) == Qt.Checked
                if item.enabled != new_enabled:
                    item.enabled = new_enabled
                    if item.funscript_type:
                        if new_enabled:
                            self._disabled_suffixes.discard(item.funscript_type)
                        else:
                            self._disabled_suffixes.add(item.funscript_type)
                    self.dataChanged.emit(index, index, [role])
                return True
        if role == Qt.EditRole:
            if isinstance(index.internalPointer(), FunscriptTreeItem):
                if index.column() == 1:
                    item: FunscriptTreeItem = index.internalPointer()
                    if item.axis != value:
                        # Axis change may move the item to a different category
                        # and/or alter the sort order within its category, so
                        # rebuild the tree under a reset.
                        self.beginResetModel()
                        item.axis = value
                        self._rebuild_tree()
                        self.endResetModel()
                        return True
        return False

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.NoItemFlags

        if index.column() == 0:
            if isinstance(index.internalPointer(), FunscriptTreeItem):
                if index.internalPointer().has_broken_script():
                    return Qt.ItemIsEnabled
                return Qt.ItemIsEnabled | Qt.ItemIsUserCheckable
            return Qt.ItemIsEnabled
        elif index.column() == 1:
            if isinstance(index.internalPointer(), ResourceCategory):
                return Qt.ItemIsEnabled
            if isinstance(index.internalPointer(), FunscriptTreeItem):
                if index.internalPointer().has_broken_script():
                    return Qt.NoItemFlags
                else:
                    return Qt.ItemIsEnabled | Qt.ItemIsEditable
            return Qt.ItemIsEnabled | Qt.ItemIsEditable
        elif index.column() == 2:
            return Qt.ItemIsEnabled

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = ...) -> typing.Any:
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self._root.data(section)
        return None

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        if not parent.isValid():
            parentItem = self._root
        else:
            parentItem = parent.internalPointer()

        childItem = parentItem.child(row)
        if childItem:
            return self.createIndex(row, column, childItem)
        return QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()

        childItem = index.internalPointer()
        parentItem = childItem.parentItem()

        if parentItem is None or parentItem == self._root:
            return QModelIndex()

        # Find parentItem's row within its own parent (the root).
        grandparent = parentItem.parentItem() or self._root
        try:
            row = grandparent.children.index(parentItem)
        except ValueError:
            row = 0
        return self.createIndex(row, 0, parentItem)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.column() > 0:
            return 0

        if not parent.isValid():
            parentItem = self._root
        else:
            parentItem = parent.internalPointer()

        return parentItem.childCount()

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return parent.internalPointer().columnCount()
        return self._root.columnCount()

    def removeRow(self, row: int, parent: QModelIndex = ...) -> bool:
        if not parent.isValid():
            return False
        parent_item = parent.internalPointer()
        if not isinstance(parent_item, ResourceCategory):
            return False
        if row < 0 or row >= len(parent_item.children):
            return False
        item = parent_item.children[row]
        self.beginResetModel()
        if item in self._items:
            self._items.remove(item)
        self._rebuild_tree()
        self.endResetModel()
        return True

    # ---- public mutators used by MediaSettingsWidget --------------------

    def add_funscript_resource_auto(self, item: FunscriptTreeItem):
        """Append an auto-detected item. Caller should wrap in beginResetModel
        / endResetModel if adding in a batch; otherwise call `rebuild()` after.
        """
        if item.funscript_type in self._disabled_suffixes:
            item.enabled = False
        self._items.append(item)

    def add_funscript_resource_manual(self, item: FunscriptTreeItem):
        """Append a user-added item. Caller should wrap in beginResetModel /
        endResetModel if adding in a batch; otherwise call `rebuild()` after.
        """
        item.is_removable = True
        self._items.append(item)

    def rebuild(self):
        """Force a tree rebuild. Use after batch mutations inside a
        beginResetModel / endResetModel block.
        """
        self._rebuild_tree()

    def funscript_conifg(self) -> list[FunscriptTreeItem]:
        return list(self._items)

    def get_config_for_axis(self, axis: AxisEnum) -> FunscriptTreeItem | None:
        # Return the first enabled item matching the axis. Items are iterated
        # in insertion order so manual adds (which come first on load) take
        # precedence over auto-detected duplicates.
        for funscript in self._items:
            if funscript.axis == axis and funscript.enabled:
                return funscript
        return None

    def detect_funscripts_from_path(self, search_directories: [str], media_file: str) -> bool:
        """
        :param search_directories: list of strings
        :param media_file: "video.mp4"
        :return: True if the item set changed
        """
        auto_items = [i for i in self._items if not i.is_removable]
        dirty = bool(auto_items)
        self._items = [i for i in self._items if i.is_removable]

        resources = funscript.collect_funscripts.collect_funscripts(search_directories, media_file)
        for res in resources:
            new_item = FunscriptTreeItem(res)
            self.add_funscript_resource_auto(new_item)
            dirty = True
        return dirty

    def clear_auto_detected_funscripts(self) -> bool:
        auto_items = [i for i in self._items if not i.is_removable]
        if not auto_items:
            return False
        self._items = [i for i in self._items if i.is_removable]
        return True

    def auto_link_funscripts(self, kit: FunscriptKitModel) -> None:
        for item in self._items:
            self.auto_link_funscript(kit, item)

    def auto_link_funscript(self, kit: FunscriptKitModel, item: FunscriptTreeItem):
        if item.has_broken_script():
            return

        suffix = item.funscript_type
        if suffix:
            for kit_item in kit.funscript_conifg():
                if kit_item.auto_loading and kit_item.allow_funscript_control:
                    if suffix in kit_item.funscript_names:
                        item.axis = kit_item.axis
                        logger.info(f'auto-linking `{item.file_name}` to {kit_item.axis.display_name()}.')
                        return
        logger.info(f'auto-linking `{item.file_name}` failed')
