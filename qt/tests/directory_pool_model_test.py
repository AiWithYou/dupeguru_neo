from pathlib import Path

from PyQt6.QtCore import QSettings

from core.directories import Directories, DirectoryState
from core.gui.directory_tree import STATE_ORDER, DirectoryTree
from qt.directories_model import STATES, STATE_DESCRIPTIONS, DirectoriesModel
from qt.preferences import Preferences
from qt.stats_label import StatsLabel


class _TreeView:
    def refresh(self):
        pass

    def refresh_states(self):
        pass


class _App:
    def __init__(self, root):
        self.directories = Directories()
        self.directories.add_path(Path(root))
        self.directory_tree = None

    def add_directory(self, path):
        self.directories.add_path(Path(path))

    def remove_directories(self, indexes):
        for index in sorted(indexes, reverse=True):
            del self.directories[index]


def test_all_comparison_pools_have_an_explicit_ui_state(tmp_path):
    assert STATE_ORDER == [
        DirectoryState.NORMAL,
        DirectoryState.REFERENCE,
        DirectoryState.COMPARE_ONLY,
        DirectoryState.EXCLUDED,
    ]
    assert STATES == [
        "Organize",
        "Keep all files",
        "Compare only",
        "Skip",
    ]
    assert STATE_DESCRIPTIONS == [
        "Duplicates in this folder can be checked and quarantined.",
        "Files in this folder are always kept and used as references.",
        "Files in this folder are compared, but never changed.",
        "This folder is not scanned.",
    ]

    app = _App(tmp_path)
    tree = DirectoryTree(app)
    app.directory_tree = tree
    tree.view = _TreeView()
    tree._refresh()

    node = tree[0]
    node.state = STATE_ORDER.index(DirectoryState.COMPARE_ONLY)
    assert app.directories.get_state(Path(tmp_path)) == DirectoryState.COMPARE_ONLY
    assert node.state == STATE_ORDER.index(DirectoryState.COMPARE_ONLY)


def test_remove_selected_toggles_nested_nodes_using_ui_indexes(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    app = _App(tmp_path)
    tree = DirectoryTree(app)
    app.directory_tree = tree
    tree.view = _TreeView()
    tree._refresh()

    root_node = tree[0]
    assert len(root_node) == 1
    node = root_node[0]
    tree.selected_nodes = [node]
    tree.remove_selected()
    assert app.directories.get_state(child) == DirectoryState.EXCLUDED

    tree.selected_nodes = [node]
    tree.remove_selected()
    assert app.directories.get_state(child) == DirectoryState.NORMAL


def test_cross_pool_preference_is_persisted_explicitly(tmp_path):
    settings_path = tmp_path / "settings.ini"
    preferences = Preferences()
    preferences._settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    preferences.load()
    preferences.cross_pool_only = True
    preferences.save()

    loaded = Preferences()
    loaded._settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    loaded.load()

    assert loaded.cross_pool_only is True


def test_stats_label_can_refresh_a_workflow_summary():
    class Model:
        display = "1 / 2 duplicates checked"
        view = None

    class View:
        text = ""

        def setText(self, text):
            self.text = text

    model = Model()
    view = View()
    refreshed = []
    label = StatsLabel(model, view, on_refresh=refreshed.append)

    label.refresh()

    assert view.text == model.display
    assert refreshed == [model.display]


def test_dropping_a_folder_refreshes_workflow_controls():
    class MimeData:
        def hasFormat(self, mime_type):
            return mime_type == DirectoriesModel.MIME_TYPE_FORMAT

        def data(self, mime_type):
            return b"file:///C:/scan-target\r\n"

    class Signal:
        emitted = None

        def emit(self, *values):
            self.emitted = values

    class Model:
        added = None

        def add_directory(self, path):
            self.added = path

    class Adapter:
        MIME_TYPE_FORMAT = DirectoriesModel.MIME_TYPE_FORMAT
        model = Model()
        foldersAdded = Signal()
        contentsChanged = Signal()
        reset_called = False

        def reset(self):
            self.reset_called = True

        refresh = DirectoriesModel.refresh

    adapter = Adapter()

    assert DirectoriesModel.dropMimeData(adapter, MimeData(), None, 0, 0, None)
    assert adapter.model.added
    assert adapter.foldersAdded.emitted == (["C:/scan-target"],)
    assert adapter.reset_called is True
    assert adapter.contentsChanged.emitted == ()
