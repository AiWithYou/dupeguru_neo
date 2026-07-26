# Created By: Virgil Dupras
# Created On: 2007/02/25
# Copyright 2015 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import heapq
import logging
import math
import multiprocessing
import sqlite3
import time
import uuid
from dataclasses import dataclass

from hscommon.trans import tr
from hscommon.jobprogress import job

from core.engine import Match
from core.scan_receipt import ScanIssue, ScanReceipt, ScanStatus
from core.pe.block import avgdiff, DifferentBlockCountError, NoBlocksError
from core.pe.candidate_index import MultiIndexHamming, hamming_distance
from core.pe.cache_sqlite import SqliteCache, capture_source_binding
from core.pe.image_features import (
    DecoderUnavailableError,
    FEATURE_VERSION,
    ImageDecodeError,
    ImageResourceLimitError,
    decode_image_features,
)

# pHash is only a candidate-generation accelerator.  Every emitted Match still passes through the
# established 15x15 block comparison below.  The multi-index query is exact within its configured
# Hamming radius, and candidate batches stay bounded while worker processes read cached blocks.

MIN_ITERATIONS = 3
BLOCK_COUNT_PER_SIDE = 15
CANDIDATE_BATCH_SIZE = 2048
DEFAULT_PHASH_DISTANCE = 8
DEFAULT_DHASH_DISTANCE = 24
DEFAULT_COLOR_HISTOGRAM_DISTANCE = 0.55
DEFAULT_MAX_CANDIDATE_PAIRS = 250_000
DEFAULT_MAX_REFINED_PAIRS = 250_000
DEFAULT_MAX_MATCHES = 50_000
MIN_MULTIPROCESS_PICTURES = 200


@dataclass(frozen=True)
class _WorstRankedPath:
    path: str
    rank: tuple

    def __lt__(self, other):
        if not isinstance(other, _WorstRankedPath):
            return NotImplemented
        return self.rank > other.rank


# Enough so that we're sure that the main thread will not wait after a result.get() call
# cpucount+1 should be enough to be sure that the spawned process will not wait after the results
# collection made by the main process.
try:
    RESULTS_QUEUE_LIMIT = multiprocessing.cpu_count() + 1
except Exception:
    # I had an IOError on app launch once. It seems to be a freak occurrence. In any case, we want
    # the app to launch, so let's just put an arbitrary value.
    logging.warning("Had problems to determine cpu count on launch.")
    RESULTS_QUEUE_LIMIT = 8


@dataclass(frozen=True)
class ImageCandidateStats:
    indexed_images: int
    possible_pairs: int
    candidate_pairs: int
    refined_pairs: int
    match_count: int
    phash_distance: int
    max_candidate_pairs: int
    max_refined_pairs: int
    max_matches: int
    candidate_limit_reached: bool = False
    refinement_limit_reached: bool = False
    match_limit_reached: bool = False

    def __post_init__(self):
        counts = (
            self.indexed_images,
            self.possible_pairs,
            self.candidate_pairs,
            self.refined_pairs,
            self.match_count,
        )
        if any(count < 0 for count in counts):
            raise ValueError("candidate statistics must not be negative")
        if self.candidate_pairs > self.possible_pairs:
            raise ValueError("candidate pairs cannot exceed possible pairs")
        if self.refined_pairs > self.candidate_pairs:
            raise ValueError("refined pairs cannot exceed candidate pairs")
        if self.match_count > self.refined_pairs:
            raise ValueError("matches cannot exceed refined pairs")
        if not 0 <= self.phash_distance <= 64:
            raise ValueError("pHash distance must be between 0 and 64")
        for name in (
            "max_candidate_pairs",
            "max_refined_pairs",
            "max_matches",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("{} must be a positive integer".format(name))
        for name in (
            "candidate_limit_reached",
            "refinement_limit_reached",
            "match_limit_reached",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError("{} must be boolean".format(name))

    @property
    def reduction_ratio(self):
        if not self.possible_pairs:
            return 1.0
        return 1 - (self.candidate_pairs / self.possible_pairs)


class ImageMatchResult(list):
    """List-compatible fuzzy matches with explicit scan coverage and candidate statistics."""

    def __init__(self, matches=(), scan_receipt=None, candidate_stats=None):
        super().__init__(matches)
        self.scan_receipt = scan_receipt
        self.candidate_stats = candidate_stats


@dataclass
class _PreparationResult:
    pictures: list
    features_by_path: dict
    volatile_blocks_by_id: dict
    issues: list
    skipped: int = 0
    resource_limited: bool = False
    fatal: bool = False


def get_cache(cache_path, readonly=False):
    return SqliteCache(cache_path or ":memory:", readonly=readonly)


def _uses_memory_cache(cache_path):
    return not cache_path or cache_path == ":memory:"


def prepare_pictures(pictures, cache_path, with_dimensions, match_rotated, j=job.nulljob):
    """Normalize and cache image features without presenting skipped inputs as analyzed."""

    cache = get_cache(cache_path)
    prepared = []
    features_by_path = {}
    volatile_blocks_by_id = {}
    issues = []
    skipped = 0
    resource_limited = False
    fatal = False
    try:
        cache.purge_outdated()
        for picture in j.iter_with_progress(pictures, tr("Analyzed %d/%d pictures")):
            if not picture.path:
                logging.warning("We have a picture with a null path here")
                skipped += 1
                issues.append(ScanIssue("missing_path", "Picture has no filesystem path"))
                continue
            path_str = picture.unicode_path
            logging.debug("Analyzing picture at %s", path_str)
            try:
                try:
                    features = cache.get_feature_metadata(path_str)
                    if features.feature_version != FEATURE_VERSION:
                        raise KeyError(path_str)
                    if match_rotated and features.orientation_count != 8:
                        raise KeyError(path_str)
                except (KeyError, ValueError):
                    before = capture_source_binding(path_str)
                    decoded = decode_image_features(
                        path_str,
                        block_count_per_side=BLOCK_COUNT_PER_SIDE,
                        include_orientations=match_rotated,
                    )
                    expected_orientations = 8 if match_rotated else 1
                    if decoded.feature_version != FEATURE_VERSION or decoded.orientation_count != expected_orientations:
                        raise ImageDecodeError("decoder returned incompatible image feature policy")
                    after = capture_source_binding(path_str)
                    if before.generation != after.generation or before.identity_json != after.identity_json:
                        raise ImageDecodeError("image changed while its features were being calculated")
                    cache.put_features(
                        path_str,
                        decoded,
                        expected_binding=after,
                    )
                    features = cache.get_feature_metadata(path_str)
                    if _uses_memory_cache(cache_path):
                        volatile_blocks_by_id[features.rowid] = decoded.blocks
                try:
                    picture.dimensions = features.dimensions
                except (AttributeError, TypeError):
                    # Some non-Qt test doubles expose dimensions as a read-only property.  Matching
                    # still uses the normalized dimensions from the cache record.
                    pass
                for name, value in (
                    ("bit_depth", features.quality.bit_depth),
                    ("exif_count", features.quality.exif_count),
                    ("metadata_count", features.quality.metadata_count),
                    (
                        "jpeg_artifact_score",
                        features.quality.jpeg_artifact_score,
                    ),
                ):
                    try:
                        setattr(picture, name, value)
                    except (AttributeError, TypeError):
                        # Test doubles and foreign frontends may expose read-only
                        # properties.  Matching does not depend on keeper metadata.
                        pass
                prepared.append(picture)
                features_by_path[path_str] = features
            except DecoderUnavailableError as error:
                logging.error("%s", error)
                issues.append(ScanIssue(error.code, str(error), path_str))
                fatal = True
                break
            except ImageResourceLimitError as error:
                logging.warning("Resource limit while reading %s: %s", path_str, error)
                issues.append(ScanIssue(error.code, str(error), path_str))
                resource_limited = True
                break
            except ImageDecodeError as error:
                logging.warning("Could not analyze %s: %s", path_str, error)
                issues.append(ScanIssue(error.code, str(error), path_str))
            except MemoryError:
                logging.warning("Ran out of memory while reading %s", path_str)
                issues.append(
                    ScanIssue(
                        "resource_limit",
                        "Not enough memory to cache normalized image features",
                        path_str,
                    )
                )
                resource_limited = True
                break
            except (OSError, ValueError, sqlite3.DatabaseError) as error:
                logging.warning("Could not cache image features for %s: %s", path_str, error)
                issues.append(ScanIssue("feature_cache_failure", str(error), path_str))
    except MemoryError:
        logging.warning("Ran out of memory while preparing pictures")
        issues.append(ScanIssue("resource_limit", "Not enough memory to prepare the picture scan"))
        resource_limited = True
    finally:
        cache.close()
    return _PreparationResult(
        pictures=prepared,
        features_by_path=features_by_path,
        volatile_blocks_by_id=volatile_blocks_by_id,
        issues=issues,
        skipped=skipped,
        resource_limited=resource_limited,
        fatal=fatal,
    )


def get_match(first, second, percentage):
    if percentage < 0:
        percentage = 0
    return Match(first, second, percentage)


def _dimensions_compatible(first, second, match_scaled, match_rotated):
    if match_scaled or first == second:
        return True
    return bool(match_rotated and (first[1], first[0]) == second)


def _histogram_distance(first, second):
    return sum(abs(left - right) for left, right in zip(first, second)) / (2 * 32 * 32)


def _compare_blocks(first_blocks, second_blocks, threshold, match_rotated):
    limit = 100 - threshold
    orientation_count = 8 if match_rotated else 1
    for orientation in range(orientation_count):
        try:
            diff = avgdiff(first_blocks[orientation], second_blocks[0], limit, MIN_ITERATIONS)
            percentage = 100 - diff
        except (DifferentBlockCountError, NoBlocksError):
            percentage = 0
        if percentage >= threshold:
            # A fuzzy picture scan never proves byte identity.  Exact groups are exclusively
            # created by the verified contents engine.
            return min(percentage, 99)
    return None


def async_compare_candidates(candidate_pairs, dbname, threshold, match_rotated=False):
    """Refine a bounded candidate batch in a worker process."""

    cache = get_cache(dbname, readonly=True)
    try:
        ids = sorted({cache_id for pair in candidate_pairs for cache_id in pair})
        blocks_by_id = dict(cache.get_multiple(ids))
        results = []
        for first_id, second_id in candidate_pairs:
            percentage = _compare_blocks(
                blocks_by_id[first_id],
                blocks_by_id[second_id],
                threshold,
                match_rotated,
            )
            if percentage is not None:
                results.append((first_id, second_id, percentage))
        return results, len(candidate_pairs)
    finally:
        cache.close()


def _build_candidate_index(pictures, features_by_path, max_distance, j):
    index = MultiIndexHamming(bit_width=64, max_distance=max_distance)
    ordered = sorted(pictures, key=lambda picture: picture.unicode_path)
    j.start_job(len(ordered), tr("Indexing picture fingerprints"))
    for picture in ordered:
        path_str = picture.unicode_path
        index.add(path_str, features_by_path[path_str].phashes[0])
        j.add_progress()
    return index, ordered


def _iter_candidate_batches(
    ordered,
    features_by_path,
    index,
    max_distance,
    match_scaled,
    match_rotated,
    counters,
    j,
    max_candidate_pairs=DEFAULT_MAX_CANDIDATE_PAIRS,
    dhash_distance=DEFAULT_DHASH_DISTANCE,
    color_histogram_distance=DEFAULT_COLOR_HISTOGRAM_DISTANCE,
):
    batch = []
    j.start_job(len(ordered), tr("Finding and verifying picture candidates"))
    for picture in ordered:
        remaining = max_candidate_pairs - counters["candidate_pairs"]
        if remaining <= 0:
            counters["candidate_limit_reached"] = True
            return
        first_path = picture.unicode_path
        first_features = features_by_path[first_path]
        candidates_by_path = {}
        worst_first = []

        def clean_heap():
            while worst_first:
                retained = candidates_by_path.get(worst_first[0].path)
                if retained == worst_first[0].rank:
                    break
                heapq.heappop(worst_first)

        def retain(second_path, rank):
            nonlocal worst_first
            previous = candidates_by_path.get(second_path)
            if previous is not None:
                if rank >= previous:
                    return
                candidates_by_path[second_path] = rank
                heapq.heappush(
                    worst_first,
                    _WorstRankedPath(second_path, rank),
                )
            elif len(candidates_by_path) < remaining:
                candidates_by_path[second_path] = rank
                heapq.heappush(
                    worst_first,
                    _WorstRankedPath(second_path, rank),
                )
            else:
                clean_heap()
                worst = worst_first[0]
                if rank >= worst.rank:
                    return
                heapq.heappop(worst_first)
                del candidates_by_path[worst.path]
                candidates_by_path[second_path] = rank
                heapq.heappush(
                    worst_first,
                    _WorstRankedPath(second_path, rank),
                )
            if len(worst_first) > max(64, len(candidates_by_path) * 2 + 16):
                worst_first = [
                    _WorstRankedPath(path, retained_rank) for path, retained_rank in candidates_by_path.items()
                ]
                heapq.heapify(worst_first)

        fingerprints = first_features.phashes if match_rotated else first_features.phashes[:1]
        dhashes = first_features.dhashes if match_rotated else first_features.dhashes[:1]
        for orientation, (fingerprint, first_dhash) in enumerate(zip(fingerprints, dhashes)):
            for candidate in index.iter_query(
                fingerprint,
                max_distance=max_distance,
                exclude_id=first_path,
            ):
                second_path = candidate.asset_id
                if first_path >= second_path:
                    continue
                second_picture = counters["pictures_by_path"][second_path]
                if picture.is_ref and second_picture.is_ref:
                    continue
                second_features = features_by_path[second_path]
                if not _dimensions_compatible(
                    first_features.dimensions,
                    second_features.dimensions,
                    match_scaled,
                    match_rotated,
                ):
                    continue
                second_dhash = second_features.dhashes[0]
                cheap_dhash_distance = hamming_distance(
                    first_dhash,
                    second_dhash,
                    64,
                )
                histogram_distance = _histogram_distance(
                    first_features.color_histogram,
                    second_features.color_histogram,
                )
                if cheap_dhash_distance > dhash_distance and histogram_distance > color_histogram_distance:
                    continue
                rank = (
                    candidate.distance / 64 + cheap_dhash_distance / 64 + histogram_distance,
                    candidate.distance,
                    cheap_dhash_distance,
                    histogram_distance,
                    orientation,
                    second_path,
                )
                retain(second_path, rank)
        for second_path, _rank in sorted(
            candidates_by_path.items(),
            key=lambda item: item[1],
        ):
            second_features = features_by_path[second_path]
            counters["candidate_pairs"] += 1
            batch.append((first_features.rowid, second_features.rowid))
            if counters["candidate_pairs"] >= max_candidate_pairs:
                counters["candidate_limit_reached"] = True
                yield tuple(batch)
                return
            if len(batch) >= CANDIDATE_BATCH_SIZE:
                yield tuple(batch)
                batch = []
        j.add_progress()
    if batch:
        yield tuple(batch)


def getmatches(
    pictures,
    cache_path,
    threshold,
    match_scaled=False,
    match_rotated=False,
    j=job.nulljob,
    phash_distance=DEFAULT_PHASH_DISTANCE,
    dhash_distance=DEFAULT_DHASH_DISTANCE,
    color_histogram_distance=DEFAULT_COLOR_HISTOGRAM_DISTANCE,
    max_candidate_pairs=DEFAULT_MAX_CANDIDATE_PAIRS,
    max_refined_pairs=DEFAULT_MAX_REFINED_PAIRS,
    max_matches=DEFAULT_MAX_MATCHES,
):
    """Find visual duplicates through exact-radius pHash candidates and 15x15 refinement."""

    if not isinstance(phash_distance, int) or isinstance(phash_distance, bool) or not 0 <= phash_distance <= 64:
        raise ValueError("phash_distance must be an integer between 0 and 64")
    if not isinstance(dhash_distance, int) or isinstance(dhash_distance, bool) or not 0 <= dhash_distance <= 64:
        raise ValueError("dhash_distance must be an integer between 0 and 64")
    if (
        isinstance(color_histogram_distance, bool)
        or not isinstance(color_histogram_distance, (int, float))
        or not math.isfinite(color_histogram_distance)
        or not 0 <= color_histogram_distance <= 1
    ):
        raise ValueError("color_histogram_distance must be between 0 and 1")
    for name, value in (
        ("max_candidate_pairs", max_candidate_pairs),
        ("max_refined_pairs", max_refined_pairs),
        ("max_matches", max_matches),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("{} must be a positive integer".format(name))
    pictures = list(pictures)
    started_at_ns = time.time_ns()
    scan_id = str(uuid.uuid4())
    issues = []
    resource_limited = False
    j = j.start_subjob([3, 1, 6])
    preparation = prepare_pictures(
        pictures,
        cache_path,
        not match_scaled,
        match_rotated,
        j=j,
    )
    issues.extend(preparation.issues)
    prepared = preparation.pictures
    features_by_path = preparation.features_by_path
    resource_limited = preparation.resource_limited

    raw_matches = []
    candidate_pairs = 0
    refined_pairs = 0
    candidate_limit_reached = False
    refinement_limit_reached = False
    match_limit_reached = False
    if prepared and not preparation.fatal and not resource_limited:
        try:
            index, ordered = _build_candidate_index(prepared, features_by_path, phash_distance, j)
            pictures_by_path = {picture.unicode_path: picture for picture in ordered}
            id2picture = {}
            for path_str, picture in pictures_by_path.items():
                features = features_by_path[path_str]
                picture.cache_id = features.rowid
                id2picture[features.rowid] = picture
            counters = {
                "candidate_pairs": 0,
                "candidate_limit_reached": False,
                "pictures_by_path": pictures_by_path,
            }
            batches = _iter_candidate_batches(
                ordered,
                features_by_path,
                index,
                phash_distance,
                match_scaled,
                match_rotated,
                counters,
                j,
                max_candidate_pairs=max_candidate_pairs,
                dhash_distance=dhash_distance,
                color_histogram_distance=color_histogram_distance,
            )
            use_multiprocessing = len(ordered) >= MIN_MULTIPROCESS_PICTURES and not _uses_memory_cache(cache_path)
            pool = None
            pool_finalized = False
            sequential_cache = None
            pending = []
            scheduled_pairs = 0
            try:
                for batch in batches:
                    remaining_refinements = max_refined_pairs - scheduled_pairs
                    if remaining_refinements <= 0:
                        refinement_limit_reached = True
                        break
                    if len(batch) >= remaining_refinements:
                        batch = batch[:remaining_refinements]
                        refinement_limit_reached = True
                    scheduled_pairs += len(batch)
                    if use_multiprocessing:
                        if pool is None:
                            worker_count = min(max(1, RESULTS_QUEUE_LIMIT - 1), 8)
                            pool = multiprocessing.Pool(processes=worker_count)
                        pending.append(
                            pool.apply_async(
                                async_compare_candidates,
                                (batch, cache_path, threshold, match_rotated),
                            )
                        )
                        if len(pending) >= RESULTS_QUEUE_LIMIT:
                            batch_matches, batch_count = pending.pop(0).get()
                            refined_pairs += batch_count
                            available = max_matches - len(raw_matches)
                            raw_matches.extend(batch_matches[:available])
                            if len(raw_matches) >= max_matches:
                                match_limit_reached = True
                                break
                    else:
                        if _uses_memory_cache(cache_path):
                            blocks_by_id = preparation.volatile_blocks_by_id
                        else:
                            if sequential_cache is None:
                                sequential_cache = get_cache(cache_path, readonly=True)
                            ids = sorted({cache_id for pair in batch for cache_id in pair})
                            blocks_by_id = dict(sequential_cache.get_multiple(ids))
                        for first_id, second_id in batch:
                            percentage = _compare_blocks(
                                blocks_by_id[first_id],
                                blocks_by_id[second_id],
                                threshold,
                                match_rotated,
                            )
                            refined_pairs += 1
                            if percentage is not None:
                                raw_matches.append((first_id, second_id, percentage))
                                if len(raw_matches) >= max_matches:
                                    match_limit_reached = True
                                    break
                    if refinement_limit_reached or match_limit_reached:
                        break
                if not match_limit_reached:
                    for async_result in pending:
                        batch_matches, batch_count = async_result.get()
                        refined_pairs += batch_count
                        available = max_matches - len(raw_matches)
                        raw_matches.extend(batch_matches[:available])
                        if len(raw_matches) >= max_matches:
                            match_limit_reached = True
                            break
                if pool is not None:
                    if match_limit_reached:
                        pool.terminate()
                    else:
                        pool.close()
                    pool_finalized = True
            except MemoryError:
                resource_limited = True
                issues.append(
                    ScanIssue(
                        "resource_limit",
                        "Not enough memory to finish picture candidate refinement",
                    )
                )
                logging.warning(
                    "Picture candidate refinement stopped at %d verified candidates",
                    refined_pairs,
                )
                if pool is not None:
                    pool.terminate()
                    pool_finalized = True
            except BaseException:
                if pool is not None:
                    pool.terminate()
                    pool_finalized = True
                raise
            finally:
                if sequential_cache is not None:
                    sequential_cache.close()
                if pool is not None:
                    if not pool_finalized:
                        pool.terminate()
                    pool.join()
            candidate_pairs = counters["candidate_pairs"]
            candidate_limit_reached = counters["candidate_limit_reached"]
        except MemoryError:
            resource_limited = True
            issues.append(
                ScanIssue(
                    "resource_limit",
                    "Not enough memory to build the picture candidate index",
                )
            )
            logging.warning("Picture candidate indexing stopped because of a memory limit")
            id2picture = {}
            raw_matches = []
    else:
        id2picture = {}

    if candidate_limit_reached:
        resource_limited = True
        issues.append(
            ScanIssue(
                "candidate_pair_limit",
                "Picture scan reached max_candidate_pairs ({})".format(max_candidate_pairs),
            )
        )
    if refinement_limit_reached:
        resource_limited = True
        issues.append(
            ScanIssue(
                "refinement_pair_limit",
                "Picture scan reached max_refined_pairs ({})".format(max_refined_pairs),
            )
        )
    if match_limit_reached:
        resource_limited = True
        issues.append(
            ScanIssue(
                "match_limit",
                "Picture scan reached max_matches ({})".format(max_matches),
            )
        )

    result = []
    try:
        for ref_id, other_id, percentage in raw_matches:
            if percentage >= threshold:
                result.append(get_match(id2picture[ref_id], id2picture[other_id], percentage))
    except MemoryError:
        resource_limited = True
        issues.append(
            ScanIssue(
                "resource_limit",
                "Not enough memory to materialize all refined picture matches",
            )
        )
        logging.warning("Picture result materialization stopped at %d matches", len(result))

    failed = sum(1 for issue in issues if issue.path)
    if preparation.fatal:
        status = ScanStatus.FAILED
    elif resource_limited:
        status = ScanStatus.RESOURCE_LIMIT
    elif issues:
        status = ScanStatus.COMPLETE_WITH_SKIPS
    else:
        status = ScanStatus.COMPLETE
    receipt = ScanReceipt(
        scan_id=scan_id,
        status=status,
        discovered=len(pictures),
        analyzed=len(prepared),
        skipped=preparation.skipped,
        failed=failed,
        started_at_ns=started_at_ns,
        finished_at_ns=time.time_ns(),
        issues=tuple(issues),
    )
    possible_pairs = len(prepared) * (len(prepared) - 1) // 2
    stats = ImageCandidateStats(
        indexed_images=len(prepared),
        possible_pairs=possible_pairs,
        candidate_pairs=candidate_pairs,
        refined_pairs=refined_pairs,
        match_count=len(result),
        phash_distance=phash_distance,
        max_candidate_pairs=max_candidate_pairs,
        max_refined_pairs=max_refined_pairs,
        max_matches=max_matches,
        candidate_limit_reached=candidate_limit_reached,
        refinement_limit_reached=refinement_limit_reached,
        match_limit_reached=match_limit_reached,
    )
    return ImageMatchResult(result, scan_receipt=receipt, candidate_stats=stats)


multiprocessing.freeze_support()
