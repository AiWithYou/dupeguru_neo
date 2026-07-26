# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

"""Internal directory names that must never become scan inputs."""

import os
import re
import unicodedata

from pathlib import Path

RESERVED_INTERNAL_DIRECTORY_NAMES = frozenset(
    {
        ".dupeguru-neo-quarantine",
        ".dupeguru-neo-dataset-executor",
        ".dupeguru-neo-dataset-quarantine",
    }
)


def _name_key(name: str) -> str:
    normalized = unicodedata.normalize("NFC", os.path.normcase(name))
    if os.name == "nt":
        # Alternate data stream spellings can alias a directory stream.
        normalized = normalized.split(":", 1)[0]
        # Win32 resolves trailing dots and spaces to the same directory name
        # unless a caller deliberately opts into special device-path syntax.
        # Treat those spellings as aliases at every public safety boundary.
        normalized = normalized.rstrip(" .")
    return normalized.casefold()


_WINDOWS_INVALID_COMPONENT_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_DEVICE_COMPONENTS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {"com{}".format(index) for index in range(1, 10)}
    | {"lpt{}".format(index) for index in range(1, 10)}
)


def is_unsafe_windows_path_component(name: str) -> bool:
    """Return whether ``name`` is not a normal Win32 directory component."""

    if os.name != "nt":
        return False
    if (
        not name
        or name.rstrip(" .") != name
        or any(character in _WINDOWS_INVALID_COMPONENT_CHARACTERS for character in name)
        or any(ord(character) < 32 for character in name)
    ):
        return True
    device_stem = name.split(".", 1)[0].casefold()
    return device_stem in _WINDOWS_DEVICE_COMPONENTS


def is_unsafe_path_component(name: str) -> bool:
    """Return whether ``name`` cannot be used as one ordinary path component."""

    return (
        not isinstance(name, str)
        or not name
        or "\0" in name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or is_unsafe_windows_path_component(name)
    )


_RESERVED_KEYS = frozenset(_name_key(name) for name in RESERVED_INTERNAL_DIRECTORY_NAMES)
_DATASET_TEMPORARY_FILE = re.compile(
    r"^\.[\s\S]+\.dupeguru-[0-9a-f]{12}-[0-9]{6}\.tmp$",
    re.IGNORECASE,
)


def is_reserved_internal_directory(path) -> bool:
    """Return whether ``path`` itself uses a reserved internal directory name."""

    candidate = Path(path)
    share = _windows_unc_share(candidate)
    return _name_key(candidate.name) in _RESERVED_KEYS or (
        share is not None and len(candidate.parts) == 1 and _name_key(share) in _RESERVED_KEYS
    )


def _windows_unc_share(path: Path) -> str | None:
    """Return the share component hidden inside a Windows UNC anchor."""

    if os.name != "nt":
        return None
    drive = os.fspath(path.drive).replace("/", "\\")
    folded = drive.casefold()
    if folded.startswith("\\\\?\\unc\\"):
        components = drive[8:].split("\\")
    elif drive.startswith("\\\\"):
        components = drive[2:].split("\\")
    else:
        return None
    components = [component for component in components if component]
    return components[1] if len(components) >= 2 else None


def is_within_reserved_internal_directory(path) -> bool:
    """Return whether any component places ``path`` in an internal area."""

    candidate = Path(path)
    share = _windows_unc_share(candidate)
    return (share is not None and _name_key(share) in _RESERVED_KEYS) or any(
        _name_key(part) in _RESERVED_KEYS for part in candidate.parts
    )


def is_reserved_internal_file(path) -> bool:
    """Return whether ``path`` is a dataset executor's temporary payload."""

    return _DATASET_TEMPORARY_FILE.fullmatch(Path(path).name) is not None


__all__ = [
    "RESERVED_INTERNAL_DIRECTORY_NAMES",
    "is_reserved_internal_directory",
    "is_reserved_internal_file",
    "is_unsafe_path_component",
    "is_unsafe_windows_path_component",
    "is_within_reserved_internal_directory",
]
