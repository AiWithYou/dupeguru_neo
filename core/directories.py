# Copyright 2017 Virgil Dupras
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

from xml.etree import ElementTree as ET
from dataclasses import dataclass
import io
import logging
import math
import os
from pathlib import Path
import time

from hscommon.jobprogress import job
from hscommon.trans import tr

from core import fs
from core.services.models import (
    DEFAULT_SCAN_MAX_FILES,
    DEFAULT_SCAN_MAX_GROUPS,
    DEFAULT_SCAN_MAX_ISSUES,
    DEFAULT_SCAN_MAX_SECONDS,
)
from core.safe_xml import parse_xml, write_xml
from core.safe_walk import WalkEventKind, walk_no_follow
from core.reserved_paths import (
    is_reserved_internal_directory,
    is_reserved_internal_file,
    is_within_reserved_internal_directory,
)

DIRECTORIES_XML_MAX_BYTES = 64 * 1024 * 1024
DIRECTORIES_XML_MAX_ROOTS = 4096
DIRECTORIES_XML_MAX_STATES = 65_536
DIRECTORIES_XML_MAX_PATH_CHARS = 32_767
DIRECTORIES_XML_MAX_TOTAL_CHARS = 48 * 1024 * 1024

# GUI scans which enumerate the live filesystem directly use the same public
# ceilings as the bounded exact-scan service.  Folder scans need their own
# retained-input ceiling, for which the service's group ceiling is the closest
# existing contract.
DIRECT_DISCOVERY_MAX_FILES = DEFAULT_SCAN_MAX_FILES
DIRECT_DISCOVERY_MAX_FOLDERS = DEFAULT_SCAN_MAX_GROUPS
DIRECT_DISCOVERY_MAX_ISSUES = DEFAULT_SCAN_MAX_ISSUES
DIRECT_DISCOVERY_MAX_SECONDS = DEFAULT_SCAN_MAX_SECONDS

__all__ = [
    "Directories",
    "DirectoryState",
    "AlreadyThereError",
    "InvalidPathError",
    "DirectoriesLoadError",
    "DirectoriesSaveError",
    "DirectDiscoveryLimits",
    "DirectDiscoveryBudget",
    "DirectDiscoveryResourceError",
]


class DirectoryState:
    """Enum describing how a folder should be considered.

    * DirectoryState.NORMAL: Scan files as incoming/deletable candidates
    * DirectoryState.REFERENCE: Scan files as protected comparison candidates
    * DirectoryState.EXCLUDED: Don't scan this folder
    * DirectoryState.COMPARE_ONLY: Scan files, but never make them destructive targets
    """

    # Values 0-2 are persisted in directory XML files and must remain stable.
    NORMAL = 0
    REFERENCE = 1
    EXCLUDED = 2
    COMPARE_ONLY = 3

    ALL = frozenset({NORMAL, REFERENCE, EXCLUDED, COMPARE_ONLY})


class AlreadyThereError(Exception):
    """The path being added is already in the directory list"""


class InvalidPathError(Exception):
    """The path being added is invalid"""


class DirectoriesLoadError(ValueError):
    """A directory-selection document failed bounded schema validation."""


class DirectoriesSaveError(ValueError):
    """The active directory selection cannot satisfy the loader contract."""


@dataclass(frozen=True)
class DirectDiscoveryLimits:
    """Finite limits for one GUI filesystem-discovery pass."""

    max_files: int = DIRECT_DISCOVERY_MAX_FILES
    max_folders: int = DIRECT_DISCOVERY_MAX_FOLDERS
    max_issues: int = DIRECT_DISCOVERY_MAX_ISSUES
    max_seconds: float = DIRECT_DISCOVERY_MAX_SECONDS

    def __post_init__(self):
        integer_limits = (
            ("max_files", self.max_files, DIRECT_DISCOVERY_MAX_FILES),
            ("max_folders", self.max_folders, DIRECT_DISCOVERY_MAX_FOLDERS),
            ("max_issues", self.max_issues, DIRECT_DISCOVERY_MAX_ISSUES),
        )
        for name, value, maximum in integer_limits:
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("{} must be an integer from 1 to {}".format(name, maximum))
        if (
            isinstance(self.max_seconds, bool)
            or not isinstance(self.max_seconds, (int, float))
            or not math.isfinite(self.max_seconds)
            or not 0 < self.max_seconds <= DIRECT_DISCOVERY_MAX_SECONDS
        ):
            raise ValueError(
                "max_seconds must be a finite number from 1 to {}".format(
                    DIRECT_DISCOVERY_MAX_SECONDS,
                )
            )


class DirectDiscoveryResourceError(RuntimeError):
    """Direct filesystem discovery could not produce one complete input list."""

    def __init__(self, code, message, *, path="", budget=None):
        super().__init__(message)
        self.code = code
        self.path = str(path)
        self.files = int(getattr(budget, "files", 0))
        self.folders = int(getattr(budget, "folders", 0))
        self.issues = int(getattr(budget, "issues", 0))
        self.events = int(getattr(budget, "events", 0))


class DirectDiscoveryBudget:
    """Mutable counters shared by every selected root in one discovery pass."""

    def __init__(self, limits=None, *, clock=time.monotonic):
        if limits is None:
            limits = DirectDiscoveryLimits()
        if not isinstance(limits, DirectDiscoveryLimits):
            raise TypeError("limits must be a DirectDiscoveryLimits instance")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.limits = limits
        self._clock = clock
        self._started_at = clock()
        if (
            isinstance(self._started_at, bool)
            or not isinstance(self._started_at, (int, float))
            or not math.isfinite(self._started_at)
        ):
            raise ValueError("clock must return a finite number")
        self._last_clock = self._started_at
        self.files = 0
        self.folders = 0
        self.issues = 0
        self.events = 0

    def _raise(self, name, maximum, path):
        raise DirectDiscoveryResourceError(
            "resource-limit-{}".format(name),
            "Direct filesystem discovery exceeded max_{} ({}).".format(name, maximum),
            path=path,
            budget=self,
        )

    def check_time(self, path=""):
        current = self._clock()
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(current)
            or current < self._last_clock
        ):
            raise DirectDiscoveryResourceError(
                "resource-limit-seconds",
                "Direct filesystem discovery received an invalid non-monotonic clock value.",
                path=path,
                budget=self,
            )
        self._last_clock = current
        if current - self._started_at >= self.limits.max_seconds:
            self._raise("seconds", self.limits.max_seconds, path)

    def count_event(self):
        self.events += 1

    def check_file_capacity(self, path):
        if self.files >= self.limits.max_files:
            self._raise("files", self.limits.max_files, path)

    def count_file(self, path):
        self.check_file_capacity(path)
        self.files += 1

    def count_folder(self, path):
        if self.folders >= self.limits.max_folders:
            self._raise("folders", self.limits.max_folders, path)
        self.folders += 1

    def check_ancestor_capacity(self, retained_ancestors, path):
        if retained_ancestors >= self.limits.max_folders:
            self._raise("folders", self.limits.max_folders, path)

    def count_issue(self, path):
        if self.issues >= self.limits.max_issues:
            self._raise("issues", self.limits.max_issues, path)
        self.issues += 1

    def memory_error(self, path=""):
        return DirectDiscoveryResourceError(
            "resource-limit-memory",
            "Direct filesystem discovery ran out of memory before its input list was complete.",
            path=path,
            budget=self,
        )


_DISCOVERY_ISSUE_EVENT_KINDS = frozenset(
    {
        WalkEventKind.SYMLINK_SKIPPED,
        WalkEventKind.REPARSE_POINT_SKIPPED,
        WalkEventKind.MOUNT_SKIPPED,
        WalkEventKind.CYCLE_SKIPPED,
        WalkEventKind.OUTSIDE_ALLOWED_ROOT_SKIPPED,
        WalkEventKind.SPECIAL_FILE_SKIPPED,
        WalkEventKind.DIRECTORY_CHANGED_SKIPPED,
        WalkEventKind.ERROR,
    }
)


class Directories:
    """Holds user folder selection.

    Manages the selection that the user make through the folder selection dialog. It also manages
    folder states, and how recursion applies to them.

    Then, when the user starts the scan, :meth:`get_files` is called to retrieve all files (wrapped
    in :mod:`core.fs`) that have to be scanned according to the chosen folders/states.
    """

    # ---Override
    def __init__(self, exclude_list=None):
        self._dirs = []
        # {path: state}
        self.states = {}
        self._revision = 0
        self._exclude_list = exclude_list
        # Results from the most recent get_files()/get_folders() traversal,
        # keyed by selected root.  Coverage-reducing events are retained up to
        # the explicit issue budget; ordinary FILE/DIRECTORY events are never
        # duplicated in this audit structure.
        self.last_walk_events = {}
        self.last_walk_coverages = {}
        self.last_walk_events_truncated = False

    def __contains__(self, path):
        for p in self._dirs:
            if path == p or p in path.parents:
                return True
        return False

    def __delitem__(self, key):
        before = tuple(self._dirs)
        self._dirs.__delitem__(key)
        if tuple(self._dirs) != before:
            self._revision += 1

    def __getitem__(self, key):
        return self._dirs.__getitem__(key)

    def __len__(self):
        return len(self._dirs)

    # ---Private
    def _default_state_for_path(self, path):
        if is_within_reserved_internal_directory(path):
            return DirectoryState.EXCLUDED
        # New logic with regex filters
        if self._exclude_list is not None and self._exclude_list.mark_count > 0:
            if self._exclude_list.is_excluded(str(path.parent), path.name):
                return DirectoryState.EXCLUDED
            # We iterate even if we only have one item here
            for denied_path_re in self._exclude_list.compiled:
                if denied_path_re.match(str(path.name)):
                    return DirectoryState.EXCLUDED
            return DirectoryState.NORMAL
        # Override this in subclasses to specify the state of some special folders.
        if path.name.startswith("."):
            return DirectoryState.EXCLUDED
        return DirectoryState.NORMAL

    def _non_excluded_override_ancestors(self, *, budget=None, j=job.nulljob):
        """Index ancestors which must stay open for a non-excluded override."""

        ancestors = set()
        selected_roots = frozenset(Path(root) for root in self._dirs)
        for override_path, override_state in self.states.items():
            if budget is not None:
                budget.check_time(override_path)
            j.check_if_cancelled()
            if override_state == DirectoryState.EXCLUDED or override_path in selected_roots:
                continue
            selected_root = None
            for parent in override_path.parents:
                if budget is not None:
                    budget.check_time(parent)
                j.check_if_cancelled()
                if parent in selected_roots:
                    selected_root = parent
                    break
            if selected_root is not None:
                for parent in override_path.parents:
                    if parent not in ancestors:
                        if budget is not None:
                            budget.check_ancestor_capacity(
                                len(ancestors),
                                parent,
                            )
                        ancestors.add(parent)
                    if parent == selected_root:
                        break
                    if budget is not None:
                        budget.check_time(parent)
                    j.check_if_cancelled()
        return frozenset(ancestors)

    def _directory_prune_reason(self, path, non_excluded_override_ancestors=None):
        """Return an intentional-prune reason, preserving child overrides."""

        if is_reserved_internal_directory(path):
            return "internal dupeGuru Neo directory is always excluded"
        if self.get_state(path) != DirectoryState.EXCLUDED:
            return None
        if non_excluded_override_ancestors is None:
            non_excluded_override_ancestors = self._non_excluded_override_ancestors()
        if path in non_excluded_override_ancestors:
            return None
        return "directory excluded by DirectoryState or ExcludeList"

    def _file_is_excluded(self, path):
        if is_reserved_internal_file(path):
            return True
        exclude_list = self._exclude_list
        return (
            exclude_list is not None
            and exclude_list.mark_count > 0
            and exclude_list.is_excluded(str(path.parent), path.name)
        )

    @staticmethod
    def _comparison_pool_for_state(state):
        if state == DirectoryState.REFERENCE:
            return "protected"
        if state == DirectoryState.COMPARE_ONLY:
            return "compare_only"
        return "incoming"

    @classmethod
    def _apply_file_state(cls, file, state):
        file.comparison_pool = cls._comparison_pool_for_state(state)
        file.is_ref = state in (DirectoryState.REFERENCE, DirectoryState.COMPARE_ONLY)

    def _walk_events(self, root, j, directory_pruner, budget):
        root = Path(root)
        events = []
        self.last_walk_events[root] = events
        try:
            event_iterator = iter(
                walk_no_follow(
                    root,
                    allowed_root=root,
                    directory_pruner=directory_pruner,
                )
            )
            while True:
                # The clock is sampled on both sides of every walker event so
                # a slow filesystem call cannot evade the elapsed-time budget.
                budget.check_time(root)
                # A cancellation already requested by the UI must prevent the
                # next potentially slow filesystem operation from starting.
                j.check_if_cancelled()
                try:
                    event = next(event_iterator)
                except StopIteration:
                    break
                budget.count_event()
                budget.check_time(event.path)
                if event.kind in _DISCOVERY_ISSUE_EVENT_KINDS:
                    try:
                        budget.count_issue(event.path)
                    except DirectDiscoveryResourceError:
                        self.last_walk_events_truncated = True
                        raise
                    events.append(event)
                if event.kind is WalkEventKind.DIRECTORY:
                    budget.count_folder(event.path)
                if event.coverage is not None:
                    self.last_walk_coverages[root] = event.coverage
                # Cancellation is checked for every event, including explicit
                # errors and coverage events, rather than only accepted files.
                j.check_if_cancelled()
                yield event
                budget.check_time(event.path)
        except MemoryError as error:
            self.last_walk_events_truncated = True
            raise budget.memory_error(root) from error

    # ---Public
    def add_path(self, path):
        """Adds ``path`` to self, if not already there.

        Raises :exc:`AlreadyThereError` if ``path`` is already in self. If path is a directory
        containing some of the directories already present in self, ``path`` will be added, but all
        directories under it will be removed. Can also raise :exc:`InvalidPathError` if ``path``
        does not exist.

        :param Path path: path to add
        """
        path = Path(path)
        if is_within_reserved_internal_directory(path):
            raise InvalidPathError()
        if path in self:
            raise AlreadyThereError()
        try:
            is_directory = path.is_dir()
        except OSError as error:
            logging.warning("Could not access selected directory %s: %s", path, error)
            raise InvalidPathError() from error
        if not is_directory:
            raise InvalidPathError()
        self._dirs = [p for p in self._dirs if path not in p.parents]
        self._dirs.append(path)
        self._revision += 1

    @staticmethod
    def get_subfolders(path):
        """Returns a sorted list of paths corresponding to subfolders in ``path``.

        :param Path path: get subfolders from there
        :rtype: list of Path
        """
        try:
            subpaths = [p for p in path.glob("*") if p.is_dir()]
            subpaths.sort(key=lambda x: x.name.lower())
            return subpaths
        except OSError:
            return []

    def get_files(self, fileclasses=None, j=job.nulljob, budget=None):
        """Returns a list of all files that are not excluded.

        Returned files also have their ``is_ref`` attr set if applicable.
        ``last_walk_events`` retains only bounded coverage-reducing audit
        events; ``last_walk_coverages`` retains final per-root coverage.
        """
        if fileclasses is None:
            fileclasses = [fs.File]
        if budget is None:
            budget = DirectDiscoveryBudget()
        if not isinstance(budget, DirectDiscoveryBudget):
            raise TypeError("budget must be a DirectDiscoveryBudget instance")
        self.last_walk_events = {}
        self.last_walk_coverages = {}
        self.last_walk_events_truncated = False
        current_path = ""
        try:
            non_excluded_override_ancestors = self._non_excluded_override_ancestors(
                budget=budget,
                j=j,
            )
        except MemoryError as error:
            raise budget.memory_error(current_path) from error

        def directory_pruner(path):
            return self._directory_prune_reason(path, non_excluded_override_ancestors)

        file_count = 0
        try:
            for path in self._dirs:
                current_path = path
                root_file_count = 0
                for event in self._walk_events(path, j, directory_pruner, budget):
                    current_path = event.path
                    if event.kind != WalkEventKind.FILE:
                        continue
                    state = self.get_state(event.path.parent)
                    if state == DirectoryState.EXCLUDED or self._file_is_excluded(event.path):
                        continue
                    try:
                        fileclass = next(
                            (candidate for candidate in fileclasses if candidate.can_handle(event.path)),
                            None,
                        )
                    except (OSError, fs.InvalidPath):
                        continue
                    if fileclass is None:
                        continue
                    budget.check_file_capacity(event.path)
                    try:
                        file = fileclass(event.path)
                    except (OSError, fs.InvalidPath):
                        continue
                    budget.count_file(event.path)
                    self._apply_file_state(file, state)
                    file_count += 1
                    root_file_count += 1
                    if not isinstance(j, job.NullJob):
                        j.set_progress(-1, tr("Collected {} files to scan").format(file_count))
                    yield file
                logging.debug("Collected %d files in folder %s", root_file_count, str(path))
        except MemoryError as error:
            raise budget.memory_error(current_path) from error

    def get_folders(self, folderclass=None, j=job.nulljob, budget=None):
        """Returns a list of all folders that are not excluded.

        Returned folders also have their ``is_ref`` attr set if applicable.
        Folder discovery uses the same no-follow traversal as file discovery.
        Its historical child-before-parent ordering uses a buffer which is
        bounded by ``budget.limits.max_folders``.
        """
        if folderclass is None:
            folderclass = fs.Folder
        if budget is None:
            budget = DirectDiscoveryBudget()
        if not isinstance(budget, DirectDiscoveryBudget):
            raise TypeError("budget must be a DirectDiscoveryBudget instance")
        self.last_walk_events = {}
        self.last_walk_coverages = {}
        self.last_walk_events_truncated = False
        current_path = ""
        try:
            non_excluded_override_ancestors = self._non_excluded_override_ancestors(
                budget=budget,
                j=j,
            )
        except MemoryError as error:
            raise budget.memory_error(current_path) from error

        def directory_pruner(path):
            return self._directory_prune_reason(path, non_excluded_override_ancestors)

        folder_count = 0
        try:
            for path in self._dirs:
                current_path = path
                folder_paths = []
                for event in self._walk_events(path, j, directory_pruner, budget):
                    current_path = event.path
                    if event.kind == WalkEventKind.DIRECTORY:
                        folder_paths.append(event.path)
                # Preserve the historical post-order contract (children before
                # parents). ``folder_paths`` cannot exceed max_folders because
                # the shared budget counts before appending every directory.
                for folder_path in reversed(folder_paths):
                    current_path = folder_path
                    budget.check_time(folder_path)
                    j.check_if_cancelled()
                    state = self.get_state(folder_path)
                    folder = None
                    if state != DirectoryState.EXCLUDED:
                        try:
                            folder = folderclass(folder_path)
                        except (OSError, fs.InvalidPath):
                            pass
                    if folder is not None:
                        self._apply_file_state(folder, state)
                        logging.debug("Yielding Folder %r state: %d", folder, state)
                        folder_count += 1
                        if not isinstance(j, job.NullJob):
                            j.set_progress(-1, tr("Collected {} folders to scan").format(folder_count))
                    # This check covers successful construction as well as
                    # excluded and constructor-error paths.
                    budget.check_time(folder_path)
                    j.check_if_cancelled()
                    if folder is None:
                        continue
                    yield folder
                    budget.check_time(folder_path)
                    j.check_if_cancelled()
        except MemoryError as error:
            raise budget.memory_error(current_path) from error

    @property
    def last_walk_errors(self):
        """Return explicit walker errors, grouped by selected root."""

        return {
            root: tuple(event for event in events if event.kind == WalkEventKind.ERROR)
            for root, events in self.last_walk_events.items()
        }

    @property
    def revision(self):
        """Monotonic revision used to protect a failed persistent source."""

        return self._revision

    def get_state(self, path):
        """Returns the state of ``path``.

        :rtype: :class:`DirectoryState`
        """
        path = Path(path)
        if is_within_reserved_internal_directory(path):
            return DirectoryState.EXCLUDED
        # direct match? easy result.
        if path in self.states:
            return self.states[path]
        state = self._default_state_for_path(path)
        if state != DirectoryState.NORMAL:
            return state
        # Find the nearest explicit or automatic parent policy without
        # materializing every discovered path in ``states``.
        for parent_path in path.parents:
            if parent_path in self.states:
                return self.states[parent_path]
            parent_default = self._default_state_for_path(parent_path)
            if parent_default != DirectoryState.NORMAL:
                return parent_default
        return state

    def has_any_file(self, budget=None):
        """Returns whether selected folders contain any file.

        Because it stops at the first file it finds, it's much faster than get_files().

        :rtype: bool
        """
        try:
            next(self.get_files(budget=budget))
            return True
        except StopIteration:
            return False

    @staticmethod
    def _require_whitespace(value, description):
        if value and value.strip():
            raise DirectoriesLoadError(f"{description} must not contain text")

    @staticmethod
    def _validate_loaded_path(value, description, *, require_absolute=True):
        if not value:
            raise DirectoriesLoadError(f"{description} has an empty path")
        if len(value) > DIRECTORIES_XML_MAX_PATH_CHARS:
            raise DirectoriesLoadError(f"{description} path is too long")
        if "\0" in value:
            raise DirectoriesLoadError(f"{description} path contains a NUL byte")
        path = Path(value)
        if require_absolute and not path.is_absolute():
            raise DirectoriesLoadError(f"{description} path is not absolute")
        if is_within_reserved_internal_directory(path):
            raise DirectoriesLoadError(f"{description} references an internal application directory")
        return path

    def _parse_loaded_state(self, infile):
        max_items = DIRECTORIES_XML_MAX_ROOTS + DIRECTORIES_XML_MAX_STATES
        root = parse_xml(
            infile,
            max_bytes=DIRECTORIES_XML_MAX_BYTES,
            max_elements=max_items + 1,
            max_depth=2,
            max_attributes_per_element=2,
            max_attributes=max_items * 2,
            max_name_chars=32,
            max_attribute_chars=DIRECTORIES_XML_MAX_PATH_CHARS,
            max_text_chars=4096,
            max_tail_chars=4096,
            max_total_chars=DIRECTORIES_XML_MAX_TOTAL_CHARS,
        )
        if root.tag != "directories":
            raise DirectoriesLoadError("directory-selection XML has the wrong root element")
        if root.attrib:
            raise DirectoriesLoadError("directories must not have attributes")
        self._require_whitespace(root.text, "directories")
        self._require_whitespace(root.tail, "directories")

        roots = []
        states = {}
        seen_roots = set()
        root_count = 0
        state_count = 0
        for item_number, element in enumerate(root, 1):
            if len(element):
                raise DirectoriesLoadError(f"directory item {item_number} must not have child elements")
            self._require_whitespace(element.text, f"directory item {item_number}")
            self._require_whitespace(element.tail, f"directory item {item_number}")
            if element.tag == "root_directory":
                root_count += 1
                if root_count > DIRECTORIES_XML_MAX_ROOTS:
                    raise DirectoriesLoadError("directory root count exceeds the supported limit")
                if set(element.attrib) != {"path"}:
                    raise DirectoriesLoadError(f"directory root {root_count} has invalid attributes")
                path = self._validate_loaded_path(
                    element.attrib["path"],
                    f"directory root {root_count}",
                )
                if path in seen_roots:
                    raise DirectoriesLoadError(f"directory root {root_count} is duplicated")
                seen_roots.add(path)
                roots.append(path)
            elif element.tag == "state":
                state_count += 1
                if state_count > DIRECTORIES_XML_MAX_STATES:
                    raise DirectoriesLoadError("directory state count exceeds the supported limit")
                if set(element.attrib) != {"path", "value"}:
                    raise DirectoriesLoadError(f"directory state {state_count} has invalid attributes")
                path = self._validate_loaded_path(
                    element.attrib["path"],
                    f"directory state {state_count}",
                )
                if path in states:
                    raise DirectoriesLoadError(f"directory state {state_count} is duplicated")
                value = element.attrib["value"]
                if value not in {str(state) for state in DirectoryState.ALL}:
                    raise DirectoriesLoadError(f"directory state {state_count} has an invalid value")
                states[path] = int(value)
            else:
                raise DirectoriesLoadError(f"directory item {item_number} has an unknown element")
        self._validate_root_relationships(roots)
        return roots, states

    @staticmethod
    def _validate_root_relationships(roots):
        """Reject duplicate or nested roots without requiring them to be online."""

        seen = set()
        for root in sorted(roots, key=lambda candidate: len(candidate.parts)):
            if root in seen or any(parent in seen for parent in root.parents):
                raise DirectoriesLoadError("directory roots overlap or duplicate each other")
            seen.add(root)

    def load_from_file(self, infile):
        """Transactionally load folder selection from bounded, strict XML.

        :param file infile: path or file pointer to XML generated through :meth:`save_to_file`
        """
        try:
            roots, states = self._parse_loaded_state(infile)
        except Exception as error:
            failure = (
                error
                if isinstance(error, DirectoriesLoadError)
                else DirectoriesLoadError(f"could not load directory-selection XML: {type(error).__name__}")
            )
            logging.warning("Error while loading directory-selection XML: %s", failure)
            return failure

        changed = self._dirs != roots or self.states != states
        # Persisted removable/network roots remain part of the selection even
        # while unavailable. The bounded walker reports them at scan time.
        self._dirs = list(roots)
        self.states = dict(states)
        self.last_walk_events = {}
        self.last_walk_coverages = {}
        self.last_walk_events_truncated = False
        if changed:
            self._revision += 1
        return None

    def save_to_file(self, outfile):
        """Save folder selection as XML to ``outfile``.

        :param file outfile: path or file pointer to XML file to save to.
        """
        root = ET.Element("directories")
        for root_path in self:
            root_path_node = ET.SubElement(root, "root_directory")
            root_path_node.set("path", str(root_path))
        for path, state in self.states.items():
            state_node = ET.SubElement(root, "state")
            state_node.set("path", str(path))
            state_node.set("value", str(state))
        tree = ET.ElementTree(root)
        try:
            payload = ET.tostring(
                root,
                encoding="utf-8",
                xml_declaration=True,
            )
            self._parse_loaded_state(io.BytesIO(payload))
        except Exception as error:
            raise DirectoriesSaveError(
                "directory selection does not satisfy the bounded loader contract: {}".format(error)
            ) from error
        write_xml(tree, outfile)

    def set_state(self, path, state):
        """Set the state of folder at ``path``.

        :param Path path: path of the target folder
        :param state: state to set folder to
        :type state: :class:`DirectoryState`
        """
        if state not in DirectoryState.ALL:
            raise ValueError("Invalid directory state: {}".format(state))
        path = Path(path)
        if is_within_reserved_internal_directory(path):
            if self.states.pop(path, None) is not None:
                self._revision += 1
            return
        if self.get_state(path) == state:
            return
        for iter_path in list(self.states.keys()):
            if path in iter_path.parents:
                del self.states[iter_path]
        self.states[path] = state
        self._revision += 1

    def current_file_pool(self, path):
        """Resolve a file's current pool from live roots, states, and filters."""

        path = Path(os.path.abspath(os.fspath(path)))
        if is_within_reserved_internal_directory(path) or is_reserved_internal_file(path):
            return "excluded"
        if not any(root == path.parent or root in path.parents for root in self._dirs):
            return "excluded"
        state = self.get_state(path.parent)
        if state == DirectoryState.EXCLUDED or self._file_is_excluded(path):
            return "excluded"
        return self._comparison_pool_for_state(state)
