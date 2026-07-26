import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtGui import QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from qt.resources import RESOURCE_FILES, ResourceError, resource_path  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


def test_every_declared_resource_exists_and_decodes(qapp):
    for alias in sorted(RESOURCE_FILES):
        path = Path(resource_path(alias))
        assert path.is_file()
        assert not QPixmap(str(path)).isNull(), alias


def test_unknown_alias_fails_clearly():
    with pytest.raises(ResourceError, match="Unknown"):
        resource_path("not-a-resource")
