# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Explainable, deterministic keeper selection.

The policy only chooses review order.  It never proves that a non-selected file
is safe to delete; destructive eligibility remains a separate exact-evidence
decision.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Sequence, Tuple

COPY_NAME_RE = re.compile(
    r"(?:^|[\s._-])(?:copy|backup|duplicate|resized|small|thumb|thumbnail)(?:$|[\s._-])",
    re.IGNORECASE,
)
NUMBERED_COPY_RE = re.compile(
    r"(?:[\s._-]\d+|\(\d+\)|\[\d+\]|\{\d+\}|(?:copy|duplicate)[\s_-]*\d*)$",
    re.IGNORECASE,
)
TEMPORARY_PARTS = {
    "download",
    "downloads",
    "temp",
    "tmp",
    "cache",
    "thumbnails",
    "recycle.bin",
}
RAW_EXTENSIONS = {
    "3fr",
    "arw",
    "cr2",
    "cr3",
    "dng",
    "erf",
    "fff",
    "iiq",
    "kdc",
    "mef",
    "mos",
    "mrw",
    "nef",
    "nrw",
    "orf",
    "pef",
    "raf",
    "raw",
    "rw2",
    "sr2",
    "srf",
    "x3f",
}
LOSSLESS_EXTENSIONS = {"bmp", "flac", "gif", "png", "tif", "tiff", "wav"} | RAW_EXTENSIONS


@dataclass(frozen=True)
class KeeperReason:
    code: str
    points: float
    message: str


@dataclass(frozen=True)
class KeeperCandidate:
    file: object
    score: float
    reasons: Tuple[KeeperReason, ...]


@dataclass(frozen=True)
class KeeperDecision:
    """Ranked candidates plus human-readable reasons."""

    candidates: Tuple[KeeperCandidate, ...]
    _candidate_by_identity: Mapping[int, KeeperCandidate] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        candidates_by_identity = {}
        for candidate in self.candidates:
            candidates_by_identity.setdefault(
                id(candidate.file),
                candidate,
            )
        object.__setattr__(
            self,
            "_candidate_by_identity",
            MappingProxyType(candidates_by_identity),
        )

    @property
    def keeper(self):
        return self.candidates[0].file

    def candidate_for(self, file) -> KeeperCandidate:
        candidate = self._candidate_by_identity.get(id(file))
        if candidate is None or candidate.file is not file:
            raise KeyError(file)
        return candidate

    def sort_key(self, file):
        candidate = self.candidate_for(file)
        return (
            -int(bool(getattr(file, "is_ref", False))),
            -candidate.score,
            _stable_path(file),
        )

    def explanation(self, file) -> str:
        candidate = self.candidate_for(file)
        positive = [reason.message for reason in candidate.reasons if reason.points > 0]
        negative = [reason.message for reason in candidate.reasons if reason.points < 0]
        if file is self.keeper:
            prefix = "Keep candidate"
            details = positive or ["stable tie-break"]
        else:
            prefix = "Review candidate"
            keeper_reasons = {reason.code: reason for reason in self.candidates[0].reasons}
            candidate_reasons = {reason.code: reason for reason in candidate.reasons}
            weaker = []
            comparative_labels = {
                "resolution": "lower resolution than the keeper",
                "bit_depth": "lower bit depth than the keeper",
                "metadata": "less metadata retained than the keeper",
                "bitrate": "lower bitrate than the keeper",
                "lossless": "less preferred file format than the keeper",
                "file_size": "smaller source file than the keeper",
            }
            for code, label in comparative_labels.items():
                keeper_points = keeper_reasons.get(
                    code,
                    KeeperReason(code, 0.0, ""),
                ).points
                candidate_points = candidate_reasons.get(
                    code,
                    KeeperReason(code, 0.0, ""),
                ).points
                if keeper_points > candidate_points + 1e-9:
                    weaker.append(label)
            details = negative + weaker
            if not details:
                details = [
                    "lower aggregate policy score ({:.3f} vs {:.3f})".format(
                        candidate.score,
                        self.candidates[0].score,
                    )
                ]
        return "{}: {}".format(prefix, ", ".join(details))


def _stable_path(file) -> str:
    return str(getattr(file, "path", "")).casefold()


def _extension(file) -> str:
    extension = getattr(file, "extension", None)
    if extension is None:
        extension = Path(str(getattr(file, "name", ""))).suffix
    return str(extension).lstrip(".").casefold()


def _dimensions(file) -> Optional[Tuple[int, int]]:
    try:
        value = getattr(file, "dimensions", None)
    except (OSError, ValueError):
        return None
    if not isinstance(value, Sequence) or len(value) != 2:
        return None
    try:
        width, height = int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _numeric_attribute(file, names: Iterable[str]) -> float:
    for name in names:
        try:
            value = getattr(file, name, None)
        except (OSError, ValueError):
            continue
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
            return float(value)
    return 0.0


def _exif_count(file) -> float:
    for name in ("exif_count", "metadata_count"):
        value = _numeric_attribute(file, (name,))
        if value:
            return value
    try:
        exif = getattr(file, "exif", None)
    except (OSError, ValueError):
        return 0.0
    if isinstance(exif, Mapping):
        return float(len(exif))
    return 0.0


def _ratio(value: float, maximum: float) -> float:
    if value <= 0 or maximum <= 0:
        return 0.0
    return min(value / maximum, 1.0)


def choose_keeper(files: Iterable[object]) -> KeeperDecision:
    """Score a duplicate group without conflating quality with deletion proof."""

    members = tuple(files)
    if not members:
        raise ValueError("keeper selection requires at least one file")
    pixels = {}
    depths = {}
    metadata_counts = {}
    bitrates = {}
    sizes = {}
    for file in members:
        dimensions = _dimensions(file)
        pixels[file] = float(dimensions[0] * dimensions[1]) if dimensions else 0.0
        depths[file] = _numeric_attribute(file, ("bit_depth", "bits_per_sample"))
        metadata_counts[file] = _exif_count(file)
        bitrates[file] = _numeric_attribute(file, ("bitrate",))
        sizes[file] = _numeric_attribute(file, ("size",))
    maxima = (
        max(pixels.values(), default=0.0),
        max(depths.values(), default=0.0),
        max(metadata_counts.values(), default=0.0),
        max(bitrates.values(), default=0.0),
        max(sizes.values(), default=0.0),
    )

    candidates = []
    for file in members:
        reasons = []
        if bool(getattr(file, "is_ref", False)):
            comparison_pool = getattr(file, "comparison_pool", None)
            if comparison_pool == "protected":
                reasons.append(KeeperReason("protected", 100.0, "protected library"))
            elif comparison_pool == "compare_only":
                reasons.append(
                    KeeperReason(
                        "compare_only",
                        100.0,
                        "immutable Compare Only source",
                    )
                )
            else:
                reasons.append(KeeperReason("reference", 100.0, "reference source"))
        if pixels[file]:
            points = 30.0 * _ratio(pixels[file], maxima[0])
            reasons.append(KeeperReason("resolution", points, "higher resolution"))
        if depths[file]:
            points = 20.0 * _ratio(depths[file], maxima[1])
            reasons.append(KeeperReason("bit_depth", points, "higher bit depth"))
        if metadata_counts[file]:
            points = 15.0 * _ratio(metadata_counts[file], maxima[2])
            reasons.append(KeeperReason("metadata", points, "more metadata retained"))
        if bitrates[file]:
            points = 20.0 * _ratio(bitrates[file], maxima[3])
            reasons.append(KeeperReason("bitrate", points, "higher bitrate"))
        extension = _extension(file)
        if extension in LOSSLESS_EXTENSIONS:
            label = "camera RAW" if extension in RAW_EXTENSIONS else "lossless format"
            reasons.append(KeeperReason("lossless", 10.0, label))
        if sizes[file]:
            reasons.append(
                KeeperReason(
                    "file_size",
                    5.0 * _ratio(sizes[file], maxima[4]),
                    "larger source file",
                )
            )
        stem = Path(str(getattr(file, "name", ""))).stem
        if COPY_NAME_RE.search(stem) or NUMBERED_COPY_RE.search(stem):
            reasons.append(KeeperReason("copy_name", -20.0, "copy/backup-style filename"))
        path_parts = {part.casefold() for part in Path(str(getattr(file, "path", ""))).parts}
        if path_parts & TEMPORARY_PARTS:
            reasons.append(KeeperReason("temporary_path", -50.0, "temporary/download folder"))
        jpeg_artifact_score = _numeric_attribute(file, ("jpeg_artifact_score", "jpeg_blockiness"))
        if jpeg_artifact_score:
            penalty = -30.0 * min(jpeg_artifact_score, 1.0)
            reasons.append(KeeperReason("jpeg_artifacts", penalty, "JPEG recompression artifacts"))
        path_depth = len(Path(str(getattr(file, "path", ""))).parts)
        if path_depth:
            reasons.append(KeeperReason("path_context", path_depth / 1000.0, "more specific library path"))
        score = sum(reason.points for reason in reasons)
        candidates.append(KeeperCandidate(file=file, score=score, reasons=tuple(reasons)))
    candidates.sort(
        key=lambda candidate: (
            -int(bool(getattr(candidate.file, "is_ref", False))),
            -candidate.score,
            _stable_path(candidate.file),
        )
    )
    return KeeperDecision(tuple(candidates))
