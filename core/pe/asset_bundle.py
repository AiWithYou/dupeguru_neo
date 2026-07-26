# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Sidecar-aware image asset bundles and conflict auditing."""

from __future__ import annotations

import codecs
import contextlib
import ctypes
import hashlib
import os
import stat
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Dict, Iterable, Iterator, List, Mapping, Optional, Set, Tuple, Union

from core.file_generation import get_file_generation_token, get_file_generation_token_from_fd
from core.pe.evidence import stable_group_id

PathInput = Union[str, Path]

MAX_SIDECAR_BYTES = 16 * 1024 * 1024
SIDECAR_READ_CHUNK_SIZE = 64 * 1024


class SidecarNaming(Enum):
    STEM = "stem"
    FULL_NAME = "full_name"
    BOTH = "both"


class SidecarReadStatus(Enum):
    OK = "ok"
    INVALID_UTF8 = "invalid_utf8"
    TOO_LARGE = "too_large"
    UNSAFE_PATH = "unsafe_path"
    CHANGED_DURING_READ = "changed_during_read"
    READ_ERROR = "read_error"


class SidecarIssueKind(Enum):
    ORPHAN = "orphan"
    AMBIGUOUS_OWNER = "ambiguous_owner"
    MISSING_REQUIRED = "missing_required"
    MULTIPLE_FOR_SLOT = "multiple_for_slot"
    INVALID_UTF8 = "invalid_utf8"
    TOO_LARGE = "too_large"
    UNSAFE_PATH = "unsafe_path"
    CHANGED_DURING_READ = "changed_during_read"
    READ_ERROR = "read_error"
    CLUSTER_PRESENCE_MISMATCH = "cluster_presence_mismatch"
    CLUSTER_CONTENT_MISMATCH = "cluster_content_mismatch"
    UNKNOWN_ASSET = "unknown_asset"


def _normalize_extension(extension: str) -> str:
    result = extension.strip().lower()
    if not result:
        raise ValueError("sidecar extension must not be empty")
    if not result.startswith("."):
        result = "." + result
    if "/" in result or "\\" in result:
        raise ValueError("sidecar extension must not contain path separators")
    return result


@dataclass(frozen=True)
class SidecarPolicy:
    extensions: Tuple[str, ...] = (".txt", ".caption", ".json")
    required_extensions: Tuple[str, ...] = ()
    text_extensions: Optional[Tuple[str, ...]] = None
    naming: SidecarNaming = SidecarNaming.BOTH
    case_sensitive: bool = True

    def __post_init__(self) -> None:
        extensions = tuple(sorted({_normalize_extension(extension) for extension in self.extensions}))
        required = tuple(sorted({_normalize_extension(extension) for extension in self.required_extensions}))
        if self.text_extensions is None:
            known_text_extensions = {".txt", ".caption", ".json"}
            text = tuple(extension for extension in extensions if extension in known_text_extensions)
        else:
            text = tuple(sorted({_normalize_extension(extension) for extension in self.text_extensions}))
        if not extensions:
            raise ValueError("sidecar policy requires at least one extension")
        if not set(required) <= set(extensions):
            raise ValueError("required sidecar extensions must be included in extensions")
        if not set(text) <= set(extensions):
            raise ValueError("text sidecar extensions must be included in extensions")
        object.__setattr__(self, "extensions", extensions)
        object.__setattr__(self, "required_extensions", required)
        object.__setattr__(self, "text_extensions", text)


class _UnsafeSidecarError(Exception):
    pass


class _SidecarChangedError(Exception):
    pass


@dataclass(frozen=True)
class _SidecarSnapshot:
    device: int
    file_id: int
    file_type: int
    size: int
    mtime_ns: int
    generation_token: bytes
    link_count: int


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(marker and attributes & marker)


def _require_single_link_regular(file_stat: os.stat_result) -> None:
    if stat.S_ISLNK(file_stat.st_mode) or _is_reparse_point(file_stat):
        raise _UnsafeSidecarError("sidecar symbolic links and reparse points are forbidden")
    if not stat.S_ISREG(file_stat.st_mode):
        raise _UnsafeSidecarError("sidecar must be a regular file")
    if int(getattr(file_stat, "st_nlink", 0)) != 1:
        raise _UnsafeSidecarError("sidecar must have exactly one filesystem link")
    if int(getattr(file_stat, "st_ino", 0)) == 0:
        raise _UnsafeSidecarError("sidecar physical identity is unavailable")


def _snapshot(file_stat: os.stat_result, generation_token: bytes) -> _SidecarSnapshot:
    _require_single_link_regular(file_stat)
    return _SidecarSnapshot(
        device=int(file_stat.st_dev),
        file_id=int(file_stat.st_ino),
        file_type=stat.S_IFMT(file_stat.st_mode),
        size=int(file_stat.st_size),
        mtime_ns=int(file_stat.st_mtime_ns),
        generation_token=generation_token,
        link_count=int(file_stat.st_nlink),
    )


def _require_no_link_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current.joinpath(part)
        component_stat = os.stat(current, follow_symlinks=False)
        if stat.S_ISLNK(component_stat.st_mode) or _is_reparse_point(component_stat):
            raise _UnsafeSidecarError("sidecar path contains a symbolic link or reparse point: '{}'".format(current))
        if current != absolute and not stat.S_ISDIR(component_stat.st_mode):
            raise _UnsafeSidecarError("sidecar parent component is not a directory: '{}'".format(current))


def _path_snapshot(path: Path, *, initial: bool) -> _SidecarSnapshot:
    try:
        _require_no_link_components(path)
        file_stat = os.stat(path, follow_symlinks=False)
    except (OSError, _UnsafeSidecarError) as error:
        if initial:
            raise
        raise _SidecarChangedError("sidecar path could not be revalidated: {}".format(error)) from error
    try:
        token = get_file_generation_token(path, stat_result=file_stat)
        return _snapshot(file_stat, token.encoded)
    except _UnsafeSidecarError as error:
        if initial:
            raise
        raise _SidecarChangedError("sidecar path became unsafe: {}".format(error)) from error


def _windows_extended_path(path: Path) -> str:
    absolute = os.path.abspath(os.fspath(path))
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _open_windows_no_follow(path: Path) -> int:
    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    share_read = 0x00000001
    share_delete = 0x00000004
    open_existing = 3
    sequential_scan = 0x08000000
    open_reparse_point = 0x00200000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        _windows_extended_path(path),
        generic_read,
        share_read | share_delete,
        None,
        open_existing,
        sequential_scan | open_reparse_point,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        return msvcrt.open_osfhandle(handle, flags)
    except Exception:
        close_handle(handle)
        raise


@contextlib.contextmanager
def _open_no_follow(path: Path) -> Iterator[BinaryIO]:
    if os.name == "nt":
        descriptor = _open_windows_no_follow(path)
    else:
        if not hasattr(os, "O_NOFOLLOW"):
            raise _UnsafeSidecarError("this platform cannot open sidecars without following links")
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb", closefd=True) as stream:
        yield stream


def _read_chunk(stream: BinaryIO, size: int) -> bytes:
    return stream.read(size)


@dataclass(frozen=True)
class SidecarAsset:
    path: str
    slot: str
    size: int
    digest: bytes
    read_status: SidecarReadStatus
    error: str = ""

    @classmethod
    def read(cls, path: PathInput, slot: str, text: bool) -> "SidecarAsset":
        normalized_slot = _normalize_extension(slot)
        path_obj = Path(path)
        read_path = Path(os.path.abspath(os.fspath(path_obj)))
        try:
            before = _path_snapshot(read_path, initial=True)
            if before.size > MAX_SIDECAR_BYTES:
                return cls(
                    str(path_obj),
                    normalized_slot,
                    0,
                    b"",
                    SidecarReadStatus.TOO_LARGE,
                    "sidecar is {} bytes; the per-file limit is {} bytes".format(
                        before.size,
                        MAX_SIDECAR_BYTES,
                    ),
                )
            digest = hashlib.sha256()
            decoder = codecs.getincrementaldecoder("utf-8")(errors="strict") if text else None
            unicode_error = ""
            bytes_read = 0
            with _open_no_follow(read_path) as stream:
                opened_before_stat = os.fstat(stream.fileno())
                opened_before = _snapshot(
                    opened_before_stat,
                    get_file_generation_token_from_fd(
                        stream.fileno(),
                        path=read_path,
                        stat_result=opened_before_stat,
                    ).encoded,
                )
                if before != opened_before:
                    raise _SidecarChangedError("sidecar changed between path validation and open")
                while True:
                    read_size = min(
                        SIDECAR_READ_CHUNK_SIZE,
                        MAX_SIDECAR_BYTES - bytes_read + 1,
                    )
                    chunk = _read_chunk(stream, read_size)
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if bytes_read > MAX_SIDECAR_BYTES:
                        raise _SidecarChangedError("sidecar grew beyond the per-file limit while being read")
                    digest.update(chunk)
                    if decoder is not None:
                        try:
                            decoder.decode(chunk, final=False)
                        except UnicodeDecodeError as error:
                            unicode_error = str(error)
                            decoder = None
                opened_after_stat = os.fstat(stream.fileno())
                opened_after = _snapshot(
                    opened_after_stat,
                    get_file_generation_token_from_fd(
                        stream.fileno(),
                        path=read_path,
                        stat_result=opened_after_stat,
                    ).encoded,
                )
            if opened_before != opened_after:
                raise _SidecarChangedError("sidecar handle changed while being read")
            if bytes_read != opened_before.size:
                raise _SidecarChangedError(
                    "sidecar byte count changed while being read: expected {}, read {}".format(
                        opened_before.size,
                        bytes_read,
                    )
                )
            after = _path_snapshot(read_path, initial=False)
            if before != after or opened_after != after:
                raise _SidecarChangedError("sidecar path identity or content generation changed while being read")
            if decoder is not None:
                try:
                    decoder.decode(b"", final=True)
                except UnicodeDecodeError as error:
                    unicode_error = str(error)
            status = SidecarReadStatus.INVALID_UTF8 if unicode_error else SidecarReadStatus.OK
            return cls(
                path=str(path_obj),
                slot=normalized_slot,
                size=bytes_read,
                digest=digest.digest(),
                read_status=status,
                error=unicode_error,
            )
        except _UnsafeSidecarError as error:
            return cls(
                str(path_obj),
                normalized_slot,
                0,
                b"",
                SidecarReadStatus.UNSAFE_PATH,
                str(error),
            )
        except _SidecarChangedError as error:
            return cls(
                str(path_obj),
                normalized_slot,
                0,
                b"",
                SidecarReadStatus.CHANGED_DURING_READ,
                str(error),
            )
        except (OSError, ValueError) as error:
            return cls(str(path_obj), normalized_slot, 0, b"", SidecarReadStatus.READ_ERROR, str(error))

    @property
    def digest_hex(self) -> str:
        return self.digest.hex()


@dataclass(frozen=True)
class AssetBundle:
    asset_id: str
    primary_path: str
    sidecars: Tuple[SidecarAsset, ...] = ()
    _sidecars_by_slot: Mapping[str, Tuple[SidecarAsset, ...]] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not self.asset_id or not self.primary_path:
            raise ValueError("asset bundle requires an ID and primary path")
        sidecars = tuple(sorted(self.sidecars, key=lambda item: (item.slot, item.path)))
        sidecars_by_slot: Dict[str, List[SidecarAsset]] = {}
        for sidecar in sidecars:
            sidecars_by_slot.setdefault(sidecar.slot, []).append(sidecar)
        object.__setattr__(self, "sidecars", sidecars)
        object.__setattr__(
            self,
            "_sidecars_by_slot",
            MappingProxyType({slot: tuple(values) for slot, values in sidecars_by_slot.items()}),
        )

    def sidecars_for(self, slot: str) -> Tuple[SidecarAsset, ...]:
        normalized_slot = _normalize_extension(slot)
        return self._sidecars_by_slot.get(normalized_slot, ())


@dataclass(frozen=True)
class SidecarIssue:
    kind: SidecarIssueKind
    asset_ids: Tuple[str, ...] = ()
    paths: Tuple[str, ...] = ()
    slot: str = ""
    group_id: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_ids", tuple(sorted(set(self.asset_ids))))
        object.__setattr__(self, "paths", tuple(sorted(set(self.paths))))


def _issue_sort_key(issue: SidecarIssue) -> Tuple[object, ...]:
    return (issue.kind.value, issue.group_id, issue.slot, issue.asset_ids, issue.paths, issue.detail)


@dataclass(frozen=True)
class BundleCatalog:
    bundles: Tuple[AssetBundle, ...]
    issues: Tuple[SidecarIssue, ...]
    policy: SidecarPolicy

    def __post_init__(self) -> None:
        bundles = tuple(sorted(self.bundles, key=lambda bundle: bundle.asset_id))
        if len({bundle.asset_id for bundle in bundles}) != len(bundles):
            raise ValueError("bundle asset IDs must be unique")
        object.__setattr__(self, "bundles", bundles)
        object.__setattr__(self, "issues", tuple(sorted(self.issues, key=_issue_sort_key)))

    def by_id(self) -> Dict[str, AssetBundle]:
        return {bundle.asset_id: bundle for bundle in self.bundles}


def _path_key(path: Path, case_sensitive: bool) -> str:
    value = str(path)
    return value if case_sensitive else value.casefold()


def _candidate_sidecar_paths(primary_path: Path, extension: str, naming: SidecarNaming) -> Set[Path]:
    result = set()
    if naming in {SidecarNaming.STEM, SidecarNaming.BOTH}:
        result.add(primary_path.with_suffix(extension))
    if naming in {SidecarNaming.FULL_NAME, SidecarNaming.BOTH}:
        result.add(Path(str(primary_path) + extension))
    return result


def build_asset_bundles(
    primary_paths: Mapping[str, PathInput],
    sidecar_paths: Iterable[PathInput],
    policy: Optional[SidecarPolicy] = None,
) -> BundleCatalog:
    """Associate explicit sidecar paths with primary assets without moving any files."""

    active_policy = policy or SidecarPolicy()
    normalized_primaries: Dict[str, Path] = {}
    seen_primary_paths = set()
    for asset_id in sorted(primary_paths):
        if not asset_id:
            raise ValueError("primary asset ID must not be empty")
        path = Path(primary_paths[asset_id])
        key = _path_key(path, active_policy.case_sensitive)
        if key in seen_primary_paths:
            raise ValueError("primary paths must be unique")
        seen_primary_paths.add(key)
        normalized_primaries[asset_id] = path

    owner_index: Dict[str, Set[str]] = {}
    for asset_id, primary_path in normalized_primaries.items():
        for extension in active_policy.extensions:
            for candidate in _candidate_sidecar_paths(primary_path, extension, active_policy.naming):
                owner_index.setdefault(_path_key(candidate, active_policy.case_sensitive), set()).add(asset_id)

    attached: Dict[str, List[SidecarAsset]] = {asset_id: [] for asset_id in normalized_primaries}
    issues: List[SidecarIssue] = []
    seen_sidecars = set()
    read_issue_kinds = {
        SidecarReadStatus.INVALID_UTF8: SidecarIssueKind.INVALID_UTF8,
        SidecarReadStatus.TOO_LARGE: SidecarIssueKind.TOO_LARGE,
        SidecarReadStatus.UNSAFE_PATH: SidecarIssueKind.UNSAFE_PATH,
        SidecarReadStatus.CHANGED_DURING_READ: SidecarIssueKind.CHANGED_DURING_READ,
        SidecarReadStatus.READ_ERROR: SidecarIssueKind.READ_ERROR,
    }
    for raw_sidecar_path in sorted((Path(path) for path in sidecar_paths), key=lambda path: str(path)):
        key = _path_key(raw_sidecar_path, active_policy.case_sensitive)
        if key in seen_sidecars:
            continue
        seen_sidecars.add(key)
        extension = _normalize_extension(raw_sidecar_path.suffix) if raw_sidecar_path.suffix else ""
        owners = owner_index.get(key, set()) if extension in active_policy.extensions else set()
        if not owners:
            issues.append(SidecarIssue(SidecarIssueKind.ORPHAN, paths=(str(raw_sidecar_path),), slot=extension))
            continue
        if len(owners) > 1:
            issues.append(
                SidecarIssue(
                    SidecarIssueKind.AMBIGUOUS_OWNER,
                    asset_ids=tuple(owners),
                    paths=(str(raw_sidecar_path),),
                    slot=extension,
                )
            )
            continue
        owner = next(iter(owners))
        sidecar = SidecarAsset.read(
            raw_sidecar_path,
            extension,
            text=extension in active_policy.text_extensions,
        )
        attached[owner].append(sidecar)
        issue_kind = read_issue_kinds.get(sidecar.read_status)
        if issue_kind is not None:
            issues.append(
                SidecarIssue(
                    issue_kind,
                    asset_ids=(owner,),
                    paths=(sidecar.path,),
                    slot=extension,
                    detail=sidecar.error,
                )
            )

    bundles = []
    for asset_id, primary_path in normalized_primaries.items():
        sidecars = tuple(attached[asset_id])
        by_slot: Dict[str, List[SidecarAsset]] = {}
        for sidecar in sidecars:
            by_slot.setdefault(sidecar.slot, []).append(sidecar)
        for slot, same_slot in by_slot.items():
            if len(same_slot) > 1:
                issues.append(
                    SidecarIssue(
                        SidecarIssueKind.MULTIPLE_FOR_SLOT,
                        asset_ids=(asset_id,),
                        paths=tuple(sidecar.path for sidecar in same_slot),
                        slot=slot,
                    )
                )
        for required in active_policy.required_extensions:
            if required not in by_slot:
                issues.append(
                    SidecarIssue(
                        SidecarIssueKind.MISSING_REQUIRED,
                        asset_ids=(asset_id,),
                        paths=(str(primary_path),),
                        slot=required,
                    )
                )
        bundles.append(AssetBundle(asset_id, str(primary_path), sidecars))
    return BundleCatalog(tuple(bundles), tuple(issues), active_policy)


def audit_sidecar_conflicts(
    catalog: BundleCatalog,
    member_groups: Iterable[Iterable[str]],
) -> Tuple[SidecarIssue, ...]:
    """Report sidecar presence and content conflicts within duplicate/leakage groups."""

    bundles = catalog.by_id()
    issues: List[SidecarIssue] = []
    normalized_groups = []
    for members in member_groups:
        member_tuple = tuple(sorted(set(members)))
        if len(member_tuple) < 2:
            continue
        normalized_groups.append(member_tuple)
    for members in sorted(normalized_groups):
        group_id = stable_group_id("sidecar-audit", members)
        unknown = tuple(member for member in members if member not in bundles)
        if unknown:
            issues.append(
                SidecarIssue(
                    SidecarIssueKind.UNKNOWN_ASSET,
                    asset_ids=unknown,
                    group_id=group_id,
                    detail="group member is not present in the bundle catalog",
                )
            )
        known_members = tuple(member for member in members if member in bundles)
        if len(known_members) < 2:
            continue
        slots = sorted(
            {sidecar.slot for member in known_members for sidecar in bundles[member].sidecars}
            | set(catalog.policy.required_extensions)
        )
        for slot in slots:
            sidecars_by_member = {member: bundles[member].sidecars_for(slot) for member in known_members}
            present = tuple(member for member, sidecars in sidecars_by_member.items() if sidecars)
            missing = tuple(member for member, sidecars in sidecars_by_member.items() if not sidecars)
            if present and missing:
                issues.append(
                    SidecarIssue(
                        SidecarIssueKind.CLUSTER_PRESENCE_MISMATCH,
                        asset_ids=known_members,
                        paths=tuple(sidecar.path for sidecars in sidecars_by_member.values() for sidecar in sidecars),
                        slot=slot,
                        group_id=group_id,
                        detail="present: {}; missing: {}".format(", ".join(present), ", ".join(missing)),
                    )
                )
            readable_digests = {
                sidecar.digest
                for sidecars in sidecars_by_member.values()
                for sidecar in sidecars
                if sidecar.read_status in {SidecarReadStatus.OK, SidecarReadStatus.INVALID_UTF8}
            }
            if len(readable_digests) > 1:
                issues.append(
                    SidecarIssue(
                        SidecarIssueKind.CLUSTER_CONTENT_MISMATCH,
                        asset_ids=known_members,
                        paths=tuple(sidecar.path for sidecars in sidecars_by_member.values() for sidecar in sidecars),
                        slot=slot,
                        group_id=group_id,
                    )
                )
    return tuple(sorted(issues, key=_issue_sort_key))
