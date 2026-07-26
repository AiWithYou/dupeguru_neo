import importlib
import os
import pkgutil

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QHeaderView  # noqa: E402

import qt  # noqa: E402
from qt.column import Column  # noqa: E402
from qt.search_edit import SearchEdit  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


def test_all_qt_modules_import_with_pyqt6():
    optional_extension_modules = {"qt.pe.block"}
    module_names = {
        module_info.name
        for module_info in pkgutil.walk_packages(qt.__path__, prefix="qt.")
        if module_info.name not in optional_extension_modules
    }
    for module_name in sorted(module_names):
        importlib.import_module(module_name)


def test_scoped_enums_work_in_representative_widgets(qapp):
    search_edit = SearchEdit()
    search_edit.show()
    qapp.processEvents()
    assert search_edit._clearButton.cursor().shape() == Qt.CursorShape.ArrowCursor

    header = QHeaderView(Qt.Orientation.Horizontal)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft)
    column = Column("name", 100)
    assert column.alignment == Qt.AlignmentFlag.AlignLeft

    search_edit.close()
    header.close()
