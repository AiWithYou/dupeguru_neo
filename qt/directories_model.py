# Created By: Virgil Dupras
# Created On: 2009-04-25
# Copyright 2015 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

from PyQt6.QtCore import pyqtSignal, Qt, QUrl, QModelIndex, QItemSelection
from PyQt6.QtWidgets import (
    QComboBox,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionComboBox,
    QStyleOptionViewItem,
    QApplication,
)
from PyQt6.QtGui import QBrush

from hscommon.trans import trget
from qt.tree_model import RefNode, TreeModel

tr = trget("ui")

HEADERS = [tr("Name"), tr("State")]
STATES = [
    tr("Organize"),
    tr("Keep all files"),
    tr("Compare only"),
    tr("Skip"),
]
STATE_DESCRIPTIONS = [
    tr("Duplicates in this folder can be checked and quarantined."),
    tr("Files in this folder are always kept and used as references."),
    tr("Files in this folder are compared, but never changed."),
    tr("This folder is not scanned."),
]


class DirectoriesDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = QComboBox(parent)
        editor.addItems(STATES)
        return editor

    def paint(self, painter, option, index):
        self.initStyleOption(option, index)
        # Draw every handling cell as a combo box so that its editability is
        # visible before the row is selected.
        option = QStyleOptionViewItem(option)
        if index.column() == 1:
            cboption = QStyleOptionComboBox()
            cboption.rect = option.rect
            cboption.palette = option.palette
            cboption.state = option.state | QStyle.StateFlag.State_Enabled
            cboption.currentText = option.text
            cboption.frame = True
            style = option.widget.style() if option.widget is not None else QApplication.style()
            style.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, cboption, painter)
            style.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, cboption, painter)
        else:
            super().paint(painter, option, index)

    def setEditorData(self, editor, index):
        value = index.model().data(index, Qt.ItemDataRole.EditRole)
        editor.setCurrentIndex(value)
        editor.showPopup()

    def setModelData(self, editor, model, index):
        value = editor.currentIndex()
        model.setData(index, value, Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


class DirectoriesModel(TreeModel):
    MIME_TYPE_FORMAT = "text/uri-list"

    def __init__(self, model, view, **kwargs):
        super().__init__(**kwargs)
        self.model = model
        self.model.view = self
        self.view = view
        self.view.setModel(self)

        self.view.selectionModel().selectionChanged[(QItemSelection, QItemSelection)].connect(self.selectionChanged)

    def _create_node(self, ref, row):
        return RefNode(self, None, ref, row)

    def _get_children(self):
        return list(self.model)

    def columnCount(self, parent=QModelIndex()):
        return 2

    def data(self, index, role):
        if not index.isValid():
            return None
        node = index.internalPointer()
        ref = node.ref
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return ref.name
            else:
                return STATES[ref.state]
        elif role == Qt.ItemDataRole.EditRole and index.column() == 1:
            return ref.state
        elif role == Qt.ItemDataRole.ToolTipRole:
            if index.column() == 0:
                return ref.name
            return STATE_DESCRIPTIONS[ref.state]
        elif role == Qt.ItemDataRole.ForegroundRole:
            state = ref.state
            if state == 1:
                return QBrush(Qt.GlobalColor.blue)
            elif state == 2:
                return QBrush(Qt.GlobalColor.darkCyan)
            elif state == 3:
                return QBrush(Qt.GlobalColor.red)
        return None

    def dropMimeData(self, mime_data, action, row, column, parent_index):
        # the data in mimeData is urlencoded **in utf-8**
        if not mime_data.hasFormat(self.MIME_TYPE_FORMAT):
            return False
        data = bytes(mime_data.data(self.MIME_TYPE_FORMAT)).decode("ascii")
        urls = data.split("\r\n")
        paths = [QUrl(url).toLocalFile() for url in urls if url]
        for path in paths:
            self.model.add_directory(path)
        self.foldersAdded.emit(paths)
        self.refresh()
        return True

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled
        result = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDropEnabled
        if index.column() == 1:
            result |= Qt.ItemFlag.ItemIsEditable
        return result

    def headerData(self, section, orientation, role):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole and section < len(HEADERS):
            return HEADERS[section]
        return None

    def mimeTypes(self):
        return [self.MIME_TYPE_FORMAT]

    def setData(self, index, value, role):
        if not index.isValid() or role != Qt.ItemDataRole.EditRole or index.column() != 1:
            return False
        node = index.internalPointer()
        ref = node.ref
        ref.state = value
        return True

    def supportedDropActions(self):
        # Normally, the correct action should be ActionLink, but the drop doesn't work. It doesn't
        # work with ActionMove either. So screw that, and accept anything.
        return Qt.DropAction.ActionMask

    # --- Events
    def selectionChanged(self, selected, deselected):
        new_nodes = [modelIndex.internalPointer().ref for modelIndex in self.view.selectionModel().selectedRows()]
        self.model.selected_nodes = new_nodes

    # --- Signals
    foldersAdded = pyqtSignal(list)
    contentsChanged = pyqtSignal()

    # --- model --> view
    def refresh(self):
        self.reset()
        self.contentsChanged.emit()

    def refresh_states(self):
        self.refreshData()
        self.contentsChanged.emit()
