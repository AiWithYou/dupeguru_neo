# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Canonical file extensions and picker filters used by the Qt application."""

from hscommon.trans import tr

RESULTS_EXTENSION = ".dupeguru"
DIRECTORIES_EXTENSION = ".dupegurudirs"

RESULTS_FILTER = "dupeGuru Neo Results (*.dupeguru)"
DIRECTORIES_FILTER = "dupeGuru Neo Directories (*.dupegurudirs)"


def translated_results_filter() -> str:
    """Return the results picker filter in the active UI language."""

    return tr("dupeGuru Neo Results (*.dupeguru)")


def translated_directories_filter() -> str:
    """Return the directories picker filter in the active UI language."""

    return tr("dupeGuru Neo Directories (*.dupegurudirs)")


def ensure_extension(path: str, extension: str) -> str:
    """Append *extension* exactly once, using case-insensitive suffix matching."""

    if not extension.startswith(".") or extension == ".":
        raise ValueError("extension must start with a dot and contain a suffix")
    return path if path.casefold().endswith(extension.casefold()) else "{}{}".format(path, extension)
