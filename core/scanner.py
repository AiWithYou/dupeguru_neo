# Copyright 2016 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import logging
import re
import os.path as op
from collections import namedtuple

from hscommon.jobprogress import job
from hscommon.util import dedupe, rem_file_ext, get_file_ext
from hscommon.trans import tr

from core import engine
from core.keeper import choose_keeper
from core.scan_receipt import ScanIssue, ScanReceipt

# It's quite ugly to have scan types from all editions all put in the same class, but because there's
# there will be some nasty bugs popping up (ScanType is used in core when in should exclusively be
# used in core_*). One day I'll clean this up.


class ScanType:
    FILENAME = 0
    FIELDS = 1
    FIELDSNOORDER = 2
    TAG = 3
    FOLDERS = 4
    CONTENTS = 5

    # PE
    FUZZYBLOCK = 10
    EXIFTIMESTAMP = 11


ScanOption = namedtuple("ScanOption", "scan_type label")

SCANNABLE_TAGS = ["track", "artist", "album", "title", "genre", "year"]

RE_DIGIT_ENDING = re.compile(r"\d+|\(\d+\)|\[\d+\]|{\d+}")


def is_same_with_digit(name, refname):
    # Returns True if name is the same as refname, but with digits (with brackets or not) at the end
    if not name.startswith(refname):
        return False
    end = name[len(refname) :].strip()
    return RE_DIGIT_ENDING.match(end) is not None


def remove_dupe_paths(files):
    # Returns files with duplicates-by-path removed. Files with the exact same path are considered
    # duplicates and only the first file to have a path is kept. In certain cases, we have files
    # that have the same path, but not with the same case, that's why we normalize. However, we also
    # have case-sensitive filesystems, and in those, we don't want to falsely remove duplicates,
    # that's why we have a `samefile` mechanism.
    result = []
    path2files = {}
    for f in files:
        normalized = op.normcase(op.normpath(str(f.path)))
        existing_files = path2files.setdefault(normalized, [])
        same_physical_path = False
        for existing in existing_files:
            if op.normpath(str(f.path)) == op.normpath(str(existing.path)):
                same_physical_path = True
                break
            try:
                if op.samefile(str(f.path), str(existing.path)):
                    same_physical_path = True
                    break
            except OSError:
                # Failure to prove identity is not proof that the entries are
                # the same; retain both and let later verification decide.
                continue
        if same_physical_path:
            continue
        existing_files.append(f)
        result.append(f)
    return result


class Scanner:
    def __init__(self):
        self.discarded_file_count = 0
        self.scan_receipt = None

    def _pair_is_in_scope(self, first, second):
        if self.comparison_scope == "all":
            return True
        if self.comparison_scope != "cross_pool":
            raise ValueError("Invalid comparison scope: {}".format(self.comparison_scope))
        first_pool = getattr(first, "comparison_pool", "incoming")
        second_pool = getattr(second, "comparison_pool", "incoming")
        return first_pool != second_pool

    def _getmatches(self, files, j):
        if (
            self.size_threshold
            or self.large_size_threshold
            or self.scan_type
            in {
                ScanType.CONTENTS,
                ScanType.FOLDERS,
            }
        ):
            j = j.start_subjob([2, 8])
            if self.size_threshold:
                files = [f for f in files if f.size >= self.size_threshold]
            if self.large_size_threshold:
                files = [f for f in files if f.size <= self.large_size_threshold]
        if self.scan_type == ScanType.CONTENTS:
            return engine.getgroups_by_contents(files, bigsize=self.big_file_size_threshold, j=j)
        elif self.scan_type == ScanType.FOLDERS:
            return engine.getgroups_by_folders(files, j=j)
        else:
            j = j.start_subjob([2, 8])
            kw = {}
            kw["match_similar_words"] = self.match_similar_words
            kw["weight_words"] = self.word_weighting
            kw["min_match_percentage"] = self.min_match_percentage
            if self.scan_type == ScanType.FIELDSNOORDER:
                self.scan_type = ScanType.FIELDS
                kw["no_field_order"] = True
            func = {
                ScanType.FILENAME: lambda f: engine.getwords(rem_file_ext(f.name)),
                ScanType.FIELDS: lambda f: engine.getfields(rem_file_ext(f.name)),
                ScanType.TAG: lambda f: [
                    engine.getwords(str(getattr(f, attrname)))
                    for attrname in SCANNABLE_TAGS
                    if attrname in self.scanned_tags
                ],
            }[self.scan_type]
            for f in j.iter_with_progress(files, tr("Read metadata of %d/%d files")):
                logging.debug("Reading metadata of %s", f.path)
                f.words = func(f)
            return engine.getmatches(files, j=j, **kw)

    @staticmethod
    def _key_func(dupe):
        return -dupe.size

    @staticmethod
    def _tie_breaker(ref, dupe):
        refname = rem_file_ext(ref.name).lower()
        dupename = rem_file_ext(dupe.name).lower()
        if "copy" in dupename:
            return False
        if "copy" in refname:
            return True
        if is_same_with_digit(dupename, refname):
            return False
        if is_same_with_digit(refname, dupename):
            return True
        return len(dupe.path.parts) > len(ref.path.parts)

    @staticmethod
    def _prioritize_group(group):
        decision = choose_keeper(group)
        group.keeper_decision = decision
        group.prioritize(decision.sort_key)

    def _partition_exact_candidates(self, candidates, ignore_list):
        """Greedily color ignore edges without scanning every prior member."""

        partitions = []
        partitions_by_kind = {}
        assigned = {}
        for file in candidates:
            extension = get_file_ext(file.name)
            kind = None if self.mix_file_kind else extension
            path = str(file.path)
            forbidden = set()
            if ignore_list:
                for neighbor in ignore_list.ignored_neighbors(path):
                    assignment = assigned.get(neighbor)
                    if assignment is not None and assignment[0] == kind:
                        forbidden.add(assignment[1])
            color = 0
            while color in forbidden:
                color += 1
            kind_partitions = partitions_by_kind.setdefault(kind, [])
            if color == len(kind_partitions):
                partition = {"extension": extension, "files": []}
                kind_partitions.append(partition)
                partitions.append(partition)
            else:
                partition = kind_partitions[color]
            partition["files"].append(file)
            assigned[path] = (kind, color)
        return partitions

    def _postprocess_exact_groups(
        self,
        scan_result,
        ignore_list,
        j,
        *,
        revalidate,
        force_exists=False,
        fail_on_group_error=False,
    ):
        """Apply the shared exact-result policies without pair expansion."""

        j.set_progress(100, tr("Almost done! Fiddling with results..."))
        exact_groups = []
        for exact_group in scan_result:
            candidates = list(exact_group)
            if self.include_exists_check or force_exists:
                existing = [file for file in candidates if file.exists()]
                if force_exists and len(existing) != len(candidates):
                    raise OSError("A catalog-projected exact file disappeared before grouping")
                candidates = existing
            if self.size_threshold:
                candidates = [file for file in candidates if file.size >= self.size_threshold]
            if self.large_size_threshold:
                candidates = [file for file in candidates if file.size <= self.large_size_threshold]
            partitions = self._partition_exact_candidates(candidates, ignore_list)
            for partition in partitions:
                if len(partition["files"]) < 2:
                    continue
                if (
                    self.comparison_scope == "cross_pool"
                    and len({getattr(file, "comparison_pool", "incoming") for file in partition["files"]}) < 2
                ):
                    continue
                try:
                    if revalidate:
                        group = engine.build_verified_exact_group(
                            partition["files"],
                            exact_group.evidence.digest,
                            size=exact_group.evidence.size,
                            algorithm=exact_group.evidence.algorithm,
                        )
                    else:
                        evidence = engine.ExactEvidence(
                            kind=engine.VerificationKind.VERIFIED_EXACT,
                            algorithm=exact_group.evidence.algorithm,
                            digest=exact_group.evidence.digest,
                            size=exact_group.evidence.size,
                        )
                        group = engine.Group.from_exact_files(
                            partition["files"],
                            evidence,
                        )
                    exact_groups.append(group)
                except (OSError, TypeError, ValueError) as error:
                    if fail_on_group_error:
                        raise
                    logging.warning("Exact partition changed before grouping: %s", error)
        logging.info("Found %d byte-verified exact groups", len(exact_groups))
        self.discarded_file_count = 0
        groups = [group for group in exact_groups if any(not file.is_ref for file in group)]
        logging.info("Created %d groups", len(groups))
        for group in groups:
            self._prioritize_group(group)
        return groups

    def get_dupe_groups_from_verified_exact(
        self,
        exact_groups,
        ignore_list=None,
        j=job.nulljob,
    ):
        """Postprocess catalog byte-verified groups without rereading all pairs."""

        if self.comparison_scope not in {"all", "cross_pool"}:
            raise ValueError("Invalid comparison scope: {}".format(self.comparison_scope))
        return self._postprocess_exact_groups(
            exact_groups,
            ignore_list,
            j,
            revalidate=False,
            force_exists=True,
            fail_on_group_error=True,
        )

    def _postprocess_folder_groups(self, scan_result, ignore_list, j):
        """Apply folder policies without expanding transitive groups into pairs."""

        j.set_progress(100, tr("Almost done! Fiddling with results..."))
        candidates_by_group = []
        matched_paths = set()
        for source_group in scan_result:
            candidates = list(source_group)
            if self.include_exists_check:
                candidates = [folder for folder in candidates if folder.exists()]
            if len(candidates) < 2:
                continue
            candidates_by_group.append(candidates)
            matched_paths.update(folder.path for folder in candidates)

        nested_paths = {path for path in matched_paths if any(parent in matched_paths for parent in path.parents)}
        groups = []
        for candidates in candidates_by_group:
            for partition in self._partition_exact_candidates(candidates, ignore_list):
                files = partition["files"]
                if len(files) < 2:
                    continue
                # Suppress a duplicate sub-tree when every member is already
                # represented by a matching ancestor.  If an unrelated third
                # folder has the same manifest, retain the complete transitive
                # group instead of recreating a partial pair graph.
                if all(folder.path in nested_paths for folder in files):
                    continue
                if (
                    self.comparison_scope == "cross_pool"
                    and len({getattr(folder, "comparison_pool", "incoming") for folder in files}) < 2
                ):
                    continue
                groups.append(engine.Group.from_unverified_transitive_files(files))

        self.discarded_file_count = 0
        groups = [group for group in groups if any(not folder.is_ref for folder in group)]
        logging.info("Created %d folder groups", len(groups))
        for group in groups:
            self._prioritize_group(group)
        return groups

    @staticmethod
    def get_scan_options():
        """Returns a list of scanning options for this scanner.

        Returns a list of ``ScanOption``.
        """
        raise NotImplementedError()

    def get_dupe_groups(self, files, ignore_list=None, j=job.nulljob):
        if self.comparison_scope not in {"all", "cross_pool"}:
            raise ValueError("Invalid comparison scope: {}".format(self.comparison_scope))
        for f in (f for f in files if not hasattr(f, "is_ref")):
            f.is_ref = False
        files = remove_dupe_paths(files)
        logging.info("Getting matches. Scan type: %d", self.scan_type)
        scan_result = self._getmatches(files, j)
        if isinstance(scan_result, engine.ExactGroupList):
            failures = tuple(scan_result.verification_failures)
            if failures:
                failed_paths = {
                    path for failure in failures for path in (failure.first_path, failure.second_path) if path
                }
                failed_count = min(len(files), max(1, len(failed_paths)))
                self.scan_receipt = ScanReceipt.incomplete(
                    discovered=len(files),
                    analyzed=max(0, len(files) - failed_count),
                    failed=failed_count,
                    issues=tuple(
                        ScanIssue(
                            code=(
                                "byte_verification_failed" if failure.phase == "byte_compare" else "exact_hash_failed"
                            ),
                            path=failure.second_path or failure.first_path,
                            message=(
                                ("Final byte comparison failed between {!r} and {!r}: {}: {}").format(
                                    failure.first_path,
                                    failure.second_path,
                                    failure.error_type,
                                    failure.message,
                                )
                                if failure.phase == "byte_compare"
                                else ("Exact-scan {} failed for {!r}: {}: {}").format(
                                    failure.phase,
                                    failure.first_path,
                                    failure.error_type,
                                    failure.message,
                                )
                            ),
                        )
                        for failure in failures
                    ),
                )
            return self._postprocess_exact_groups(
                scan_result,
                ignore_list,
                j,
                revalidate=True,
            )
        if isinstance(scan_result, engine.FolderGroupList):
            return self._postprocess_folder_groups(
                scan_result,
                ignore_list,
                j,
            )
        matches = scan_result
        logging.info("Found %d matches" % len(matches))
        matches = [match for match in matches if self._pair_is_in_scope(match.first, match.second)]
        j.set_progress(100, tr("Almost done! Fiddling with results..."))
        # In removing what we call here "false matches", we first want to remove, if we scan by
        # folders, we want to remove folder matches for which the parent is also in a match (they're
        # "duplicated duplicates if you will). Then, we also don't want mixed file kinds if the
        # option isn't enabled, we want matches for which both files exist and, lastly, we don't
        # want matches with both files as ref.
        if self.scan_type == ScanType.FOLDERS and matches:
            allpath = {m.first.path for m in matches}
            allpath |= {m.second.path for m in matches}
            sortedpaths = sorted(allpath)
            toremove = set()
            last_parent_path = sortedpaths[0]
            for p in sortedpaths[1:]:
                if last_parent_path in p.parents:
                    toremove.add(p)
                else:
                    last_parent_path = p
            matches = [m for m in matches if m.first.path not in toremove or m.second.path not in toremove]
        if not self.mix_file_kind:
            matches = [m for m in matches if get_file_ext(m.first.name) == get_file_ext(m.second.name)]
        if self.include_exists_check:
            matches = [m for m in matches if m.first.exists() and m.second.exists()]
        # Contents already handles ref checks, other scan types might not catch during scan
        if self.scan_type != ScanType.CONTENTS:
            matches = [m for m in matches if not (m.first.is_ref and m.second.is_ref)]
        if ignore_list:
            matches = [m for m in matches if not ignore_list.are_ignored(str(m.first.path), str(m.second.path))]
        logging.info("Grouping matches")
        groups = engine.get_groups(matches)
        if self.scan_type in {
            ScanType.FILENAME,
            ScanType.FIELDS,
            ScanType.FIELDSNOORDER,
            ScanType.TAG,
        }:
            matched_files = dedupe([m.first for m in matches] + [m.second for m in matches])
            self.discarded_file_count = len(matched_files) - sum(len(g) for g in groups)
        else:
            # Ticket #195
            # To speed up the scan, we don't bother comparing contents of files that are both ref
            # files. However, this messes up "discarded" counting because there's a missing match
            # in cases where we end up with a dupe group anyway (with a non-ref file). Because it's
            # impossible to have discarded matches in exact dupe scans, we simply set it at 0, thus
            # bypassing our tricky problem.
            # Also, although ScanType.FuzzyBlock is not always doing exact comparisons, we also
            # bypass ref comparison, thus messing up with our "discarded" count. So we're
            # effectively disabling the "discarded" feature in PE, but it's better than falsely
            # reporting discarded matches.
            self.discarded_file_count = 0
        groups = [g for g in groups if any(not f.is_ref for f in g)]
        logging.info("Created %d groups" % len(groups))
        for g in groups:
            self._prioritize_group(g)
        return groups

    match_similar_words = False
    min_match_percentage = 80
    mix_file_kind = True
    scan_type = ScanType.FILENAME
    scanned_tags = {"artist", "title"}
    size_threshold = 0
    large_size_threshold = 0
    big_file_size_threshold = 0
    word_weighting = False
    include_exists_check = True
    comparison_scope = "all"
