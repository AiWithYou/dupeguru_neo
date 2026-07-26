# Created By: Virgil Dupras
# Created On: 2006/01/29
# Copyright 2015 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import difflib
import itertools
import logging
import string
from collections.abc import Set
from collections import defaultdict, namedtuple
from dataclasses import dataclass
from enum import Enum
from unicodedata import normalize

from hscommon.util import flatten, multi_replace
from hscommon.trans import tr
from hscommon.jobprogress import job

(
    WEIGHT_WORDS,
    MATCH_SIMILAR_WORDS,
    NO_FIELD_ORDER,
) = range(3)

JOB_REFRESH_RATE = 100
PROGRESS_MESSAGE = tr("%d matches found from %d groups")
# Legacy filename/tag matching still represents a non-transitive similarity
# graph with explicit edges.  Keep that graph within the results persistence
# contract instead of silently returning an incomplete scan or constructing a
# report which the loader must reject.
MAX_SIMILAR_SCAN_MATCHES = 1_000_000
MAX_SIMILAR_CANDIDATE_COMPARISONS = 2_000_000
MAX_SIMILAR_MATCHES_PER_GROUP = 1_000_000


class MatchLimitError(RuntimeError):
    """A complete similarity result cannot be represented within safe limits."""


def getwords(s):
    # We decompose the string so that ascii letters with accents can be part of the word.
    s = normalize("NFD", s)
    s = multi_replace(s, "-_&+():;\\[]{}.,<>/?~!@#$*", " ").lower()
    # logging.debug(f"DEBUG chars for: {s}\n"
    #               f"{[c for c in s if ord(c) != 32]}\n"
    #               f"{[ord(c) for c in s if ord(c) != 32]}")
    # HACK We shouldn't ignore non-ascii characters altogether. Any Unicode char
    # above common european characters that cannot be "sanitized" (ie. stripped
    # of their accents, etc.) are preserved as is. The arbitrary limit is
    # obtained from this one: ord("\u037e") GREEK QUESTION MARK
    s = "".join(
        c
        for c in s
        if (ord(c) <= 894 and c in string.ascii_letters + string.digits + string.whitespace) or ord(c) > 894
    )
    return [_f for _f in s.split(" ") if _f]  # remove empty elements


def getfields(s):
    fields = [getwords(field) for field in s.split(" - ")]
    return [_f for _f in fields if _f]


def unpack_fields(fields):
    result = []
    for field in fields:
        if isinstance(field, list):
            result += field
        else:
            result.append(field)
    return result


def compare(first, second, flags=()):
    """Returns the % of words that match between ``first`` and ``second``

    The result is a ``int`` in the range 0..100.
    ``first`` and ``second`` can be either a string or a list (of words).
    """
    if not (first and second):
        return 0
    if any(isinstance(element, list) for element in first):
        return compare_fields(first, second, flags)
    second = second[:]  # We must use a copy of second because we remove items from it
    match_similar = MATCH_SIMILAR_WORDS in flags
    weight_words = WEIGHT_WORDS in flags
    joined = first + second
    total_count = sum(len(word) for word in joined) if weight_words else len(joined)
    match_count = 0
    in_order = True
    for word in first:
        if match_similar and (word not in second):
            similar = difflib.get_close_matches(word, second, 1, 0.8)
            if similar:
                word = similar[0]
        if word in second:
            if second[0] != word:
                in_order = False
            second.remove(word)
            match_count += len(word) if weight_words else 1
    result = round(((match_count * 2) / total_count) * 100)
    if (result == 100) and (not in_order):
        result = 99  # We cannot consider a match exact unless the ordering is the same
    return result


def compare_fields(first, second, flags=()):
    """Returns the score for the lowest matching :ref:`fields`.

    ``first`` and ``second`` must be lists of lists of string. Each sub-list is then compared with
    :func:`compare`.
    """
    if len(first) != len(second):
        return 0
    if NO_FIELD_ORDER in flags:
        results = []
        # We don't want to remove field directly in the list. We must work on a copy.
        second = second[:]
        for field1 in first:
            max_score = 0
            matched_field = None
            for field2 in second:
                r = compare(field1, field2, flags)
                if r > max_score:
                    max_score = r
                    matched_field = field2
            results.append(max_score)
            if matched_field:
                second.remove(matched_field)
    else:
        results = [compare(field1, field2, flags) for field1, field2 in zip(first, second)]
    return min(results) if results else 0


def build_word_dict(objects, j=job.nulljob):
    """Returns a dict of objects mapped by their words.

    objects must have a ``words`` attribute being a list of strings or a list of lists of strings
    (:ref:`fields`).

    The result will be a dict with words as keys, lists of objects as values.
    """
    result = defaultdict(set)
    for object in j.iter_with_progress(objects, "Prepared %d/%d files", JOB_REFRESH_RATE):
        for word in unpack_fields(object.words):
            result[word].add(object)
    return result


def merge_similar_words(word_dict):
    """Take all keys in ``word_dict`` that are similar, and merge them together.

    ``word_dict`` has been built with :func:`build_word_dict`. Similarity is computed with Python's
    ``difflib.get_close_matches()``, which computes the number of edits that are necessary to make
    a word equal to the other.
    """
    keys = list(word_dict.keys())
    keys.sort(key=len)  # we want the shortest word to stay
    while keys:
        key = keys.pop(0)
        similars = difflib.get_close_matches(key, keys, 100, 0.8)
        if not similars:
            continue
        objects = word_dict[key]
        for similar in similars:
            objects |= word_dict[similar]
            del word_dict[similar]
            keys.remove(similar)


def reduce_common_words(word_dict, threshold):
    """Remove all objects from ``word_dict`` values where the object count >= ``threshold``

    ``word_dict`` has been built with :func:`build_word_dict`.

    The exception to this removal are the objects where all the words of the object are common.
    Because if we remove them, we will miss some duplicates!
    """
    uncommon_words = {word for word, objects in word_dict.items() if len(objects) < threshold}
    for word, objects in list(word_dict.items()):
        if len(objects) < threshold:
            continue
        reduced = set()
        for o in objects:
            if not any(w in uncommon_words for w in unpack_fields(o.words)):
                reduced.add(o)
        if reduced:
            word_dict[word] = reduced
        else:
            del word_dict[word]


# Writing docstrings in a namedtuple is tricky. From Python 3.3, it's possible to set __doc__, but
# some research allowed me to find a more elegant solution, which is what is done here. See
# http://stackoverflow.com/questions/1606436/adding-docstrings-to-namedtuples-in-python


class Match(namedtuple("Match", "first second percentage")):
    """Represents a match between two :class:`~core.fs.File`.

    Regarless of the matching method, when two files are determined to match, a Match pair is created,
    which holds, of course, the two matched files, but also their match "level".

    .. attribute:: first

        first file of the pair.

    .. attribute:: second

        second file of the pair.

    .. attribute:: percentage

        their match level according to the scan method which found the match. int from 1 to 100. For
        exact scan methods, such as Contents scans, this will always be 100.
    """

    __slots__ = ()


class VerificationKind(str, Enum):
    """Describes what evidence supports a duplicate relationship."""

    UNVERIFIED = "unverified"
    SIMILAR = "similar"
    VERIFIED_EXACT = "verified_exact"


@dataclass(frozen=True)
class ExactComparisonEvidence:
    """A typed edge proving that two members had identical stable bytes."""

    first: object
    second: object
    result: object


@dataclass(frozen=True)
class ExactVerificationFailure:
    """A read/hash/comparison failure that prevents complete exact coverage."""

    first_path: str
    second_path: str
    error_type: str
    message: str
    phase: str = "byte_compare"


@dataclass(frozen=True)
class ExactEvidence:
    """Evidence shared by a byte-verified exact duplicate group."""

    kind: VerificationKind
    algorithm: str
    digest: bytes
    size: int
    comparisons: tuple[ExactComparisonEvidence, ...] = ()


@dataclass(frozen=True)
class ReportedExactEvidence:
    """Non-authoritative metadata read from a saved result report."""

    algorithm: str
    digest: bytes
    size: int
    saved_at_ns: int = 0
    kind: VerificationKind = VerificationKind.UNVERIFIED
    reported_kind: VerificationKind = VerificationKind.VERIFIED_EXACT


class ExactMatchesView(Set):
    """A lazy, O(k)-storage view of an exact group's pair relationships."""

    def __init__(self, group):
        self.group = group

    def __contains__(self, match):
        try:
            first, second, percentage = match
        except (TypeError, ValueError):
            return False
        return (
            percentage == 100
            and first is not second
            and first in self.group.unordered
            and second in self.group.unordered
        )

    def __iter__(self):
        for first, second in itertools.combinations(self.group.ordered, 2):
            yield Match(first, second, 100)

    def __len__(self):
        count = len(self.group)
        return count * (count - 1) // 2


class ExactGroupList(list):
    """Verified groups plus failures that made the exact scan incomplete."""

    def __init__(self, iterable=(), verification_failures=()):
        super().__init__(iterable)
        self.verification_failures = list(verification_failures)


class FolderGroupList(list):
    """Aggregate-digest folder groups stored without pair materialization."""


def get_match(first, second, flags=()):
    # it is assumed here that first and second both have a "words" attribute
    percentage = compare(first.words, second.words, flags)
    return Match(first, second, percentage)


def getmatches(
    objects,
    min_match_percentage=0,
    match_similar_words=False,
    weight_words=False,
    no_field_order=False,
    j=job.nulljob,
):
    """Returns a list of :class:`Match` within ``objects`` after fuzzily matching their words.

    :param objects: List of :class:`~core.fs.File` to match.
    :param int min_match_percentage: minimum % of words that have to match.
    :param bool match_similar_words: make similar words (see :func:`merge_similar_words`) match.
    :param bool weight_words: longer words are worth more in match % computations.
    :param bool no_field_order: match :ref:`fields` regardless of their order.
    :param j: A :ref:`job progress instance <jobs>`.
    """
    COMMON_WORD_THRESHOLD = 50
    j = j.start_subjob(2)
    sj = j.start_subjob(2)
    for o in objects:
        if not hasattr(o, "words"):
            o.words = getwords(o.name)
    word_dict = build_word_dict(objects, sj)
    reduce_common_words(word_dict, COMMON_WORD_THRESHOLD)
    if match_similar_words:
        merge_similar_words(word_dict)
    largest_bucket_comparisons = max(
        (len(items) * (len(items) - 1) // 2 for items in word_dict.values()),
        default=0,
    )
    if largest_bucket_comparisons > MAX_SIMILAR_CANDIDATE_COMPARISONS:
        raise MatchLimitError(
            "Similarity scan exceeds the {} candidate-comparison safety limit; "
            "narrow the folders or raise the match threshold.".format(MAX_SIMILAR_CANDIDATE_COMPARISONS)
        )
    match_flags = []
    if weight_words:
        match_flags.append(WEIGHT_WORDS)
    if match_similar_words:
        match_flags.append(MATCH_SIMILAR_WORDS)
    if no_field_order:
        match_flags.append(NO_FIELD_ORDER)
    j.start_job(len(word_dict), PROGRESS_MESSAGE % (0, 0))
    compared = defaultdict(set)
    result = []
    candidate_comparisons = 0
    try:
        word_count = 0
        # This whole 'popping' thing is there to avoid taking too much memory at the same time.
        while word_dict:
            items = word_dict.popitem()[1]
            while items:
                ref = items.pop()
                compared_already = compared[ref]
                to_compare = items - compared_already
                compared_already |= to_compare
                candidate_comparisons += len(to_compare)
                if candidate_comparisons > MAX_SIMILAR_CANDIDATE_COMPARISONS:
                    raise MatchLimitError(
                        "Similarity scan exceeds the {} candidate-comparison safety limit; "
                        "narrow the folders or raise the match threshold.".format(MAX_SIMILAR_CANDIDATE_COMPARISONS)
                    )
                for other in to_compare:
                    m = get_match(ref, other, match_flags)
                    if m.percentage >= min_match_percentage:
                        if len(result) >= MAX_SIMILAR_SCAN_MATCHES:
                            raise MatchLimitError(
                                "Similarity scan exceeds the {} saved-match safety limit; "
                                "narrow the folders or raise the match threshold.".format(MAX_SIMILAR_SCAN_MATCHES)
                            )
                        result.append(m)
            word_count += 1
            j.add_progress(desc=PROGRESS_MESSAGE % (len(result), word_count))
    except MemoryError as error:
        # An incomplete similarity graph can produce misleading groups.  Fail
        # the scan explicitly so callers never review or save partial output.
        del compared  # This should give us enough room to call logging.
        logging.warning("Memory Overflow. Matches: %d. Word dict: %d" % (len(result), len(word_dict)))
        raise MatchLimitError(
            "Similarity scan ran out of memory before a complete result could be produced."
        ) from error
    return result


def _bucket_by_digest(files, attrname):
    digest2files = defaultdict(list)
    for file in files:
        digest = getattr(file, attrname, None)
        if digest is not None:
            digest2files[digest].append(file)
    return [items for items in digest2files.values() if len(items) > 1]


def _record_exact_read_failure(result, file, phase, error):
    path = str(getattr(file, "path", ""))
    logging.warning(
        "Couldn't read exact-scan %s for %r: %s",
        phase,
        file,
        error,
    )
    result.verification_failures.append(
        ExactVerificationFailure(
            first_path=path,
            second_path="",
            error_type=type(error).__name__,
            message=str(error) or type(error).__name__,
            phase=phase,
        )
    )


def _read_exact_attribute(file, attrname):
    strict_reader = getattr(file, "read_info_strict", None)
    if strict_reader is not None:
        return strict_reader(attrname)
    return getattr(file, attrname)


def _begin_exact_file(file):
    begin = getattr(file, "begin_exact_scan", None)
    if begin is not None:
        return begin()
    return _read_exact_attribute(file, "size")


def _bucket_by_exact_digest(files, attrname, result):
    digest2files = defaultdict(list)
    for file in files:
        try:
            digest = _read_exact_attribute(file, attrname)
            if digest is None:
                raise ValueError("Exact-scan {} returned no digest".format(attrname))
        except Exception as error:
            _record_exact_read_failure(result, file, attrname, error)
            continue
        digest2files[digest].append(file)
    return [items for items in digest2files.values() if len(items) > 1]


def _compare_exact_files(first, second, stop_check=None):
    if stop_check is not None and stop_check():
        raise InterruptedError("exact scan resource limit reached")
    interruptible_compare = getattr(first, "compare_bytes_interruptible", None)
    if stop_check is not None and interruptible_compare is not None:
        return interruptible_compare(second, stop_check)
    compare = getattr(first, "compare_bytes", None)
    if compare is None:
        raise TypeError(f"{type(first).__name__} does not support verified byte comparison")
    result = compare(second)
    if stop_check is not None and stop_check():
        raise InterruptedError("exact scan resource limit reached")
    return result


def _verified_classes(files, digest, size, verification_failures, stop_check=None):
    classes = []
    for file in files:
        if stop_check is not None and stop_check():
            return []
        placed = False
        for exact_class in classes:
            representative = exact_class["files"][0]
            try:
                comparison = _compare_exact_files(representative, file, stop_check=stop_check)
            except Exception as ex:
                logging.warning("Couldn't byte-verify %r and %r: %s", representative, file, ex)
                if stop_check is not None and stop_check():
                    return []
                verification_failures.append(
                    ExactVerificationFailure(
                        first_path=str(getattr(representative, "path", "")),
                        second_path=str(getattr(file, "path", "")),
                        error_type=type(ex).__name__,
                        message=str(ex) or type(ex).__name__,
                    )
                )
                # One unreadable edge makes this entire digest bucket
                # incomplete.  Stop here instead of accumulating a quadratic
                # number of failures when many members are unreadable.
                return []
            if comparison is not None and comparison is not False:
                first_snapshot = getattr(comparison, "first", None)
                second_snapshot = getattr(comparison, "second", None)
                bytes_compared = getattr(comparison, "bytes_compared", None)
                if first_snapshot is not None or second_snapshot is not None:
                    if (
                        first_snapshot is None
                        or second_snapshot is None
                        or first_snapshot.size != size
                        or second_snapshot.size != size
                        or bytes_compared != size
                    ):
                        verification_failures.append(
                            ExactVerificationFailure(
                                first_path=str(getattr(representative, "path", "")),
                                second_path=str(getattr(file, "path", "")),
                                error_type="EvidenceSizeMismatch",
                                message=(
                                    "stable byte-comparison evidence does not match "
                                    "the candidate size; the bucket was withheld"
                                ),
                                phase="byte_compare",
                            )
                        )
                        return []
                exact_class["files"].append(file)
                exact_class["comparisons"].append(
                    ExactComparisonEvidence(
                        first=representative,
                        second=file,
                        result=comparison,
                    )
                )
                placed = True
                break
            verification_failures.append(
                ExactVerificationFailure(
                    first_path=str(getattr(representative, "path", "")),
                    second_path=str(getattr(file, "path", "")),
                    error_type="FullDigestCollision",
                    message=("members of one full-digest bucket were not byte-identical; " "the bucket was withheld"),
                )
            )
            # A digest collision can otherwise create quadratically many
            # equivalence-class comparisons.  Withhold the entire bucket and
            # report incomplete coverage instead of weakening the proof or
            # risking unbounded work.
            return []
        if not placed:
            classes.append({"files": [file], "comparisons": []})
    algorithm = getattr(files[0], "digest_algorithm", "test-double")
    result = []
    for exact_class in classes:
        if len(exact_class["files"]) < 2:
            continue
        evidence = ExactEvidence(
            kind=VerificationKind.VERIFIED_EXACT,
            algorithm=algorithm,
            digest=digest,
            size=size,
            comparisons=tuple(exact_class["comparisons"]),
        )
        result.append(Group.from_exact_files(exact_class["files"], evidence))
    return result


def build_verified_exact_group(files, digest, size=None, algorithm=None):
    """Revalidate and directly build one exact group from a known candidate set."""

    files = list(files)
    if len(files) < 2:
        raise ValueError("An exact group requires at least two files")
    if size is None:
        size = files[0].size
    generation_validators = []
    for file in files:
        validate = getattr(file, "validate_exact_scan", None)
        baseline = getattr(file, "_exact_scan_snapshot", None)
        if validate is not None and baseline is not None:
            validate()
            generation_validators.append(validate)
    representative = files[0]
    comparisons = []
    for file in files[1:]:
        comparison = _compare_exact_files(representative, file)
        if comparison is None or comparison is False:
            raise ValueError("Candidate files are no longer byte-identical")
        first_snapshot = getattr(comparison, "first", None)
        second_snapshot = getattr(comparison, "second", None)
        bytes_compared = getattr(comparison, "bytes_compared", None)
        if first_snapshot is not None or second_snapshot is not None:
            if (
                first_snapshot is None
                or second_snapshot is None
                or first_snapshot.size != size
                or second_snapshot.size != size
                or bytes_compared != size
            ):
                raise ValueError("Stable byte-comparison evidence does not match the exact group size")
        comparisons.append(
            ExactComparisonEvidence(
                first=representative,
                second=file,
                result=comparison,
            )
        )
    for validate in generation_validators:
        validate()
    evidence = ExactEvidence(
        kind=VerificationKind.VERIFIED_EXACT,
        algorithm=algorithm or getattr(representative, "digest_algorithm", "test-double"),
        digest=digest,
        size=size,
        comparisons=tuple(comparisons),
    )
    return Group.from_exact_files(files, evidence)


def getgroups_by_contents(files, bigsize=0, j=job.nulljob, stop_check=None):
    """Return byte-verified exact groups without materializing every file pair.

    Partial and sampled hashes are candidate filters only. Every candidate that
    reaches an exact group has a full digest and a final streaming byte
    comparison against a group representative.
    """
    files = list(files)
    result = ExactGroupList()
    size2files = defaultdict(list)
    begun_files = []
    for file in files:
        if stop_check is not None and stop_check():
            return result
        try:
            size = _begin_exact_file(file)
        except Exception as error:
            _record_exact_read_failure(result, file, "size", error)
            continue
        begun_files.append(file)
        size2files[size].append(file)
    possible_groups = [(size, items) for size, items in size2files.items() if len(items) > 1]
    logical_match_count = 0
    j.start_job(len(possible_groups), PROGRESS_MESSAGE % (0, 0))
    for group_count, (size, size_group) in enumerate(possible_groups, 1):
        if stop_check is not None and stop_check():
            return result
        candidate_groups = _bucket_by_exact_digest(
            size_group,
            "digest_partial",
            result,
        )
        if bigsize > 0 and size > bigsize:
            candidate_groups = [
                sample_group
                for partial_group in candidate_groups
                for sample_group in _bucket_by_exact_digest(
                    partial_group,
                    "digest_samples",
                    result,
                )
            ]
        for candidate_group in candidate_groups:
            if stop_check is not None and stop_check():
                return result
            full_digest_groups = defaultdict(list)
            for file in candidate_group:
                if stop_check is not None and stop_check():
                    return result
                try:
                    digest = _read_exact_attribute(file, "digest")
                    if digest is None:
                        raise ValueError("Exact-scan digest returned no digest")
                    algorithm = getattr(file, "digest_algorithm", "test-double")
                except Exception as error:
                    _record_exact_read_failure(
                        result,
                        file,
                        "digest",
                        error,
                    )
                    continue
                full_digest_groups[(algorithm, digest)].append(file)
            for (_, digest), full_digest_group in full_digest_groups.items():
                if stop_check is not None and stop_check():
                    return result
                if len(full_digest_group) > 1:
                    verified_groups = _verified_classes(
                        full_digest_group,
                        digest,
                        size,
                        result.verification_failures,
                        stop_check=stop_check,
                    )
                    if stop_check is not None and stop_check():
                        return result
                    result.extend(verified_groups)
                    logical_match_count += sum(len(group) * (len(group) - 1) // 2 for group in verified_groups)
        j.add_progress(desc=PROGRESS_MESSAGE % (logical_match_count, group_count))
    failed_paths = {
        path for failure in result.verification_failures for path in (failure.first_path, failure.second_path) if path
    }
    changed_files = set()
    for file in begun_files:
        if stop_check is not None and stop_check():
            return result
        validate = getattr(file, "validate_exact_scan", None)
        if validate is None:
            continue
        try:
            validate()
        except Exception as error:
            path = str(getattr(file, "path", ""))
            changed_files.add(file)
            if path not in failed_paths:
                _record_exact_read_failure(
                    result,
                    file,
                    "generation_validation",
                    error,
                )
                failed_paths.add(path)
    if changed_files:
        result[:] = [group for group in result if not any(file in changed_files for file in group)]
    return result


def getmatches_by_contents(files, bigsize=0, j=job.nulljob):
    """Compatibility wrapper returning exact match pairs.

    The scanner consumes :func:`getgroups_by_contents` directly to retain O(k)
    storage. This wrapper lazily generates each exact group's pair relations and
    materializes them only for callers of the legacy API.
    """
    groups = getgroups_by_contents(files, bigsize=bigsize, j=j)
    return [match for group in groups for match in group.matches]


def getgroups_by_folders(folders, j=job.nulljob):
    """Return aggregate-digest folder groups with O(k) storage.

    Folder digests describe recursive manifests rather than a byte stream, so
    they deliberately remain unverified matches and never use partial hashes.
    """
    size2folders = defaultdict(list)
    for folder in folders:
        size2folders[folder.size].append(folder)
    possible_groups = [items for items in size2folders.values() if len(items) > 1]
    result = FolderGroupList()
    logical_match_count = 0
    j.start_job(len(possible_groups), PROGRESS_MESSAGE % (0, 0))
    for group_count, size_group in enumerate(possible_groups, 1):
        digest_groups = _bucket_by_digest(size_group, "digest")
        for digest_group in digest_groups:
            if not any(not folder.is_ref for folder in digest_group):
                continue
            result.append(Group.from_unverified_transitive_files(digest_group))
            count = len(digest_group)
            logical_match_count += count * (count - 1) // 2
        j.add_progress(desc=PROGRESS_MESSAGE % (logical_match_count, group_count))
    return result


def getmatches_by_folders(folders, j=job.nulljob):
    """Compatibility wrapper which materializes legacy folder match pairs."""

    groups = getgroups_by_folders(folders, j=j)
    return [match for group in groups for match in group.matches]


class Group:
    """A group of :class:`~core.fs.File` that match together.

    This manages match pairs into groups and ensures that all files in the group match to each
    other.

    .. attribute:: ref

        The "reference" file, which is the file among the group that isn't going to be deleted.

    .. attribute:: ordered

        Ordered list of duplicates in the group (including the :attr:`ref`).

    .. attribute:: unordered

        Set duplicates in the group (including the :attr:`ref`).

    .. attribute:: dupes

        An ordered list of the group's duplicate, without :attr:`ref`. Equivalent to
        ``ordered[1:]``

    .. attribute:: percentage

        Average match percentage of match pairs containing :attr:`ref`.
    """

    # ---Override
    def __init__(self):
        self._clear()

    def __contains__(self, item):
        return item in self.unordered

    def __getitem__(self, key):
        return self.ordered.__getitem__(key)

    def __iter__(self):
        return iter(self.ordered)

    def __len__(self):
        return len(self.ordered)

    # ---Private
    def _clear(self):
        self._percentage = None
        self._matches_for_ref = None
        self.matches = set()
        self.candidates = defaultdict(set)
        self.ordered = []
        self.unordered = set()
        self.verification_kind = VerificationKind.UNVERIFIED
        self.evidence = None
        self._is_exact = False
        self.compact_relation = None

    def _get_matches_for_ref(self):
        if self._is_exact:
            ref = self.ref
            return [Match(ref, item, 100) for item in self.dupes]
        if self._matches_for_ref is None:
            ref = self.ref
            self._matches_for_ref = [match for match in self.matches if ref in match]
        return self._matches_for_ref

    # ---Public
    @classmethod
    def from_exact_files(cls, files, evidence):
        """Build a byte-verified exact group without pairwise Match storage."""
        files = list(files)
        if len(files) < 2:
            raise ValueError("An exact group requires at least two files")
        if evidence.kind is not VerificationKind.VERIFIED_EXACT:
            raise ValueError("Exact groups require verified exact evidence")
        if any(file.size != evidence.size for file in files):
            raise ValueError("Exact group members must match the evidence size")
        group = cls()
        group.ordered = files
        group.unordered = set(files)
        if len(group.unordered) != len(files):
            raise ValueError("An exact group cannot contain the same file twice")
        group.verification_kind = evidence.kind
        group.evidence = evidence
        group._is_exact = True
        group.compact_relation = VerificationKind.VERIFIED_EXACT.value
        group.matches = ExactMatchesView(group)
        return group

    @classmethod
    def from_unverified_exact_report(cls, files, evidence=None):
        """Build an O(k) historical exact-result group.

        Result files are reports, not live proofs: a file may have been replaced
        after the report was written.  This constructor therefore preserves the
        compact group shape and display percentage while deliberately leaving
        ``verification_kind`` unverified.  Destructive callers must perform a
        fresh byte comparison and create a new :class:`ExactEvidence`.
        """

        return cls.from_unverified_transitive_files(
            files,
            evidence=evidence,
            relation="reported_exact",
        )

    @classmethod
    def from_unverified_transitive_files(
        cls,
        files,
        evidence=None,
        relation="folder_manifest",
    ):
        """Build an O(k) group for a transitive, non-destructive relation.

        This is used for saved exact reports and recursive folder-manifest
        equality.  The lazy 100% pair view preserves the historical Group API,
        while ``UNVERIFIED`` prevents the relation from granting a file-action
        capability.
        """

        files = list(files)
        if len(files) < 2:
            raise ValueError("A transitive group requires at least two files")
        group = cls()
        group.ordered = files
        group.unordered = set(files)
        if len(group.unordered) != len(files):
            raise ValueError("A transitive group cannot contain the same file twice")
        group.verification_kind = VerificationKind.UNVERIFIED
        group.evidence = evidence
        group._is_exact = True
        group.compact_relation = relation
        group.matches = ExactMatchesView(group)
        return group

    @classmethod
    def from_saved_matches(cls, files, matches):
        """Restore a validated non-exact clique without incremental cubic work.

        A serialized non-exact group is the complete match graph for its
        members.  Incrementally replaying every edge through :meth:`add_match`
        repeatedly scans the growing member set and becomes O(k³) for a dense
        group.  Validate the graph once, then install the same final state in
        O(k + m).
        """

        files = list(files)
        matches = list(matches)
        if len(files) < 2:
            raise ValueError("A saved similarity group requires at least two files")
        unordered = set(files)
        if len(unordered) != len(files):
            raise ValueError("A saved similarity group cannot contain the same file twice")
        identities = {id(file): file for file in files}
        pair_keys = set()
        normalized_matches = set()
        for match in matches:
            try:
                first, second, percentage = match
            except (TypeError, ValueError) as error:
                raise ValueError("A saved similarity match is invalid") from error
            first_identity = id(first)
            second_identity = id(second)
            if (
                first_identity not in identities
                or identities[first_identity] is not first
                or second_identity not in identities
                or identities[second_identity] is not second
                or first is second
                or type(percentage) is not int
                or not 0 <= percentage <= 100
            ):
                raise ValueError("A saved similarity match is outside its group")
            pair_key = (
                (first_identity, second_identity)
                if first_identity < second_identity
                else (second_identity, first_identity)
            )
            if pair_key in pair_keys:
                raise ValueError("A saved similarity group contains a duplicate match pair")
            pair_keys.add(pair_key)
            normalized_matches.add(Match(first, second, percentage))
        expected_pairs = len(files) * (len(files) - 1) // 2
        if len(pair_keys) != expected_pairs:
            raise ValueError("A saved similarity group does not contain a complete match graph")

        group = cls()
        group.ordered = files
        group.unordered = unordered
        group.matches = normalized_matches
        group.verification_kind = VerificationKind.SIMILAR
        return group

    def add_match(self, match):
        """Adds ``match`` to internal match list and possibly add duplicates to the group.

        A duplicate can only be considered as such if it matches all other duplicates in the group.
        This method registers that pair (A, B) represented in ``match`` as possible candidates and,
        if A and/or B end up matching every other duplicates in the group, add these duplicates to
        the group.

        :param tuple match: pair of :class:`~core.fs.File` to add
        """
        if self._is_exact:
            raise TypeError("Pair matches cannot be added to an exact group")

        def add_candidate(item, match):
            matches = self.candidates[item]
            matches.add(match)
            if self.unordered <= matches:
                self.ordered.append(item)
                self.unordered.add(item)

        if match in self.matches:
            return
        self.matches.add(match)
        self.verification_kind = VerificationKind.SIMILAR
        first, second, _ = match
        if first not in self.unordered:
            add_candidate(first, second)
        if second not in self.unordered:
            add_candidate(second, first)
        self._percentage = None
        self._matches_for_ref = None

    def discard_matches(self):
        """Remove all recorded matches that didn't result in a duplicate being added to the group.

        You can call this after the duplicate scanning process to free a bit of memory.
        """
        if self._is_exact:
            return set()
        discarded = {m for m in self.matches if not all(obj in self.unordered for obj in [m.first, m.second])}
        self.matches -= discarded
        self.candidates = defaultdict(set)
        return discarded

    def get_match_of(self, item):
        """Returns the match pair between ``item`` and :attr:`ref`."""
        if item is self.ref:
            return
        if self._is_exact:
            try:
                if item in self.unordered:
                    return Match(self.ref, item, 100)
            except TypeError:
                # Exact groups contain hashable file objects.  Preserve the
                # historical ``None`` result for unrelated unhashable values.
                pass
            return
        for m in self._get_matches_for_ref():
            if item in m:
                return m

    def prioritize(self, key_func, tie_breaker=None):
        """Reorders :attr:`ordered` according to ``key_func``.

        :param key_func: Key (f(x)) to be used for sorting
        :param tie_breaker: function to be used to select the reference position in case the top
                            duplicates have the same key_func() result.
        """
        # tie_breaker(ref, dupe) --> True if dupe should be ref
        # Returns True if anything changed during prioritization.
        new_order = sorted(self.ordered, key=lambda x: (-x.is_ref, key_func(x)))
        changed = new_order != self.ordered
        self.ordered = new_order
        if tie_breaker is None:
            return changed
        ref = self.ref
        key_value = key_func(ref)
        for dupe in self.dupes:
            if key_func(dupe) != key_value:
                break
            if tie_breaker(ref, dupe):
                ref = dupe
        if ref is not self.ref:
            self.switch_ref(ref)
            return True
        return changed

    def remove_dupes(self, items, discard_matches=True):
        """Remove several members with one ordered-list rebuild.

        The single-item API delegates here so callers keep the same behavior,
        while result actions can remove a large duplicate set in O(k) work.
        """
        removals = self.unordered.intersection(items)
        if not removals:
            return set()
        self.ordered[:] = [member for member in self.ordered if member not in removals]
        self.unordered.difference_update(removals)
        self._percentage = None
        self._matches_for_ref = None
        if (len(self) > 1) and any(not getattr(member, "is_ref", False) for member in self):
            if discard_matches and not self._is_exact:
                self.matches = {
                    match for match in self.matches if match.first not in removals and match.second not in removals
                }
        else:
            self._clear()
        return removals

    def remove_dupe(self, item, discard_matches=True):
        self.remove_dupes((item,), discard_matches)

    def switch_ref(self, with_dupe):
        """Make the :attr:`ref` dupe of the group switch position with ``with_dupe``."""
        if self.ref.is_ref:
            return False
        try:
            self.ordered.remove(with_dupe)
            self.ordered.insert(0, with_dupe)
            self._percentage = None
            self._matches_for_ref = None
            return True
        except ValueError:
            return False

    dupes = property(lambda self: self[1:])

    @property
    def percentage(self):
        if self._is_exact:
            return 100 if self.dupes else 0
        if self._percentage is None:
            if self.dupes:
                matches = self._get_matches_for_ref()
                self._percentage = sum(match.percentage for match in matches) // len(matches)
            else:
                self._percentage = 0
        return self._percentage

    @property
    def ref(self):
        if self:
            return self[0]


def get_groups(matches):
    """Returns a list of :class:`Group` from ``matches``.

    Create groups out of match pairs in the smartest way possible.
    """
    if len(matches) > MAX_SIMILAR_SCAN_MATCHES:
        raise MatchLimitError(
            "Similarity graph exceeds the {} saved-match safety limit.".format(MAX_SIMILAR_SCAN_MATCHES)
        )
    matches.sort(key=lambda match: -match.percentage)
    dupe2group = {}
    groups = []
    try:
        for match in matches:
            first, second, _ = match
            first_group = dupe2group.get(first)
            second_group = dupe2group.get(second)
            if first_group:
                if second_group:
                    if first_group is second_group:
                        target_group = first_group
                    else:
                        continue
                else:
                    target_group = first_group
                    dupe2group[second] = target_group
            else:
                if second_group:
                    target_group = second_group
                    dupe2group[first] = target_group
                else:
                    target_group = Group()
                    groups.append(target_group)
                    dupe2group[first] = target_group
                    dupe2group[second] = target_group
            if match not in target_group.matches and len(target_group.matches) >= MAX_SIMILAR_MATCHES_PER_GROUP:
                raise MatchLimitError(
                    "A similarity group exceeds the {} saved-match safety limit.".format(MAX_SIMILAR_MATCHES_PER_GROUP)
                )
            target_group.add_match(match)
    except MemoryError as error:
        del dupe2group
        del matches
        logging.warning(f"Memory Overflow. Groups: {len(groups)}")
        raise MatchLimitError(
            "Similarity grouping ran out of memory before a complete result could be produced."
        ) from error
    # Now that we have a group, we have to discard groups' matches and see if there're any "orphan"
    # matches, that is, matches that were candidate in a group but that none of their 2 files were
    # accepted in the group. With these orphan groups, it's safe to build additional groups
    matched_files = set(flatten(groups))
    orphan_matches = []
    for group in groups:
        orphan_matches += {
            m for m in group.discard_matches() if not any(obj in matched_files for obj in [m.first, m.second])
        }
    if groups and orphan_matches:
        groups += get_groups(orphan_matches)  # no job, as it isn't supposed to take a long time
    return groups
