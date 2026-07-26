from pathlib import Path

from PyQt6.QtCore import QSettings

from core.directories import Directories, DirectoryState
from core.gui.directory_tree import STATE_ORDER, DirectoryTree
from qt.directories_model import STATES
from qt.preferences import Preferences


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
        "Incoming Files",
        "Protected Library",
        "Compare Only",
        "Excluded",
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
