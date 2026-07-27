# Copyright 2016 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import os.path as op
import sys

from hscommon.plat import ISWINDOWS, ISOSX, ISLINUX


def _application_base_path(module_file, frozen_base):
    """Return the directory that contains bundled locale and help data."""

    if frozen_base is not None:
        return op.abspath(frozen_base)
    # In a source or installed Python layout, qt/ is directly below the
    # application data root.
    return op.abspath(op.join(op.dirname(module_file), ".."))


# PyInstaller exposes its data root through ``sys._MEIPASS``. Frozen Python
# modules live in the embedded PYZ, so their synthetic ``__file__`` path need
# not exist on disk. Testing ``exists(__file__)`` therefore selected the
# process working directory and left application gettext catalogs unloaded.
BASE_PATH = _application_base_path(__file__, getattr(sys, "_MEIPASS", None))
HELP_PATH = op.join(BASE_PATH, "help", "en")


def localized_help_path(language):
    """Return the bundled help root selected by the application language."""

    if language == "ja":
        return op.join(BASE_PATH, "help", "ja")
    return HELP_PATH


if ISWINDOWS:
    INITIAL_FOLDER_IN_DIALOGS = "C:\\"
elif ISOSX:
    INITIAL_FOLDER_IN_DIALOGS = "/"
elif ISLINUX:
    INITIAL_FOLDER_IN_DIALOGS = "/"
else:
    # unsupported platform, however '/' is a good guess for a path which is available
    INITIAL_FOLDER_IN_DIALOGS = "/"
