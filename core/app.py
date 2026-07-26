# Copyright 2016 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import cProfile
import datetime
import errno
import os
import os.path as op
import logging
import subprocess
import shlex
from pathlib import Path

from hscommon.jobprogress import job
from hscommon.notify import Broadcaster
from hscommon.conflict import smart_move, smart_copy
from hscommon.gui.progress_window import ProgressWindow
from hscommon.safe_fileops import (
    ensure_plain_directory,
    remove_empty_directories,
    validate_cleanup_path,
    validate_source_destination,
)
from hscommon.util import escape, nonone, allsame
from hscommon.trans import tr
from hscommon import desktop

from core import se, me, pe
from core.pe.cache_sqlite import SqliteCache
from core.pe.photo import get_delta_dimensions
from core.util import cmp_value, fix_surrogate_encoding
from core import __appname__, directories, engine, results, export, fs, prioritize
from core.catalog import CatalogStateError
from core.catalog_service import CatalogService, CatalogServiceError
from core.catalog_worker import CatalogWorkerError
from core.ignore import IgnoreList, IgnoreListLimitError
from core.exclude import (
    ExcludeDict as ExcludeList,
    ExcludeListLimitError,
)
from core.scanner import ScanType
from core.destructive_eligibility import (
    evaluate_batch,
    evaluate_relocation_batch,
    evaluate_rename,
)
from core.action_plan import ActionPlanError, build_bound_deletion_plan
from core.quarantine import QuarantineError, QuarantineManager
from core.safe_action import platform_file_system
from core.scan_receipt import (
    ScanIssue,
    ScanReceipt,
    ScanStatus,
    receipt_from_walk_coverages,
)
from core.gui.deletion_options import DeletionOptions
from core.gui.details_panel import DetailsPanel
from core.gui.directory_tree import DirectoryTree
from core.gui.ignore_list_dialog import IgnoreListDialog
from core.gui.exclude_list_dialog import ExcludeListDialogCore
from core.gui.problem_dialog import ProblemDialog
from core.gui.stats_label import StatsLabel

HAD_FIRST_LAUNCH_PREFERENCE = "HadFirstLaunch"
DEBUG_MODE_PREFERENCE = "DebugMode"
HASH_CACHE_FILENAME = "hash_cache_v3.sqlite3"
CATALOG_GUI_EXACT_PAGE_GROUPS = 100
CATALOG_GUI_EXACT_PAGE_FILES = 10_000
CATALOG_GUI_MAX_EXACT_GROUPS = 10_000
CATALOG_GUI_MAX_EXACT_FILES = 100_000
CATALOG_GUI_MAX_EXACT_GROUP_MEMBERS = 10_000

MSG_NO_MARKED_DUPES = tr("There are no marked duplicates. Nothing has been done.")
MSG_NO_SELECTED_DUPES = tr("There are no selected duplicates. Nothing has been done.")
MSG_CUSTOM_COMMAND_BOUNDARY = tr(
    "Run the configured external command for {} selected file(s)?\n\n"
    "External commands run outside dupeGuru's safety model. They can modify or "
    "permanently delete files, including protected or perceptually similar files. "
    "dupeGuru cannot verify or undo those changes."
)


def parse_custom_command(template, dupe_path, reference_path, *, windows=None):
    """Parse a custom-command template into an argv list without invoking a shell."""

    if windows is None:
        windows = os.name == "nt"
    argv = shlex.split(template, posix=not windows)
    if windows:
        argv = [
            token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"} else token
            for token in argv
        ]
    argv = [token.replace("%d", str(dupe_path)).replace("%r", str(reference_path)) for token in argv]
    if not argv:
        raise ValueError("custom command is empty")
    if any("\0" in token for token in argv):
        raise ValueError("custom command contains a NUL byte")
    return argv


MSG_MANY_FILES_TO_OPEN = tr(
    "You're about to open many files at once. Depending on what those "
    "files are opened with, doing so can create quite a mess. Continue?"
)


class DestType:
    DIRECT = 0
    RELATIVE = 1
    ABSOLUTE = 2


class JobType:
    SCAN = "job_scan"
    LOAD = "job_load"
    MOVE = "job_move"
    COPY = "job_copy"
    DELETE = "job_delete"
    RESTORE = "job_restore"


class AppMode:
    STANDARD = 0
    MUSIC = 1
    PICTURE = 2


JOBID2TITLE = {
    JobType.SCAN: tr("Scanning for duplicates"),
    JobType.LOAD: tr("Loading"),
    JobType.MOVE: tr("Moving"),
    JobType.COPY: tr("Copying"),
    JobType.DELETE: tr("Moving verified duplicates to quarantine"),
    JobType.RESTORE: tr("Restoring quarantined files"),
}


class DupeGuru(Broadcaster):
    """Holds everything together.

    Instantiated once per running application, it holds a reference to every high-level object
    whose reference needs to be held: :class:`~core.results.Results`,
    :class:`~core.directories.Directories`, :mod:`core.gui` instances, etc..

    It also hosts high level methods and acts as a coordinator for all those elements. This is why
    some of its methods seem a bit shallow, like for example :meth:`mark_all` and
    :meth:`remove_duplicates`. These methos are just proxies for a method in :attr:`results`, but
    they are also followed by a notification call which is very important if we want GUI elements
    to be correctly notified of a change in the data they're presenting.

    .. attribute:: directories

        Instance of :class:`~core.directories.Directories`. It holds the current folder selection.

    .. attribute:: results

        Instance of :class:`core.results.Results`. Holds the results of the latest scan.

    .. attribute:: selected_dupes

        List of currently selected dupes from our :attr:`results`. Whenever the user changes its
        selection at the UI level, :attr:`result_table` takes care of updating this attribute, so
        you can trust that it's always up-to-date.

    .. attribute:: result_table

        Instance of :mod:`meta-gui <core.gui>` table listing the results from :attr:`results`
    """

    # --- View interface
    # get_default(key_name)
    # set_default(key_name, value)
    # show_message(msg)
    # open_url(url)
    # open_path(path)
    # reveal_path(path)
    # ask_yes_no(prompt) --> bool
    # create_results_window()
    # show_results_window()
    # show_problem_dialog()
    # select_dest_folder(prompt: str) --> str
    # select_dest_file(prompt: str, ext: str) --> str

    NAME = PROMPT_NAME = __appname__

    def __init__(self, view, portable=False):
        if view.get_default(DEBUG_MODE_PREFERENCE):
            logging.getLogger().setLevel(logging.DEBUG)
            logging.debug("Debug mode enabled")
        Broadcaster.__init__(self)
        self.view = view
        self.appdata = desktop.special_folder_path(desktop.SpecialFolder.APPDATA, portable=portable)
        if not op.exists(self.appdata):
            os.makedirs(self.appdata)
        self.app_mode = AppMode.STANDARD
        self.discarded_file_count = 0
        self.exclude_list = ExcludeList()
        hash_cache_file = op.join(self.appdata, HASH_CACHE_FILENAME)
        fs.filesdb.connect(hash_cache_file)
        self.directories = directories.Directories(self.exclude_list)
        self.results = results.Results(self)
        self.ignore_list = IgnoreList()
        self._directories_failed_revision = None
        self._ignore_list_failed_revision = None
        self._exclude_list_failed_revision = None
        # In addition to "app-level" options, this dictionary also holds options that will be
        # sent to the scanner. They don't have default values because those defaults values are
        # defined in the scanner class.
        self.options = {
            "escape_filter_regexp": True,
            "clean_empty_dirs": False,
            "ignore_hardlink_matches": True,
            "copymove_dest_type": DestType.RELATIVE,
            "include_exists_check": True,
            "rehash_ignore_mtime": False,
            "comparison_scope": "all",
            "direct_scan_max_files": directories.DIRECT_DISCOVERY_MAX_FILES,
            "direct_scan_max_folders": directories.DIRECT_DISCOVERY_MAX_FOLDERS,
            "direct_scan_max_issues": directories.DIRECT_DISCOVERY_MAX_ISSUES,
            "direct_scan_max_seconds": directories.DIRECT_DISCOVERY_MAX_SECONDS,
        }
        self.selected_dupes = []
        self.details_panel = DetailsPanel(self)
        self.directory_tree = DirectoryTree(self)
        self.problem_dialog = ProblemDialog(self)
        self.ignore_list_dialog = IgnoreListDialog(self)
        self.exclude_list_dialog = ExcludeListDialogCore(self)
        self.stats_label = StatsLabel(self)
        self.result_table = None
        self.deletion_options = DeletionOptions()
        self.quarantine_manager = QuarantineManager()
        self.last_quarantine_plan_paths = ()
        self._last_file_action_outcome = {}
        self._catalog_resume_scan_id = None
        self._catalog_resume_roots = ()
        self.progress_window = ProgressWindow(self._job_completed, self._job_error)
        children = [self.directory_tree, self.stats_label, self.details_panel]
        for child in children:
            child.connect()

    # --- Private
    def _recreate_result_table(self):
        if self.result_table is not None:
            self.result_table.disconnect()
        if self.app_mode == AppMode.PICTURE:
            self.result_table = pe.result_table.ResultTable(self)
        elif self.app_mode == AppMode.MUSIC:
            self.result_table = me.result_table.ResultTable(self)
        else:
            self.result_table = se.result_table.ResultTable(self)
        self.result_table.connect()
        self.view.create_results_window()

    def _get_picture_cache_path(self):
        cache_name = "cached_pictures_v5.db"
        return op.join(self.appdata, cache_name)

    def _get_dupe_sort_key(self, dupe, get_group, key, delta):
        if self.app_mode in (AppMode.MUSIC, AppMode.PICTURE) and key == "folder_path":
            dupe_folder_path = getattr(dupe, "display_folder_path", dupe.folder_path)
            return str(dupe_folder_path).lower()
        if self.app_mode == AppMode.PICTURE and delta and key == "dimensions":
            r = cmp_value(dupe, key)
            ref_value = cmp_value(get_group().ref, key)
            return get_delta_dimensions(r, ref_value)
        if key == "marked":
            return self.results.is_marked(dupe)
        if key == "percentage":
            m = get_group().get_match_of(dupe)
            return m.percentage
        elif key == "dupe_count":
            return 0
        else:
            result = cmp_value(dupe, key)
        if delta:
            refval = cmp_value(get_group().ref, key)
            if key in self.result_table.DELTA_COLUMNS:
                result -= refval
            else:
                same = cmp_value(dupe, key) == refval
                result = (same, result)
        return result

    def _get_group_sort_key(self, group, key):
        if self.app_mode in (AppMode.MUSIC, AppMode.PICTURE) and key == "folder_path":
            dupe_folder_path = getattr(group.ref, "display_folder_path", group.ref.folder_path)
            return str(dupe_folder_path).lower()
        if key == "percentage":
            return group.percentage
        if key == "dupe_count":
            return len(group)
        if key == "marked":
            return len([dupe for dupe in group.dupes if self.results.is_marked(dupe)])
        return cmp_value(group.ref, key)

    def _do_delete(self, j, bound_plan):
        """Preflight the whole batch, then stage it in recoverable quarantine."""

        plan = bound_plan.plan
        action_dupes = dict(bound_plan.action_dupes)
        self.results.problems = []
        self._last_file_action_outcome = {
            "operation": plan.actions[0].operation if plan.actions else "quarantine",
            "applied": 0,
            "failed": 0,
        }
        j.start_job(max(1, len(plan.actions)))
        current_eligibility = evaluate_batch(
            self.results,
            tuple(action_dupes.values()),
            self.directories.current_file_pool,
        )
        if not current_eligibility.ok:
            reasons = sorted({item.message for _, item in current_eligibility.blocked})
            message = "\n".join(reasons)
            self.results.problems = [(dupe, message) for _, dupe in bound_plan.action_dupes]
            self._last_file_action_outcome["failed"] = len(self.results.problems)
            for _ in plan.actions:
                j.add_progress()
            return
        try:
            batch = self.quarantine_manager.prepare(plan)
        except (OSError, QuarantineError, ValueError) as error:
            self.results.problems = [(dupe, str(error)) for _, dupe in bound_plan.action_dupes]
            self._last_file_action_outcome["failed"] = len(self.results.problems)
            return

        if batch.failures:
            failures = {failure.action_id: failure for failure in batch.failures}
            for action_id, dupe in bound_plan.action_dupes:
                failure = failures.get(action_id)
                if failure is None:
                    message = tr("No files were changed because another batch preflight failed.")
                else:
                    message = "{}: {}".format(failure.code, failure.message)
                self.results.problems.append((dupe, message))
                j.add_progress()
            self._last_file_action_outcome["failed"] = len(self.results.problems)
            return

        managed_results = self.quarantine_manager.execute(batch)
        removed = []
        failed = []
        restorable_plan_paths = []
        for managed in managed_results:
            dupe = action_dupes.get(managed.action_id)
            if dupe is None:
                continue
            if managed.status == "applied":
                removed.append(dupe)
                if managed.safe_state == "staged":
                    restorable_plan_paths.append(managed.operation_plan_path)
            else:
                failed.append((dupe, "{}: {}".format(managed.failure_code, managed.message)))
                # A stage can move the target before a later journal failure.
                # Keep its recovery plan visible and stop presenting the
                # original path as a live result.
                if managed.safe_state == "staged":
                    removed.append(dupe)
                    restorable_plan_paths.append(managed.operation_plan_path)
            j.add_progress()

        if removed:
            self.results.remove_duplicates(removed)
        self.results.mark_none()
        self.results.problems = failed
        for dupe, _ in failed:
            if self.results.get_group_of_duplicate(dupe) is not None:
                self.results.mark(dupe)
        self.last_quarantine_plan_paths = tuple(restorable_plan_paths)
        self._last_file_action_outcome["applied"] = len(removed)
        self._last_file_action_outcome["failed"] = len(failed)

    def _do_restore_last_quarantine(self, j, plan_paths):
        failures = []
        restored = 0
        remaining = []
        j.start_job(max(1, len(plan_paths)))
        for plan_path in plan_paths:
            try:
                result = self.quarantine_manager.restore(Path(plan_path))
            except (OSError, QuarantineError, ValueError) as error:
                failures.append(str(error))
                remaining.append(plan_path)
            else:
                if result.status == "applied" and result.safe_state == "restored":
                    restored += 1
                else:
                    failures.append("{}: {}".format(result.failure_code, result.message))
                    remaining.append(plan_path)
            j.add_progress()
        self.last_quarantine_plan_paths = tuple(remaining)
        self._last_file_action_outcome = {
            "operation": "restore",
            "applied": restored,
            "failed": len(failures),
            "messages": tuple(failures),
        }

    def _create_file(self, path):
        # We add fs.Folder to fileclasses in case the file we're loading contains folder paths.
        return fs.get_file(path, self.fileclasses + [se.fs.Folder])

    def _get_file(self, str_path):
        path = Path(str_path)
        f = self._create_file(path)
        if f is None:
            return None
        try:
            f._read_all_info(attrnames=self.METADATA_TO_READ)
            return f
        except OSError:
            return None

    def _get_export_data(self):
        column_specs = tuple(
            (column.name, column.display)
            for column in self.result_table._columns.ordered_columns
            if column.visible and column.name != "marked"
        )
        colnames = [display for _name, display in column_specs]
        group_members = tuple((group_id, group, tuple(group)) for group_id, group in enumerate(self.results.groups))

        def iter_rows():
            for group_id, group, members in group_members:
                for dupe in members:
                    data = self.get_display_info(dupe, group)
                    yield [
                        group_id,
                        *(fix_surrogate_encoding(data[column_name]) for column_name, _display in column_specs),
                    ]

        return colnames, iter_rows()

    def _results_changed(self):
        self.selected_dupes = [d for d in self.selected_dupes if self.results.get_group_of_duplicate(d) is not None]
        self.notify("results_changed")

    def _start_job(self, jobid, func, args=()):
        title = JOBID2TITLE[jobid]
        try:
            self.progress_window.run(jobid, title, func, args=args)
        except job.JobInProgressError:
            msg = tr(
                "A previous action is still hanging in there. You can't start a new one yet. Wait "
                "a few seconds, then try again."
            )
            self.view.show_message(msg)

    def _job_completed(self, jobid):
        if jobid == JobType.SCAN:
            self._results_changed()
            fs.filesdb.commit()
            receipt = self.results.scan_receipt
            if receipt is not None and not receipt.complete:
                direct_discovery_issue = next(
                    (issue for issue in receipt.issues if issue.code.startswith("direct-discovery-resource-limit-")),
                    None,
                )
                if direct_discovery_issue is not None:
                    self.view.show_message(
                        tr(
                            "The scan stopped before matching because direct folder discovery "
                            "reached a safety limit. No partial results were analyzed, and bulk "
                            "file actions are disabled.\n\n{}\n\nFor a very large exact-match "
                            "library, select the Contents scan so the Persistent Catalog can "
                            "process it in bounded resumable batches."
                        ).format(direct_discovery_issue.message)
                    )
                else:
                    self.view.show_message(
                        tr(
                            "The scan is incomplete. Results are shown for review, but bulk file "
                            "actions are disabled until a complete rescan succeeds."
                        )
                    )
                if self.results.groups:
                    self.view.show_results_window()
            elif not self.results.groups:
                self.view.show_message(tr("No duplicates found."))
            else:
                self.view.show_results_window()
        if jobid in {JobType.MOVE, JobType.DELETE}:
            self._results_changed()
        if jobid == JobType.LOAD:
            self._recreate_result_table()
            self._results_changed()
            self.view.show_results_window()
        if jobid in {JobType.COPY, JobType.MOVE, JobType.DELETE}:
            if self.results.problems:
                self.problem_dialog.refresh()
                self.view.show_problem_dialog()
            else:
                if jobid == JobType.COPY:
                    msg = tr("All marked files were copied successfully.")
                elif jobid == JobType.MOVE:
                    msg = tr("All marked files were moved successfully.")
                else:
                    msg = tr("All selected byte-verified files were moved to recoverable quarantine.")
                self.view.show_message(msg)
        if jobid == JobType.RESTORE:
            outcome = self._last_file_action_outcome
            restored = int(outcome.get("applied", 0))
            failed = int(outcome.get("failed", 0))
            if failed:
                details = "\n".join(outcome.get("messages", ()))
                self.view.show_message(
                    tr("{} file(s) were restored; {} could not be restored.\n\n{}").format(restored, failed, details)
                )
            else:
                self.view.show_message(
                    tr(
                        "{} file(s) were restored to their original paths. " "Run a new scan to refresh the results."
                    ).format(restored)
                )

    def _job_error(self, jobid, err):
        if jobid == JobType.LOAD:
            msg = tr("Could not load file: {}").format(err)
            self.view.show_message(msg)
            return False
        else:
            raise err

    @staticmethod
    def _remove_hardlink_dupes(files, *, budget=None, j=job.nulljob):
        seen_identities = set()
        result = []
        for file in files:
            if budget is not None:
                budget.check_time(file.path)
            j.check_if_cancelled()
            try:
                file_stat = file.path.stat(follow_symlinks=False)
                identity = (file_stat.st_dev, file_stat.st_ino)
            except OSError:
                # Keep the entry when identity cannot be established; silently
                # discarding it could hide a real file from the scan.
                result.append(file)
                if budget is not None:
                    budget.check_time(file.path)
                j.check_if_cancelled()
                continue
            if not identity[0] or not identity[1]:
                # Some Windows and network filesystems expose zero when this
                # POSIX-shaped identity is unavailable.  Zero is not proof
                # that two directory entries are hard links.
                result.append(file)
                if budget is not None:
                    budget.check_time(file.path)
                j.check_if_cancelled()
                continue
            if identity not in seen_identities:
                seen_identities.add(identity)
                result.append(file)
            if budget is not None:
                budget.check_time(file.path)
            j.check_if_cancelled()
        return result

    def _select_dupes(self, dupes):
        if dupes == self.selected_dupes:
            return
        self.selected_dupes = dupes
        self.notify("dupes_selected")

    # --- Protected
    def _get_fileclasses(self):
        if self.app_mode == AppMode.PICTURE:
            return [pe.photo.PLAT_SPECIFIC_PHOTO_CLASS]
        elif self.app_mode == AppMode.MUSIC:
            return [me.fs.MusicFile]
        else:
            return [se.fs.File]

    def _prioritization_categories(self):
        if self.app_mode == AppMode.PICTURE:
            return pe.prioritize.all_categories()
        elif self.app_mode == AppMode.MUSIC:
            return me.prioritize.all_categories()
        else:
            return prioritize.all_categories()

    # --- Public
    def add_directory(self, d):
        """Adds folder ``d`` to :attr:`directories`.

        Shows an error message dialog if something bad happens.

        :param str d: path of folder to add
        """
        try:
            self.directories.add_path(Path(d))
            self.notify("directories_changed")
        except directories.AlreadyThereError:
            self.view.show_message(tr("'{}' already is in the list.").format(d))
        except directories.InvalidPathError:
            self.view.show_message(tr("'{}' does not exist.").format(d))

    def add_selected_to_ignore_list(self):
        """Adds :attr:`selected_dupes` to :attr:`ignore_list`."""
        dupes = self.without_ref(self.selected_dupes)
        if not dupes:
            self.view.show_message(MSG_NO_SELECTED_DUPES)
            return
        msg = tr("All selected %d matches are going to be ignored in all subsequent scans. Continue?")
        if not self.view.ask_yes_no(msg % len(dupes)):
            return
        selected_by_group = {}
        for dupe in dupes:
            group = self.results.get_group_of_duplicate(dupe)
            entry = selected_by_group.setdefault(
                id(group),
                {"group": group, "selected_ids": set()},
            )
            entry["selected_ids"].add(id(dupe))

        def relationships():
            for entry in selected_by_group.values():
                group = list(entry["group"])
                selected_ids = entry["selected_ids"]
                selected_indexes = [index for index, item in enumerate(group) if id(item) in selected_ids]
                selected_index_set = set(selected_indexes)
                for selected_index in selected_indexes:
                    selected = group[selected_index]
                    for other_index, other in enumerate(group):
                        if other_index == selected_index:
                            continue
                        # An edge between two selected members is yielded once.
                        if other_index in selected_index_set and other_index < selected_index:
                            continue
                        yield str(other.path), str(selected.path)

        try:
            self.ignore_list.ignore_many(relationships())
        except IgnoreListLimitError as error:
            self.view.show_message(
                tr(
                    "The selected matches were not added because the persistent "
                    "ignore list would exceed its safety limits. No ignore "
                    "relationships were changed.\n\n{}"
                ).format(error)
            )
            return
        self.remove_duplicates(dupes)
        self.ignore_list_dialog.refresh()

    def apply_filter(self, result_filter):
        """Apply a filter ``filter`` to the results so that it shows only dupe groups that match it.

        :param str filter: filter to apply
        """
        self.results.apply_filter(None)
        if self.options["escape_filter_regexp"]:
            result_filter = escape(result_filter, set("()[]\\.|+?^"))
            result_filter = escape(result_filter, "*", ".")
        self.results.apply_filter(result_filter)
        self._results_changed()

    def clean_empty_dirs(self, path, boundary):
        if self.options["clean_empty_dirs"]:
            remove_empty_directories(path, boundary)

    def clear_picture_cache(self):
        cache_path = self._get_picture_cache_path()
        if not os.path.lexists(cache_path):
            return
        cache = SqliteCache(cache_path)
        try:
            cache.clear()
        finally:
            cache.close()

    def clear_hash_cache(self):
        fs.filesdb.clear()

    def copy_or_move(
        self,
        dupe,
        copy: bool,
        destination: str,
        dest_type: DestType,
        expected_source_snapshot,
    ):
        source_path = Path(dupe.path)
        location_candidates = [Path(root) for root in self.directories if Path(root) in source_path.parents]
        if not location_candidates:
            raise OSError(errno.EPERM, "The source is outside the selected directory roots", str(source_path))
        location_path = max(location_candidates, key=lambda candidate: len(candidate.parts))
        dest_path = Path(destination)
        if dest_type in {DestType.RELATIVE, DestType.ABSOLUTE}:
            # no filename, no windows drive letter
            source_base = source_path.relative_to(source_path.anchor).parent
            if dest_type == DestType.RELATIVE:
                source_base = source_base.relative_to(location_path.relative_to(location_path.anchor))
            dest_path = dest_path.joinpath(source_base)
        validate_source_destination(source_path, dest_path)
        ensure_plain_directory(dest_path)
        # Add filename to dest_path. For file move/copy, it's not required, but for folders, yes.
        dest_path = dest_path.joinpath(source_path.name)
        logging.debug("Copy/Move operation from '%s' to '%s'", source_path, dest_path)
        # Raises an EnvironmentError if there's a problem
        rename_no_replace = platform_file_system().rename_no_replace_bound
        if copy:
            smart_copy(
                source_path,
                dest_path,
                rename_no_replace=rename_no_replace,
                expected_source_snapshot=expected_source_snapshot,
            )
        else:
            if self.options["clean_empty_dirs"]:
                validate_cleanup_path(source_path.parent, location_path)
            smart_move(
                source_path,
                dest_path,
                rename_no_replace=rename_no_replace,
                expected_source_snapshot=expected_source_snapshot,
            )
            try:
                self.clean_empty_dirs(source_path.parent, location_path)
            except OSError as error:
                # The move already committed atomically. Cleanup is optional and fail-closed:
                # report it in the log without misrepresenting the successfully moved file as
                # still present in the results.
                logging.warning("Could not remove an empty source directory: %s", error)

    def copy_or_move_marked(self, copy):
        """Start an async move (or copy) job on marked duplicates.

        :param bool copy: If True, duplicates will be copied instead of moved
        """

        marked = tuple(dupe for dupe in self.results.dupes if self.results.is_marked(dupe))

        def do(j):
            current_eligibility = evaluate_relocation_batch(
                self.results,
                marked,
                self.directories.current_file_pool,
            )
            if not current_eligibility.ok:
                reasons = sorted({item.message for _, item in current_eligibility.blocked})
                message = "\n".join(reasons)
                self.results.problems = [(dupe, message) for dupe in marked]
                j.start_job(max(1, len(marked)))
                for _ in marked:
                    j.add_progress()
                return

            def op(dupe):
                j.add_progress()
                eligibility = evaluate_relocation_batch(
                    self.results,
                    (dupe,),
                    self.directories.current_file_pool,
                )
                if not eligibility.ok:
                    reason = eligibility.blocked[0][1].message
                    raise OSError(errno.ESTALE, reason, str(dupe.path))
                expected_source_snapshot = dupe.validate_review_scan()
                self.copy_or_move(
                    dupe,
                    copy,
                    destination,
                    desttype,
                    expected_source_snapshot,
                )

            j.start_job(self.results.mark_count)
            self.results.perform_on_marked(op, not copy)

        if not self.results.mark_count:
            self.view.show_message(MSG_NO_MARKED_DUPES)
            return
        eligibility = evaluate_relocation_batch(
            self.results,
            marked,
            self.directories.current_file_pool,
        )
        if not eligibility.ok:
            reasons = sorted({item.message for _, item in eligibility.blocked})
            operation = tr("copied") if copy else tr("moved")
            self.view.show_message(
                tr(
                    "No files were {}. Organizer operations require a complete current scan and "
                    "an Incoming Files target.\n\n{}"
                ).format(operation, "\n".join(reasons))
            )
            return
        destination = self.view.select_dest_folder(
            tr("Select a directory to copy marked files to")
            if copy
            else tr("Select a directory to move marked files to")
        )
        if destination:
            desttype = self.options["copymove_dest_type"]
            jobid = JobType.COPY if copy else JobType.MOVE
            self._start_job(jobid, do)

    def delete_marked(self):
        """Build fresh proofs and move marked exact duplicates to quarantine."""
        if not self.results.mark_count:
            self.view.show_message(MSG_NO_MARKED_DUPES)
            return
        marked = [dupe for dupe in self.results.dupes if self.results.is_marked(dupe)]
        eligibility = evaluate_batch(
            self.results,
            marked,
            self.directories.current_file_pool,
        )
        if not eligibility.ok:
            reasons = sorted({item.message for _, item in eligibility.blocked})
            message = tr(
                "No files were changed. Bulk actions require a complete current scan and "
                "byte-verified exact duplicates.\n\n{}"
            ).format("\n".join(reasons))
            self.view.show_message(message)
            return
        if not self.deletion_options.show(self.results.mark_count):
            return
        try:
            bound_plan = build_bound_deletion_plan(
                self.results,
                marked,
                tuple(self.directories),
                current_pool_resolver=self.directories.current_file_pool,
            )
        except (OSError, ActionPlanError, ValueError) as error:
            self.view.show_message(
                tr("No files were changed because a safe action plan could not be built:\n\n{}").format(error)
            )
            return
        logging.debug(
            "Starting proof-bound quarantine job with %d actions",
            len(bound_plan.plan.actions),
        )
        self._start_job(JobType.DELETE, self._do_delete, args=(bound_plan,))

    def restore_last_quarantine(self):
        """Restore the most recent GUI quarantine batch without overwriting paths."""

        plan_paths = tuple(self.last_quarantine_plan_paths)
        if not plan_paths:
            self.view.show_message(
                tr(
                    "There is no restorable quarantine batch in this session. "
                    "Use the quarantine CLI list command to inspect older batches."
                )
            )
            return
        prompt = tr(
            "Restore {} quarantined file(s) to their original paths? " "Existing paths will never be overwritten."
        ).format(len(plan_paths))
        if not self.view.ask_yes_no(prompt):
            return
        self._start_job(JobType.RESTORE, self._do_restore_last_quarantine, args=(plan_paths,))

    def export_to_xhtml(self):
        """Export current results to XHTML.

        The configuration of the :attr:`result_table` (columns order and visibility) is used to
        determine how the data is presented in the export. In other words, the exported table in
        the resulting XHTML will look just like the results table.
        """
        colnames, rows = self._get_export_data()
        export_path = export.export_to_xhtml(colnames, rows)
        desktop.open_path(export_path)

    def export_to_csv(self):
        """Export current results to CSV.

        The columns and their order in the resulting CSV file is determined in the same way as in
        :meth:`export_to_xhtml`.
        """
        dest_file = self.view.select_dest_file(tr("Select a destination for your exported CSV"), "csv")
        if dest_file:
            colnames, rows = self._get_export_data()
            try:
                export.export_to_csv(dest_file, colnames, rows)
            except OSError as e:
                self.view.show_message(tr("Couldn't write to file: {}").format(str(e)))

    def get_display_info(self, dupe, group, delta=False):
        def empty_data():
            return {c.name: "---" for c in self.result_table.COLUMNS[1:]}

        if (dupe is None) or (group is None):
            return empty_data()
        try:
            return dupe.get_display_info(group, delta)
        except Exception as e:
            logging.warning("Exception (type: %s) on GetDisplayInfo for %s: %s", type(e), str(dupe.path), str(e))
            return empty_data()

    def invoke_custom_command(self):
        """Calls command in ``CustomCommand`` pref with ``%d`` and ``%r`` placeholders replaced.

        Using the current selection, ``%d`` is replaced with the currently selected dupe and ``%r``
        is replaced with that dupe's ref file. If there's no selection, the command is not invoked.
        If the dupe is a ref, ``%d`` and ``%r`` will be the same.
        """
        cmd = self.view.get_default("CustomCommand")
        if not cmd:
            msg = tr("You have no custom command set up. Set it up in your preferences.")
            self.view.show_message(msg)
            return
        if not self.selected_dupes:
            return
        dupes = self.selected_dupes
        if not self.view.ask_yes_no(MSG_CUSTOM_COMMAND_BOUNDARY.format(len(dupes))):
            return
        refs = [self.results.get_group_of_duplicate(dupe).ref for dupe in dupes]
        for dupe, ref in zip(dupes, refs):
            try:
                argv = parse_custom_command(cmd, dupe.path, ref.path)
                completed = subprocess.run(
                    argv,
                    shell=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except (OSError, ValueError) as error:
                logging.warning("Custom command could not be started: %s", type(error).__name__)
                self.view.show_message(tr("The custom command could not be started."))
                continue
            logging.info(
                "Custom command %s finished with exit code %d",
                op.basename(argv[0]),
                completed.returncode,
            )

    def load(self):
        """Load directory selection and ignore list from files in appdata.

        This method is called during startup so that directory selection and ignore list, which
        is persistent data, is the same as when the last session was closed (when :meth:`save` was
        called).
        """
        p = op.join(self.appdata, "last_directories.xml")
        directories_failure = self.directories.load_from_file(p) if op.exists(p) else None
        if directories_failure is not None:
            self._directories_failed_revision = self.directories.revision
            self.view.show_message(
                tr(
                    "The folder list could not be loaded because it is invalid "
                    "or exceeds the supported safety limits. The current folder "
                    "list was kept and the invalid source will not be replaced "
                    "unless the folder selection is changed.\n\n{}"
                ).format(directories_failure)
            )
        else:
            self._directories_failed_revision = None
        self.notify("directories_changed")
        p = op.join(self.appdata, "ignore_list.xml")
        ignore_failure = self.ignore_list.load_from_xml(p) if op.exists(p) else None
        if ignore_failure is not None:
            self._ignore_list_failed_revision = self.ignore_list.revision
            self.view.show_message(
                tr(
                    "The ignore list could not be loaded because it is invalid "
                    "or exceeds the supported safety limits. The current ignore "
                    "list was kept.\n\n{}"
                ).format(ignore_failure)
            )
        else:
            self._ignore_list_failed_revision = None
        self.ignore_list_dialog.refresh()
        p = op.join(self.appdata, "exclude_list.xml")
        exclude_failure = self.exclude_list.load_from_xml(p)
        if exclude_failure is not None and op.exists(p):
            self._exclude_list_failed_revision = self.exclude_list.revision
            self.view.show_message(
                tr(
                    "The exclusion list could not be loaded because it is invalid "
                    "or exceeds the supported safety limits. The current exclusion "
                    "list was kept.\n\n{}"
                ).format(exclude_failure)
            )
        else:
            self._exclude_list_failed_revision = None
        self.exclude_list_dialog.refresh()

    def load_directories(self, filepath):
        failure = self.directories.load_from_file(filepath)
        if failure is not None:
            self.view.show_message(
                tr(
                    "The folder list could not be loaded because it is invalid or exceeds "
                    "the supported safety limits. The current folder list was kept."
                )
            )
            return failure
        self.notify("directories_changed")
        return None

    def load_from(self, filename):
        """Start an async job to load results from ``filename``.

        :param str filename: path of the XML file (created with :meth:`save_as`) to load
        """

        def do(j):
            self.results.load_from_xml(filename, self._get_file, j)

        self._start_job(JobType.LOAD, do)

    def make_selected_reference(self):
        """Promote :attr:`selected_dupes` to reference position within their respective groups.

        Each selected dupe will become the :attr:`~core.engine.Group.ref` of its group. If there's
        more than one dupe selected for the same group, only the first (in the order currently shown
        in :attr:`result_table`) dupe will be promoted.
        """
        dupes = self.without_ref(self.selected_dupes)
        changed_groups = set()
        for dupe in dupes:
            g = self.results.get_group_of_duplicate(dupe)
            if g not in changed_groups and self.results.make_ref(dupe):
                changed_groups.add(g)
        # It's not always obvious to users what this action does, so to make it a bit clearer,
        # we change our selection to the ref of all changed groups. However, we also want to keep
        # the files that were ref before and weren't changed by the action. In effect, what this
        # does is that we keep our old selection, but remove all non-ref dupes from it.
        # If no group was changed, however, we don't touch the selection.
        if not self.result_table.power_marker:
            if changed_groups:
                self.selected_dupes = [
                    d for d in self.selected_dupes if self.results.get_group_of_duplicate(d).ref is d
                ]
            self.notify("results_changed")
        else:
            # If we're in "Dupes Only" mode (previously called Power Marker), things are a bit
            # different. The refs are not shown in the table, and if our operation is successful,
            # this means that there's no way to follow our dupe selection. Then, the best thing to
            # do is to keep our selection index-wise (different dupe selection, but same index
            # selection).
            self.notify("results_changed_but_keep_selection")

    def mark_all(self):
        """Set all dupes in the results as marked."""
        self.results.mark_all()
        self.notify("marking_changed")

    def mark_none(self):
        """Set all dupes in the results as unmarked."""
        self.results.mark_none()
        self.notify("marking_changed")

    def mark_invert(self):
        """Invert the marked state of all dupes in the results."""
        self.results.mark_invert()
        self.notify("marking_changed")

    def mark_dupe(self, dupe, marked):
        """Change marked status of ``dupe``.

        :param dupe: dupe to mark/unmark
        :type dupe: :class:`~core.fs.File`
        :param bool marked: True = mark, False = unmark
        """
        if marked:
            self.results.mark(dupe)
        else:
            self.results.unmark(dupe)
        self.notify("marking_changed")

    def open_selected(self):
        """Open :attr:`selected_dupes` with their associated application."""
        if len(self.selected_dupes) > 10 and not self.view.ask_yes_no(MSG_MANY_FILES_TO_OPEN):
            return
        for dupe in self.selected_dupes:
            desktop.open_path(dupe.path)

    def purge_ignore_list(self):
        """Remove files that don't exist from :attr:`ignore_list`."""
        self.ignore_list.filter(lambda f, s: op.exists(f) and op.exists(s))
        self.ignore_list_dialog.refresh()

    def remove_directories(self, indexes):
        """Remove root directories at ``indexes`` from :attr:`directories`.

        :param indexes: Indexes of the directories to remove.
        :type indexes: list of int
        """
        try:
            indexes = sorted(indexes, reverse=True)
            for index in indexes:
                del self.directories[index]
            self.notify("directories_changed")
        except IndexError:
            pass

    def remove_duplicates(self, duplicates):
        """Remove ``duplicates`` from :attr:`results`.

        Calls :meth:`~core.results.Results.remove_duplicates` and send appropriate notifications.

        :param duplicates: duplicates to remove.
        :type duplicates: list of :class:`~core.fs.File`
        """
        self.results.remove_duplicates(self.without_ref(duplicates))
        self.notify("results_changed_but_keep_selection")

    def remove_marked(self):
        """Removed marked duplicates from the results (without touching the files themselves)."""
        if not self.results.mark_count:
            self.view.show_message(MSG_NO_MARKED_DUPES)
            return
        msg = tr("You are about to remove %d files from results. Continue?")
        if not self.view.ask_yes_no(msg % self.results.mark_count):
            return
        self.results.perform_on_marked(lambda x: None, True)
        self._results_changed()

    def remove_selected(self):
        """Removed :attr:`selected_dupes` from the results (without touching the files themselves)."""
        dupes = self.without_ref(self.selected_dupes)
        if not dupes:
            self.view.show_message(MSG_NO_SELECTED_DUPES)
            return
        msg = tr("You are about to remove %d files from results. Continue?")
        if not self.view.ask_yes_no(msg % len(dupes)):
            return
        self.remove_duplicates(dupes)

    def rename_selected(self, newname):
        """Renames the selected dupes's file to ``newname``.

        If there's more than one selected dupes, the first one is used.

        :param str newname: The filename to rename the dupe's file to.
        """
        try:
            d = self.selected_dupes[0]
            eligibility = evaluate_rename(
                self.results,
                d,
                self.directories.current_file_pool,
            )
            if not eligibility.allowed:
                self.view.show_message(tr("The file was not renamed. {}").format(eligibility.message))
                return False
            d.rename(newname)
            previous_receipt = self.results.scan_receipt
            discovered = max(1, int(getattr(previous_receipt, "discovered", 1)))
            self.results.scan_receipt = ScanReceipt.incomplete(
                discovered=discovered,
                analyzed=max(0, discovered - 1),
                skipped=1,
                issues=(
                    ScanIssue(
                        code="result_path_changed",
                        path=str(d.path),
                        message=(
                            "A result file was renamed after the scan; " "run a new scan before changing more files."
                        ),
                    ),
                ),
            )
            return True
        except (IndexError, fs.FSError) as e:
            logging.warning("dupeGuru Warning: %s" % str(e))
        return False

    def reprioritize_groups(self, sort_key):
        """Sort dupes in each group (in :attr:`results`) according to ``sort_key``.

        Called by the re-prioritize dialog. Calls :meth:`~core.engine.Group.prioritize` and, once
        the sorting is done, show a message that confirms the action.

        :param sort_key: The key being sent to :meth:`~core.engine.Group.prioritize`
        :type sort_key: f(dupe)
        """
        count = 0
        for group in self.results.groups:
            if group.prioritize(key_func=sort_key):
                count += 1
        if count:
            self.results.refresh_required = True
        self._results_changed()
        msg = tr("{} duplicate groups were changed by the re-prioritization.").format(count)
        self.view.show_message(msg)

    def reveal_selected(self):
        if self.selected_dupes:
            desktop.reveal_path(self.selected_dupes[0].path)

    def save(self):
        if not op.exists(self.appdata):
            os.makedirs(self.appdata)
        p = op.join(self.appdata, "last_directories.xml")
        if self._directories_failed_revision == self.directories.revision and op.exists(p):
            logging.warning(
                "Preserving the invalid directory-selection source because it was not modified after the load failure"
            )
        else:
            try:
                self.directories.save_to_file(p)
            except directories.DirectoriesSaveError as error:
                logging.warning(
                    "The folder list was not saved because it failed bounded validation: %s",
                    error,
                )
                self.view.show_message(
                    tr(
                        "The folder list was not saved because it is invalid or "
                        "exceeds the supported safety limits. Any existing folder-list "
                        "file was kept unchanged.\n\n{}"
                    ).format(error)
                )
            else:
                self._directories_failed_revision = None
        p = op.join(self.appdata, "ignore_list.xml")
        if self._ignore_list_failed_revision == self.ignore_list.revision and op.exists(p):
            logging.warning(
                "Preserving the invalid ignore-list source because it was not modified after the load failure"
            )
        else:
            self.ignore_list.save_to_xml(p)
            self._ignore_list_failed_revision = None
        p = op.join(self.appdata, "exclude_list.xml")
        if self._exclude_list_failed_revision == self.exclude_list.revision and op.exists(p):
            logging.warning(
                "Preserving the invalid exclusion-list source because it was not modified after the load failure"
            )
        else:
            try:
                self.exclude_list.save_to_xml(p)
            except ExcludeListLimitError as error:
                logging.warning(
                    "The exclusion list was not saved because it failed bounded validation: %s",
                    error,
                )
                self.view.show_message(
                    tr(
                        "The exclusion list was not saved because it is invalid "
                        "or exceeds the supported safety limits. Any existing "
                        "exclusion-list file was kept unchanged.\n\n{}"
                    ).format(error)
                )
            else:
                self._exclude_list_failed_revision = None
        self.notify("save_session")

    def close(self):
        fs.filesdb.close()

    def save_as(self, filename):
        """Save results in ``filename``.

        :param str filename: path of the file to save results (as XML) to.
        """
        try:
            self.results.save_to_xml(filename)
        except (OSError, ValueError) as e:
            self.view.show_message(tr("Couldn't write to file: {}").format(str(e)))

    def save_directories_as(self, filename):
        """Save directories in ``filename``.

        :param str filename: path of the file to save directories (as XML) to.
        """
        try:
            self.directories.save_to_file(filename)
        except (OSError, directories.DirectoriesSaveError) as e:
            self.view.show_message(tr("Couldn't write to file: {}").format(str(e)))

    def _catalog_selected_roots(self):
        non_excluded_override_ancestors = self.directories._non_excluded_override_ancestors()
        return tuple(
            Path(path)
            for path in self.directories
            if self.directories._directory_prune_reason(
                Path(path),
                non_excluded_override_ancestors,
            )
            is None
        )

    def _direct_discovery_limits(self):
        return directories.DirectDiscoveryLimits(
            max_files=self.options["direct_scan_max_files"],
            max_folders=self.options["direct_scan_max_folders"],
            max_issues=self.options["direct_scan_max_issues"],
            max_seconds=self.options["direct_scan_max_seconds"],
        )

    @staticmethod
    def _direct_discovery_resource_receipt(error, folder_scan):
        discovered = error.folders if folder_scan else error.files
        return ScanReceipt.incomplete(
            discovered=discovered,
            analyzed=0,
            skipped=discovered,
            issues=(
                ScanIssue(
                    code="direct-discovery-{}".format(error.code),
                    path=error.path,
                    message=str(error),
                ),
            ),
            status=ScanStatus.RESOURCE_LIMIT,
        )

    def _record_direct_discovery_resource_failure(self, error, folder_scan):
        logging.warning("Direct filesystem discovery stopped at a resource limit: %s", error)
        self.results.groups = []
        self.results.scan_receipt = self._direct_discovery_resource_receipt(
            error,
            folder_scan,
        )
        self.discarded_file_count = 0

    @staticmethod
    def _bind_direct_scan_generations(files, j):
        """Capture one fail-closed organizer baseline for every direct-scan file."""

        for index, file in enumerate(files):
            if index % 256 == 0:
                j.check_if_cancelled()
            begin = getattr(file, "begin_review_scan", None)
            if begin is None:
                raise fs.FileChangedError(
                    "A direct-scan input cannot provide a stable organizer baseline: {}".format(
                        getattr(file, "path", "<unknown>"),
                    )
                )
            begin()

    @staticmethod
    def _validate_direct_scan_generations(files, j):
        """Require every direct-scan input to remain on its captured generation."""

        for index, file in enumerate(files):
            if index % 256 == 0:
                j.check_if_cancelled()
            validate = getattr(file, "validate_review_scan", None)
            if validate is None:
                raise fs.FileChangedError(
                    "A direct-scan input lost its organizer baseline: {}".format(
                        getattr(file, "path", "<unknown>"),
                    )
                )
            validate()

    def _record_scan_generation_failure(self, error, file_count, *, after_matching):
        logging.warning("Direct scan did not retain stable file generations: %s", error)
        discovered = max(1, file_count)
        analyzed = max(0, discovered - 1) if after_matching else 0
        skipped = 0 if after_matching else max(0, discovered - 1)
        self.results.groups = []
        self.results.scan_receipt = ScanReceipt.incomplete(
            discovered=discovered,
            analyzed=analyzed,
            skipped=skipped,
            failed=1,
            issues=(
                ScanIssue(
                    code="scan_generation_changed",
                    path=str(getattr(error, "filename", "") or ""),
                    message=str(error),
                ),
            ),
            status=ScanStatus.FAILED,
        )
        self.discarded_file_count = 0

    def _catalog_file_filter(self, path):
        if self.directories.get_state(path.parent) == directories.DirectoryState.EXCLUDED:
            return "file is inside an excluded directory"
        if self.directories._file_is_excluded(path):
            return "file is excluded by ExcludeList"
        if not any(fileclass.can_handle(path) for fileclass in self.fileclasses):
            return "file type is not supported by this application mode"
        return None

    @staticmethod
    def _catalog_cancel_requested(j):
        try:
            j.check_if_cancelled()
        except job.JobCancelled:
            return True
        return False

    @staticmethod
    def _catalog_incomplete_receipt(service_result, message):
        failed = max(
            1,
            service_result.status.error_count + service_result.work_failed,
        )
        discovered = max(
            service_result.files_observed,
            service_result.status.work_counts["total"],
            failed,
        )
        analyzed = max(0, discovered - failed)
        return ScanReceipt.incomplete(
            discovered=discovered,
            analyzed=analyzed,
            failed=failed,
            issues=(
                ScanIssue(
                    code="catalog_scan_partial",
                    message=message,
                ),
            ),
        )

    @staticmethod
    def _catalog_projection_limit_receipt(service_result, counts, projected_groups, projected_files):
        discovered = max(1, service_result.files_observed)
        return ScanReceipt.incomplete(
            discovered=discovered,
            analyzed=discovered - 1,
            skipped=1,
            issues=(
                ScanIssue(
                    code="catalog_projection_limit",
                    message=(
                        "The complete catalog projection exceeds GUI safety limits "
                        "(groups={groups}, duplicate files={files}, largest group={largest}; "
                        "displayed complete groups={shown_groups}, files={shown_files}). "
                        "Displayed results are review-only."
                    ).format(
                        groups=counts.group_count,
                        files=counts.file_count,
                        largest=counts.max_group_members,
                        shown_groups=projected_groups,
                        shown_files=projected_files,
                    ),
                ),
            ),
            status=ScanStatus.RESOURCE_LIMIT,
        )

    def _materialize_catalog_exact_group(self, service, catalog_group):
        files = []
        for item in catalog_group.files:
            path = item.path
            state = self.directories.get_state(path.parent)
            if state == directories.DirectoryState.EXCLUDED or self.directories._file_is_excluded(path):
                continue
            file = fs.get_file(path, fileclasses=self.fileclasses)
            if file is None:
                raise CatalogStateError("Catalog path '{}' no longer has a supported file type".format(path))
            if not service.worker.hydrate_file(
                file,
                item.content_version_id,
            ):
                raise CatalogStateError(
                    "Catalog generation {} could not hydrate '{}'".format(
                        item.content_version_id,
                        path,
                    )
                )
            self.directories._apply_file_state(file, state)
            files.append(file)
        if len(files) < 2:
            return None
        evidence = engine.ExactEvidence(
            kind=engine.VerificationKind.VERIFIED_EXACT,
            algorithm="sha256",
            digest=catalog_group.full_digest,
            size=catalog_group.size,
        )
        return engine.Group.from_exact_files(files, evidence)

    def _run_catalog_contents_scan(self, scanner, j):
        roots = self._catalog_selected_roots()
        if not roots:
            self.results.groups = []
            self.results.scan_receipt = ScanReceipt.incomplete(
                discovered=1,
                analyzed=0,
                skipped=1,
                issues=(
                    ScanIssue(
                        code="no_catalog_roots",
                        message="All selected roots are excluded",
                    ),
                ),
            )
            return

        roots_key = tuple(os.path.normcase(os.path.abspath(str(root))) for root in roots)

        def cancel_check():
            return self._catalog_cancel_requested(j)

        service = None
        service_result = None
        try:
            service = CatalogService(
                Path(self.appdata) / "catalog.sqlite3",
                roots,
                directory_pruner=self.directories._directory_prune_reason,
                file_filter=self._catalog_file_filter,
            )
            if self._catalog_resume_scan_id is not None and self._catalog_resume_roots == roots_key:
                service_result = service.resume(
                    self._catalog_resume_scan_id,
                    cancel_check=cancel_check,
                )
            else:
                service_result = service.run(cancel_check=cancel_check)

            if (
                service_result.outcome != "finished"
                or service_result.catalog_status != "complete"
                or not service_result.status.verified_projection_allowed
            ):
                self.results.groups = []
                detail = "; ".join(service_result.errors) or "catalog scan is incomplete"
                self.results.scan_receipt = self._catalog_incomplete_receipt(
                    service_result,
                    detail,
                )
                if service_result.catalog_status == "running":
                    self._catalog_resume_scan_id = service_result.scan_id
                    self._catalog_resume_roots = roots_key
                else:
                    self._catalog_resume_scan_id = None
                    self._catalog_resume_roots = ()
                return

            projection_counts = service.verified_exact_projection_counts()
            projection_limited = (
                projection_counts.group_count > CATALOG_GUI_MAX_EXACT_GROUPS
                or projection_counts.file_count > CATALOG_GUI_MAX_EXACT_FILES
                or projection_counts.max_group_members > CATALOG_GUI_MAX_EXACT_GROUP_MEMBERS
            )
            projected_groups = 0
            projected_files = 0
            compact_groups = engine.ExactGroupList()
            if cancel_check():
                self._catalog_resume_scan_id = None
                self._catalog_resume_roots = ()
                self.results.groups = []
                self.results.scan_receipt = self._catalog_incomplete_receipt(
                    service_result,
                    "catalog projection was interrupted",
                )
                return
            for catalog_group in service.iter_verified_exact_groups(
                page_size=CATALOG_GUI_EXACT_PAGE_GROUPS,
                max_page_files=CATALOG_GUI_EXACT_PAGE_FILES,
                max_group_members=CATALOG_GUI_MAX_EXACT_GROUP_MEMBERS,
            ):
                if cancel_check():
                    self._catalog_resume_scan_id = None
                    self._catalog_resume_roots = ()
                    self.results.groups = []
                    self.results.scan_receipt = self._catalog_incomplete_receipt(
                        service_result,
                        "catalog projection was interrupted",
                    )
                    return
                member_count = len(catalog_group.files)
                if (
                    projected_groups >= CATALOG_GUI_MAX_EXACT_GROUPS
                    or projected_files + member_count > CATALOG_GUI_MAX_EXACT_FILES
                ):
                    projection_limited = True
                    break
                projected_groups += 1
                projected_files += member_count
                group = self._materialize_catalog_exact_group(service, catalog_group)
                if group is not None:
                    compact_groups.append(group)
            self.results.groups = scanner.get_dupe_groups_from_verified_exact(
                compact_groups,
                self.ignore_list,
                j,
            )
            if projection_limited:
                self.results.scan_receipt = self._catalog_projection_limit_receipt(
                    service_result,
                    projection_counts,
                    projected_groups,
                    projected_files,
                )
            else:
                self.results.scan_receipt = ScanReceipt.completed(service_result.files_observed)
            self.discarded_file_count = scanner.discarded_file_count
            self._catalog_resume_scan_id = None
            self._catalog_resume_roots = ()
        except job.JobCancelled:
            self.results.groups = []
            if service_result is None:
                self.results.scan_receipt = ScanReceipt.incomplete(
                    discovered=1,
                    analyzed=0,
                    failed=1,
                    issues=(
                        ScanIssue(
                            code="catalog_scan_partial",
                            message="catalog scan was interrupted",
                        ),
                    ),
                )
            else:
                self.results.scan_receipt = self._catalog_incomplete_receipt(
                    service_result,
                    "catalog projection was interrupted",
                )
                if service_result.catalog_status == "running":
                    self._catalog_resume_scan_id = service_result.scan_id
                    self._catalog_resume_roots = roots_key
                else:
                    self._catalog_resume_scan_id = None
                    self._catalog_resume_roots = ()
        except (
            CatalogServiceError,
            CatalogStateError,
            CatalogWorkerError,
            OSError,
            ValueError,
        ) as error:
            self.results.groups = []
            self.results.scan_receipt = ScanReceipt.incomplete(
                discovered=1,
                analyzed=0,
                failed=1,
                issues=(
                    ScanIssue(
                        code="catalog_projection_failed",
                        message=str(error),
                    ),
                ),
            )
            self._catalog_resume_scan_id = None
            self._catalog_resume_roots = ()
        finally:
            if service is not None:
                service.close()

    def start_scanning(self, profile_scan=False):
        """Starts an async job to scan for duplicates.

        Scans folders selected in :attr:`directories` and put the results in :attr:`results`
        """
        scanner = self.SCANNER_CLASS()
        fs.filesdb.ignore_mtime = self.options["rehash_ignore_mtime"] is True
        # Configure the scanner before choosing the collection engine because
        # CONTENTS scans use the durable catalog rather than the legacy walk.
        for k, v in self.options.items():
            if hasattr(scanner, k):
                setattr(scanner, k, v)
        if self.app_mode == AppMode.PICTURE:
            scanner.cache_path = self._get_picture_cache_path()
        # Do not perform an unbounded synchronous preflight walk.  The actual
        # asynchronous collection below owns the one shared discovery budget.
        has_scan_input = bool(len(self.directories))
        if not has_scan_input:
            self.view.show_message(tr("The selected directories contain no scannable file."))
            return
        self.results.groups = []
        self._recreate_result_table()
        self._results_changed()

        def do(j):
            if profile_scan:
                pr = cProfile.Profile()
                pr.enable()
            j.set_progress(0, tr("Collecting files to scan"))
            if scanner.scan_type == ScanType.CONTENTS:
                self._run_catalog_contents_scan(scanner, j)
            else:
                folder_scan = scanner.scan_type == ScanType.FOLDERS
                budget = directories.DirectDiscoveryBudget(
                    self._direct_discovery_limits(),
                )
                try:
                    try:
                        if folder_scan:
                            files = list(
                                self.directories.get_folders(
                                    folderclass=se.fs.Folder,
                                    j=j,
                                    budget=budget,
                                )
                            )
                        else:
                            files = list(
                                self.directories.get_files(
                                    fileclasses=self.fileclasses,
                                    j=j,
                                    budget=budget,
                                )
                            )
                        if self.options["ignore_hardlink_matches"]:
                            files = self._remove_hardlink_dupes(
                                files,
                                budget=budget,
                                j=j,
                            )
                        logging.info("Scanning %d files" % len(files))
                        if not folder_scan:
                            try:
                                self._bind_direct_scan_generations(files, j)
                            except (OSError, TypeError, ValueError) as error:
                                self._record_scan_generation_failure(
                                    error,
                                    len(files),
                                    after_matching=False,
                                )
                                return
                        groups = scanner.get_dupe_groups(files, self.ignore_list, j)
                        if not folder_scan:
                            try:
                                self._validate_direct_scan_generations(files, j)
                            except (OSError, TypeError, ValueError) as error:
                                self._record_scan_generation_failure(
                                    error,
                                    len(files),
                                    after_matching=True,
                                )
                                return
                        stored_coverages = getattr(
                            self.directories,
                            "last_walk_coverages",
                            (),
                        )
                        walk_coverages = (
                            tuple(stored_coverages.values())
                            if hasattr(stored_coverages, "values")
                            else tuple(stored_coverages)
                        )
                        receipt = receipt_from_walk_coverages(len(files), walk_coverages)
                        matcher_receipt = getattr(scanner, "scan_receipt", None)
                        if matcher_receipt is not None and not getattr(
                            matcher_receipt,
                            "allows_destructive_actions",
                            False,
                        ):
                            receipt = matcher_receipt
                    except MemoryError as error:
                        raise budget.memory_error() from error
                except directories.DirectDiscoveryResourceError as error:
                    self._record_direct_discovery_resource_failure(
                        error,
                        folder_scan,
                    )
                else:
                    # Publish results only after collection and matching both
                    # completed. A resource failure can therefore never expose
                    # or analyze a partial discovery list.
                    self.results.groups = groups
                    self.results.scan_receipt = receipt
                    self.discarded_file_count = scanner.discarded_file_count
            if profile_scan:
                pr.disable()
                pr.dump_stats(op.join(self.appdata, f"{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}.profile"))

        self._start_job(JobType.SCAN, do)

    def toggle_selected_mark_state(self):
        selected = self.without_ref(self.selected_dupes)
        if not selected:
            return
        if allsame(self.results.is_marked(d) for d in selected):
            markfunc = self.results.mark_toggle
        else:
            markfunc = self.results.mark
        for dupe in selected:
            markfunc(dupe)
        self.notify("marking_changed")

    def without_ref(self, dupes):
        """Returns ``dupes`` with all reference elements removed."""
        return [dupe for dupe in dupes if self.results.get_group_of_duplicate(dupe).ref is not dupe]

    def get_default(self, key, fallback_value=None):
        result = nonone(self.view.get_default(key), fallback_value)
        if fallback_value is not None and not isinstance(result, type(fallback_value)):
            # we don't want to end up with garbage values from the prefs
            try:
                result = type(fallback_value)(result)
            except Exception:
                result = fallback_value
        return result

    def set_default(self, key, value):
        self.view.set_default(key, value)

    # --- Properties
    @property
    def stat_line(self):
        result = self.results.stat_line
        if self.discarded_file_count:
            result = tr("%s (%d discarded)") % (result, self.discarded_file_count)
        return result

    @property
    def fileclasses(self):
        return self._get_fileclasses()

    @property
    def SCANNER_CLASS(self):
        if self.app_mode == AppMode.PICTURE:
            return pe.scanner.ScannerPE
        elif self.app_mode == AppMode.MUSIC:
            return me.scanner.ScannerME
        else:
            return se.scanner.ScannerSE

    @property
    def METADATA_TO_READ(self):
        if self.app_mode == AppMode.PICTURE:
            return ["size", "mtime", "dimensions", "exif_timestamp"]
        elif self.app_mode == AppMode.MUSIC:
            return [
                "size",
                "mtime",
                "duration",
                "bitrate",
                "samplerate",
                "title",
                "artist",
                "album",
                "genre",
                "year",
                "track",
                "comment",
            ]
        else:
            return ["size", "mtime"]
