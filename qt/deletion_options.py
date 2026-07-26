# Created By: Virgil Dupras
# Created On: 2012-05-30
# Copyright 2015 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox

from hscommon.trans import trget

tr = trget("ui")


class DeletionOptions(QDialog):
    def __init__(self, parent, model, **kwargs):
        flags = Qt.WindowType.CustomizeWindowHint | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowSystemMenuHint
        super().__init__(parent, flags, **kwargs)
        self.model = model
        self._setupUi()
        self.model.view = self

        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.rejected.connect(self.reject)

    def _setupUi(self):
        self.setWindowTitle(tr("Deletion Options"))
        self.resize(400, 270)
        self.verticalLayout = QVBoxLayout(self)
        self.msgLabel = QLabel()
        self.msgLabel.setWordWrap(True)
        self.verticalLayout.addWidget(self.msgLabel)
        text = tr(
            "This action only moves files into recoverable quarantine. "
            "Permanent finalization is always a separate explicit operation."
        )
        self.safetyMessageLabel = QLabel(text)
        self.safetyMessageLabel.setWordWrap(True)
        self.verticalLayout.addWidget(self.safetyMessageLabel)
        self.buttonBox = QDialogButtonBox()
        self.buttonBox.addButton(tr("Proceed"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttonBox.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        self.verticalLayout.addWidget(self.buttonBox)

    # --- model --> view
    def update_msg(self, msg: str):
        self.msgLabel.setText(msg)

    def show(self):
        result = self.exec()
        return result == QDialog.DialogCode.Accepted
