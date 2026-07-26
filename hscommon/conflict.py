# Created By: Virgil Dupras
# Created On: 2008-01-08
# Copyright 2015 Hardcoded Software (http://www.hardcoded.net)

# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

"""When you have to deal with names that have to be unique and can conflict together, you can use
this module that deals with conflicts by prepending unique numbers in ``[]`` brackets to the name.
"""

import errno
import os
import re
import stat

from pathlib import Path
from typing import Iterator, List

from hscommon.safe_fileops import RenameNoReplace, copy_to_first_available, move_to_first_available

# This matches [123], but not [12] (3 digits being the minimum).
# It also matches [1234] [12345] etc..
# And only at the start of the string
re_conflict = re.compile(r"^\[\d{3}\d*\] ")
MAX_CONFLICT_CANDIDATES = 100_001


def get_conflicted_name(other_names: List[str], name: str) -> str:
    """Returns name with a ``[000]`` number in front of it.

    The number between brackets depends on how many conlicted filenames
    there already are in other_names.
    """
    name = get_unconflicted_name(name)
    if name not in other_names:
        return name
    i = 0
    while True:
        newname = "[%03d] %s" % (i, name)
        if newname not in other_names:
            return newname
        i += 1


def get_unconflicted_name(name: str) -> str:
    """Returns ``name`` without ``[]`` brackets.

    Brackets which, of course, might have been added by func:`get_conflicted_name`.
    """
    return re_conflict.sub("", name, 1)


def is_conflicted(name: str) -> bool:
    """Returns whether ``name`` is prepended with a bracketed number."""
    return re_conflict.match(name) is not None


def _is_reparse_point(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(marker and attributes & marker)


def _destination_base(source_path: Path, dest_path: Path) -> Path:
    source_stat = os.lstat(source_path)
    if stat.S_ISLNK(source_stat.st_mode) or _is_reparse_point(source_stat):
        raise OSError(errno.ELOOP, "The source must not be a link or reparse point", str(source_path))
    try:
        destination_stat = os.lstat(dest_path)
    except FileNotFoundError:
        return dest_path
    if stat.S_ISLNK(destination_stat.st_mode) or _is_reparse_point(destination_stat):
        raise OSError(errno.ELOOP, "The destination must not be a link or reparse point", str(dest_path))
    if not stat.S_ISDIR(source_stat.st_mode) and stat.S_ISDIR(destination_stat.st_mode):
        return dest_path.joinpath(source_path.name)
    return dest_path


def _destination_candidates(destination: Path) -> Iterator[Path]:
    """Yield the legacy conflict-name order without trusting a directory snapshot."""

    yielded = {destination.name}
    yield destination
    base_name = get_unconflicted_name(destination.name)
    if base_name not in yielded:
        yielded.add(base_name)
        yield destination.with_name(base_name)
    for index in range(MAX_CONFLICT_CANDIDATES - len(yielded)):
        name = "[%03d] %s" % (index, base_name)
        if name in yielded:
            continue
        yield destination.with_name(name)


def smart_move(
    source_path: Path,
    dest_path: Path,
    *,
    rename_no_replace: RenameNoReplace,
    expected_source_snapshot=None,
) -> Path:
    """Move with conflict resolution and an injected atomic no-replace primitive."""

    destination = _destination_base(source_path, dest_path)
    return move_to_first_available(
        source_path,
        _destination_candidates(destination),
        rename_no_replace,
        expected_source_snapshot=expected_source_snapshot,
    )


def smart_copy(
    source_path: Path,
    dest_path: Path,
    *,
    rename_no_replace: RenameNoReplace,
    expected_source_snapshot=None,
) -> Path:
    """Copy through a flushed sibling staging entry, then atomically publish without replacement."""

    destination = _destination_base(source_path, dest_path)
    return copy_to_first_available(
        source_path,
        _destination_candidates(destination),
        rename_no_replace,
        expected_source_snapshot=expected_source_snapshot,
    )
