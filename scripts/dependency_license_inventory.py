#!/usr/bin/env python3

"""Generate and verify the license inventory embedded in portable bundles."""

from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import sys
import tempfile
from urllib.parse import urlsplit

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_SCHEMA = "dupeguru.third-party-license-inventory"
_SCHEMA_VERSION = 2
_SOURCE_LOCK_SCHEMA = "dupeguru.release-source-lock"
_SOURCE_LOCK_SCHEMA_VERSION = 1
_MANIFEST_ALGORITHM = "sha256-length-prefixed-v1"
_MANIFEST_DOMAIN = b"dupeguru-installed-distribution-files-v1\0"
_MAX_LOCK_SIZE = 1024 * 1024
_MAX_SOURCE_LOCK_SIZE = 4 * 1024 * 1024
_MAX_INVENTORY_INDEX_SIZE = 8 * 1024 * 1024
_MAX_INVENTORY_FILES = 2048
_MAX_SOURCE_ENTRIES = 512
_MAX_PROVIDERS_PER_SOURCE = 32
_MAX_LICENSE_FILE_SIZE = 16 * 1024 * 1024
_MAX_LICENSE_TOTAL_SIZE = 64 * 1024 * 1024
_MAX_DISTRIBUTION_FILES = 100_000
_MAX_TOTAL_DISTRIBUTION_FILES = 250_000
_MAX_INSTALLED_FILE_SIZE = 2 * 1024 * 1024 * 1024
_MAX_DISTRIBUTION_BYTES = 8 * 1024 * 1024 * 1024
_MAX_TOTAL_INSTALLED_BYTES = 16 * 1024 * 1024 * 1024
_MAX_RECORD_SIZE = 64 * 1024 * 1024
_MAX_DISTRIBUTION_PATH_BYTES = 4096
_MAX_SOURCE_ARCHIVE_SIZE = 2 * 1024 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LICENSE_LEAF = re.compile(
    r"^(?:licen[cs]e|copying|notice|copyright)(?:[._-].*)?$",
    re.IGNORECASE,
)
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._+-]+")
_SOURCE_ENTRY_KEYS = {
    "filename",
    "kind",
    "name",
    "provides",
    "revision",
    "sha256",
    "size",
    "tag",
    "url",
    "version",
}
_SOURCE_ENTRY_REQUIRED_KEYS = {
    "filename",
    "kind",
    "name",
    "provides",
    "sha256",
    "size",
    "url",
    "version",
}
_PACKAGE_KEYS = {
    "canonical_name",
    "files",
    "installed_provenance",
    "license",
    "license_classifiers",
    "license_expression",
    "license_files_declared",
    "metadata_warnings",
    "name",
    "source_archive",
    "source_provider",
    "version",
}


def _source_date_epoch() -> int:
    raw_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if raw_epoch is None:
        raise RuntimeError("SOURCE_DATE_EPOCH is required for the license inventory")
    try:
        epoch = int(raw_epoch)
    except ValueError as error:
        raise RuntimeError("SOURCE_DATE_EPOCH must be an integer") from error
    if epoch < 0:
        raise RuntimeError("SOURCE_DATE_EPOCH must not be negative")
    return epoch


def _timestamp() -> str:
    return datetime.fromtimestamp(_source_date_epoch(), timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(
    path: Path,
    *,
    label: str = "license inventory",
    maximum_size: int = _MAX_INVENTORY_INDEX_SIZE,
) -> dict:
    def no_duplicates(pairs):
        document = {}
        for key, value in pairs:
            if key in document:
                raise RuntimeError(f"duplicate JSON key in {label}: {key}")
            document[key] = value
        return document

    try:
        size = path.stat().st_size
        if not 0 < size <= maximum_size:
            raise RuntimeError(f"{label} JSON has an invalid size: {size}")
        content = path.read_bytes()
        if content.startswith(b"\xef\xbb\xbf"):
            raise RuntimeError(f"{label} JSON must be UTF-8 without a BOM")
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=no_duplicates,
        )
    except RuntimeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid {label} JSON: {path}") from error
    if not isinstance(document, dict):
        raise RuntimeError(f"{label} JSON must be an object")
    return document


def _safe_relative_path(raw_path: str) -> PurePosixPath:
    if not isinstance(raw_path, str):
        raise RuntimeError(f"unsafe distribution file path: {raw_path!r}")
    if (
        not raw_path
        or "\0" in raw_path
        or "\\" in raw_path
        or raw_path.startswith("/")
        or len(raw_path.encode("utf-8")) > _MAX_DISTRIBUTION_PATH_BYTES
    ):
        raise RuntimeError(f"unsafe distribution file path: {raw_path!r}")
    path = PurePosixPath(raw_path)
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts) or path.as_posix() != raw_path:
        raise RuntimeError(f"unsafe distribution file path: {raw_path!r}")
    return path


def _safe_record_path(raw_path: str) -> PurePosixPath:
    if not isinstance(raw_path, str):
        raise RuntimeError(f"unsafe installed distribution path: {raw_path!r}")
    if (
        not raw_path
        or "\0" in raw_path
        or "\\" in raw_path
        or raw_path.startswith("/")
        or len(raw_path.encode("utf-8")) > _MAX_DISTRIBUTION_PATH_BYTES
    ):
        raise RuntimeError(f"unsafe installed distribution path: {raw_path!r}")
    path = PurePosixPath(raw_path)
    if not path.parts or path.as_posix() != raw_path:
        raise RuntimeError(f"unsafe installed distribution path: {raw_path!r}")
    non_parent_seen = False
    for part in path.parts:
        if part in {"", "."} or ":" in part:
            raise RuntimeError(f"unsafe installed distribution path: {raw_path!r}")
        if part == "..":
            if non_parent_seen:
                raise RuntimeError(f"unsafe installed distribution path: {raw_path!r}")
        else:
            non_parent_seen = True
    if not non_parent_seen:
        raise RuntimeError(f"unsafe installed distribution path: {raw_path!r}")
    return path


def _safe_output_component(value: str) -> str:
    component = _SAFE_COMPONENT.sub("-", value).strip(".-")
    if not component or component in {".", ".."}:
        raise RuntimeError(f"unsafe license inventory component: {value!r}")
    return component


def _marker_environment(system: str | None = None) -> dict[str, str]:
    environment = default_environment()
    if system is None:
        return environment
    try:
        sys_platform, os_name = {
            "Linux": ("linux", "posix"),
            "Windows": ("win32", "nt"),
            "Darwin": ("darwin", "posix"),
        }[system]
    except KeyError as error:
        raise RuntimeError(f"unsupported inventory platform: {system!r}") from error
    environment.update(
        {
            "os_name": os_name,
            "platform_system": system,
            "sys_platform": sys_platform,
        }
    )
    return environment


def _read_lock(
    lock_path: Path,
    *,
    system: str | None = None,
) -> tuple[list[Requirement], list[Requirement]]:
    if lock_path.is_symlink() or not lock_path.is_file():
        raise RuntimeError("dependency lock must be a regular non-symlink file")
    lock_path = lock_path.resolve(strict=True)
    if not 0 < lock_path.stat().st_size <= _MAX_LOCK_SIZE:
        raise RuntimeError("dependency lock has an invalid size")
    try:
        text = lock_path.read_text(encoding="utf-8")
    except UnicodeError as error:
        raise RuntimeError("dependency lock must be UTF-8") from error
    if text.startswith("\ufeff"):
        raise RuntimeError("dependency lock must be UTF-8 without a BOM")
    all_requirements = []
    seen = set()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            requirement = Requirement(line)
        except Exception as error:
            raise RuntimeError(f"invalid dependency lock line {line_number}") from error
        specifiers = list(requirement.specifier)
        if (
            requirement.url is not None
            or requirement.extras
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise RuntimeError(f"dependency lock line {line_number} must be one exact version pin")
        name = canonicalize_name(requirement.name)
        if name in seen:
            raise RuntimeError(f"duplicate dependency lock entry: {name}")
        seen.add(name)
        all_requirements.append(requirement)
    if not all_requirements:
        raise RuntimeError("dependency lock contains no requirements")
    active = [
        requirement
        for requirement in all_requirements
        if requirement.marker is None
        or requirement.marker.evaluate(
            {**_marker_environment(system), "extra": ""},
        )
    ]
    if not active:
        raise RuntimeError("dependency lock has no active requirements")
    return all_requirements, active


def _validate_source_entry(source: dict, index: int) -> dict:
    if set(source) - _SOURCE_ENTRY_KEYS or not _SOURCE_ENTRY_REQUIRED_KEYS.issubset(source):
        raise RuntimeError(f"source lock entry {index} has unsupported or missing fields")
    scalar_fields = ("filename", "kind", "name", "sha256", "url", "version")
    if not all(isinstance(source.get(field), str) and source[field] for field in scalar_fields):
        raise RuntimeError(f"source lock entry {index} has invalid text fields")
    for optional in ("revision", "tag"):
        if optional in source and (not isinstance(source[optional], str) or not source[optional]):
            raise RuntimeError(f"source lock entry {index} has an invalid {optional}")
    filename = source["filename"]
    if PurePosixPath(filename).name != filename or filename in {".", ".."} or "\\" in filename or "\0" in filename:
        raise RuntimeError(f"source lock entry {index} has an unsafe filename")
    digest = source["sha256"]
    if _SHA256.fullmatch(digest) is None:
        raise RuntimeError(f"source lock entry {index} has an invalid SHA-256")
    size = source["size"]
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= _MAX_SOURCE_ARCHIVE_SIZE:
        raise RuntimeError(f"source lock entry {index} has an invalid size")
    parsed_url = urlsplit(source["url"])
    if (
        parsed_url.scheme != "https"
        or not parsed_url.netloc
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.fragment
    ):
        raise RuntimeError(f"source lock entry {index} has an unsafe URL")
    provides = source["provides"]
    if not isinstance(provides, list) or not provides or len(provides) > _MAX_PROVIDERS_PER_SOURCE:
        raise RuntimeError(f"source lock entry {index} has an invalid provider list")
    normalized = []
    for raw_provider in provides:
        if not isinstance(raw_provider, str) or not raw_provider:
            raise RuntimeError(f"source lock entry {index} has an invalid provider")
        provider = canonicalize_name(raw_provider)
        if raw_provider != provider or provider in normalized:
            raise RuntimeError(f"source lock entry {index} has a non-canonical or duplicate provider")
        normalized.append(provider)
    return source


def _source_providers(source_lock_path: Path) -> dict[str, dict]:
    if source_lock_path.is_symlink() or not source_lock_path.is_file():
        raise RuntimeError("source lock must be a regular non-symlink file")
    source_lock_path = source_lock_path.resolve(strict=True)
    if source_lock_path.name != "release-sources.json":
        raise RuntimeError("source lock must be named release-sources.json")
    document = _load_json(
        source_lock_path,
        label="release source lock",
        maximum_size=_MAX_SOURCE_LOCK_SIZE,
    )
    if document.get("schema") != _SOURCE_LOCK_SCHEMA or document.get("schema_version") != _SOURCE_LOCK_SCHEMA_VERSION:
        raise RuntimeError("release source lock schema is unsupported")
    sources = document.get("sources")
    if not isinstance(sources, list) or not sources or len(sources) > _MAX_SOURCE_ENTRIES:
        raise RuntimeError("release source lock sources must be a bounded non-empty array")
    providers = {}
    for index, raw_source in enumerate(sources, start=1):
        if not isinstance(raw_source, dict):
            raise RuntimeError(f"source lock entry {index} must be an object")
        source = _validate_source_entry(raw_source, index)
        for provider in source["provides"]:
            if provider in providers:
                raise RuntimeError(f"duplicate release source provider: {provider}")
            providers[provider] = source
    return providers


def _declared_license_files(distribution: metadata.Distribution) -> list[str]:
    declared = []
    for raw_path in distribution.metadata.get_all("License-File") or ():
        normalized = _safe_relative_path(raw_path.replace("\\", "/")).as_posix()
        if normalized not in declared:
            declared.append(normalized)
    return sorted(declared)


def _is_declared_match(path: str, declared: str) -> bool:
    lowered = path.lower()
    declared_lower = declared.lower()
    return (
        lowered == declared_lower
        or lowered.endswith(f"/{declared_lower}")
        or lowered.endswith(f"/licenses/{declared_lower}")
    )


def _candidate_license_paths(
    distribution: metadata.Distribution,
    declared: list[str],
) -> list[tuple[PurePosixPath, bool]]:
    available = []
    for package_path in distribution.files or ():
        raw_path = str(package_path).replace("\\", "/")
        leaf = PurePosixPath(raw_path).name
        lowered_path = raw_path.lower()
        declared_match = any(_is_declared_match(raw_path, declared_path) for declared_path in declared)
        in_distribution_metadata = ".dist-info/" in lowered_path
        if not (declared_match or (in_distribution_metadata and _LICENSE_LEAF.fullmatch(leaf))):
            continue
        relative = _safe_relative_path(raw_path)
        in_distribution_metadata = any(part.lower().endswith(".dist-info") for part in relative.parts)
        available.append((relative, in_distribution_metadata))
    candidates = {}
    for declared_path in declared:
        matches = [
            (relative, in_distribution_metadata)
            for relative, in_distribution_metadata in available
            if _is_declared_match(relative.as_posix(), declared_path)
        ]
        preferred = [item for item in matches if item[1]] or matches
        for relative, _ in preferred:
            candidates[relative.as_posix()] = (relative, True)
    for relative, in_distribution_metadata in available:
        if in_distribution_metadata and _LICENSE_LEAF.fullmatch(relative.name):
            rendered = relative.as_posix()
            candidates.setdefault(rendered, (relative, False))
    missing = [
        declared_path
        for declared_path in declared
        if not any(_is_declared_match(rendered, declared_path) for rendered in candidates)
    ]
    if missing:
        raise RuntimeError(
            "declared license files are absent from installed distribution "
            f"{distribution.metadata.get('Name')}: {missing}"
        )
    return [candidates[path] for path in sorted(candidates)]


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _stat_identity(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _path_identity(file_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _stable_file_digest(path: Path, *, maximum_size: int, label: str) -> tuple[str, int]:
    if _is_link(path):
        raise RuntimeError(f"{label} must not be a symlink or junction: {path}")
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise RuntimeError(f"{label} is not a regular file: {path}")
            if not 0 <= before.st_size <= maximum_size:
                raise RuntimeError(f"{label} has an invalid size ({before.st_size}): {path}")
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError(f"cannot read {label}: {path}") from error
    if _stat_identity(before) != _stat_identity(after) or _path_identity(after) != _path_identity(current):
        raise RuntimeError(f"{label} changed while it was being hashed: {path}")
    return digest.hexdigest(), after.st_size


def _resolve_installed_file(
    distribution: metadata.Distribution,
    package_path: PurePosixPath,
    installation_root: Path,
) -> tuple[Path, str]:
    try:
        located = Path(distribution.locate_file(package_path.as_posix()))
    except Exception as error:
        raise RuntimeError(f"cannot locate installed distribution file: {package_path}") from error
    if not located.is_absolute():
        located = Path.cwd().joinpath(located)
    located_absolute = Path(os.path.abspath(located))
    try:
        relative = located_absolute.relative_to(installation_root)
    except ValueError as error:
        raise RuntimeError(f"installed distribution file escapes sys.prefix: {package_path}") from error
    cursor = installation_root
    for part in relative.parts:
        cursor = cursor.joinpath(part)
        if _is_link(cursor):
            raise RuntimeError(f"installed distribution path contains a symlink or junction: {cursor}")
    try:
        located_resolved = located_absolute.resolve(strict=True)
    except OSError as error:
        raise RuntimeError(f"installed distribution file is missing: {package_path}") from error
    if installation_root not in (located_resolved, *located_resolved.parents):
        raise RuntimeError(f"installed distribution file resolves outside sys.prefix: {package_path}")
    if not located_resolved.is_file():
        raise RuntimeError(f"installed distribution file is not regular: {package_path}")
    manifest_path = PurePosixPath(*relative.parts).as_posix()
    if not manifest_path or len(manifest_path.encode("utf-8")) > _MAX_DISTRIBUTION_PATH_BYTES:
        raise RuntimeError(f"installed distribution manifest path is invalid: {package_path}")
    return located_resolved, manifest_path


def _installation_root(path: Path | None) -> Path:
    root = Path(sys.prefix) if path is None else path
    root = Path(os.path.abspath(root))
    if _is_link(root) or not root.is_dir():
        raise RuntimeError("installed distribution root must be a regular non-link directory")
    return root.resolve(strict=True)


def _installed_distribution_provenance(
    distribution: metadata.Distribution,
    installation_root: Path,
) -> dict:
    raw_files = distribution.files
    if raw_files is None:
        raise RuntimeError(f"installed distribution has no RECORD file list: {distribution.metadata.get('Name')}")
    files = list(raw_files)
    if not files or len(files) > _MAX_DISTRIBUTION_FILES:
        raise RuntimeError(
            "installed distribution file count is outside its bound: "
            f"{distribution.metadata.get('Name')} ({len(files)})"
        )
    manifest_entries = []
    found_paths = set()
    record_entries = []
    total_size = 0
    for raw_package_path in files:
        package_path = _safe_record_path(str(raw_package_path).replace("\\", "/"))
        located, manifest_path = _resolve_installed_file(
            distribution,
            package_path,
            installation_root,
        )
        if manifest_path in found_paths:
            continue
        found_paths.add(manifest_path)
        digest, size = _stable_file_digest(
            located,
            maximum_size=_MAX_INSTALLED_FILE_SIZE,
            label="installed distribution file",
        )
        total_size += size
        if total_size > _MAX_DISTRIBUTION_BYTES:
            raise RuntimeError("installed distribution exceeds its byte bound: " f"{distribution.metadata.get('Name')}")
        entry = (manifest_path, size, digest)
        manifest_entries.append(entry)
        if (
            package_path.name == "RECORD"
            and len(package_path.parts) >= 2
            and package_path.parts[-2].lower().endswith(".dist-info")
        ):
            record_entries.append(entry)
    if len(record_entries) != 1:
        raise RuntimeError(
            "installed distribution must contain exactly one dist-info/RECORD: "
            f"{distribution.metadata.get('Name')} ({len(record_entries)})"
        )
    record_path, record_size, record_digest = record_entries[0]
    if not 0 < record_size <= _MAX_RECORD_SIZE:
        raise RuntimeError(
            "installed distribution RECORD has an invalid size: " f"{distribution.metadata.get('Name')} ({record_size})"
        )
    manifest = hashlib.sha256()
    manifest.update(_MANIFEST_DOMAIN)
    for manifest_path, size, digest in sorted(manifest_entries):
        encoded_path = manifest_path.encode("utf-8")
        manifest.update(len(encoded_path).to_bytes(4, "big"))
        manifest.update(encoded_path)
        manifest.update(size.to_bytes(8, "big"))
        manifest.update(bytes.fromhex(digest))
    return {
        "files_manifest": {
            "algorithm": _MANIFEST_ALGORITHM,
            "file_count": len(manifest_entries),
            "sha256": manifest.hexdigest(),
            "total_size": total_size,
        },
        "record": {
            "path": record_path,
            "sha256": record_digest,
            "size": record_size,
        },
    }


def _installed_distribution(
    requirement: Requirement,
    installation_root: Path,
) -> tuple[metadata.Distribution, str, str, dict]:
    canonical_name = canonicalize_name(requirement.name)
    try:
        distribution = metadata.distribution(requirement.name)
    except metadata.PackageNotFoundError as error:
        raise RuntimeError(f"pinned distribution is not installed: {canonical_name}") from error
    installed_name = distribution.metadata.get("Name")
    if not isinstance(installed_name, str) or canonicalize_name(installed_name) != canonical_name:
        raise RuntimeError(f"installed distribution name mismatch: {canonical_name}")
    pinned_version = next(iter(requirement.specifier)).version
    if distribution.version != pinned_version:
        raise RuntimeError(
            f"installed {requirement.name} version {distribution.version} "
            f"does not match pinned version {pinned_version}"
        )
    provenance = _installed_distribution_provenance(distribution, installation_root)
    return distribution, installed_name, pinned_version, provenance


def _read_distribution_file(
    distribution: metadata.Distribution,
    package_path: PurePosixPath,
) -> bytes:
    base = Path(distribution.locate_file(""))
    located = Path(distribution.locate_file(package_path.as_posix()))
    base_absolute = Path(os.path.abspath(base))
    located_absolute = Path(os.path.abspath(located))
    try:
        relative = located_absolute.relative_to(base_absolute)
    except ValueError as error:
        raise RuntimeError(f"distribution file escapes its installation root: {package_path}") from error
    cursor = base_absolute
    if _is_link(cursor):
        raise RuntimeError(f"distribution root is a symlink or junction: {base_absolute}")
    for part in relative.parts:
        cursor = cursor.joinpath(part)
        if _is_link(cursor):
            raise RuntimeError(f"distribution license path contains a symlink or junction: {cursor}")
    base_resolved = base_absolute.resolve(strict=True)
    located_resolved = located_absolute.resolve(strict=True)
    if base_resolved not in (located_resolved, *located_resolved.parents):
        raise RuntimeError(f"distribution license resolves outside its installation root: {package_path}")
    digest, size = _stable_file_digest(
        located_resolved,
        maximum_size=_MAX_LICENSE_FILE_SIZE,
        label="distribution license",
    )
    if size == 0:
        raise RuntimeError(f"distribution license has an invalid size ({size}): {package_path}")
    content = located_resolved.read_bytes()
    if len(content) != size or _sha256_bytes(content) != digest:
        raise RuntimeError(f"distribution license changed after verification: {package_path}")
    return content


def _license_metadata(distribution: metadata.Distribution) -> dict:
    expression = (distribution.metadata.get("License-Expression") or "").strip()
    legacy = (distribution.metadata.get("License") or "").strip()
    classifiers = sorted(
        value for value in distribution.metadata.get_all("Classifier") or () if value.startswith("License ::")
    )
    if not expression and not legacy and not classifiers:
        raise RuntimeError("installed distribution has no license metadata: " f"{distribution.metadata.get('Name')}")
    return {
        "license_expression": expression or None,
        "license": legacy or None,
        "license_classifiers": classifiers,
    }


def _render_text(document: dict) -> str:
    lines = [
        "dupeGuru Neo portable third-party license inventory",
        f"Generated: {document['generated_at']}",
        f"Platform: {document['platform']['system']} / {document['platform']['machine']}",
        (f"Dependency lock: {document['lock']['path']} " f"(SHA-256 {document['lock']['sha256']})"),
        (f"Source lock: {document['source_lock']['path']} " f"(SHA-256 {document['source_lock']['sha256']})"),
        "",
    ]
    for package in document["packages"]:
        designation = package["license_expression"] or package["license"] or ", ".join(package["license_classifiers"])
        provenance = package["installed_provenance"]
        source = package["source_archive"]
        lines.append(f"{package['name']} {package['version']} — {designation}")
        lines.append(
            f"  Source provider: {package['source_provider']} -> " f"{source['filename']} (SHA-256 {source['sha256']})"
        )
        lines.append(
            f"  Installed RECORD: {provenance['record']['path']} " f"(SHA-256 {provenance['record']['sha256']})"
        )
        manifest = provenance["files_manifest"]
        lines.append(
            f"  Installed files: {manifest['file_count']} files, "
            f"{manifest['total_size']} bytes "
            f"({manifest['algorithm']} {manifest['sha256']})"
        )
        for warning in package["metadata_warnings"]:
            lines.append(f"  WARNING: {warning}")
        for license_file in package["files"]:
            lines.append(
                "  "
                f"{license_file['copied_path']} "
                f"(from {license_file['source_path']}, "
                f"SHA-256 {license_file['sha256']})"
            )
        lines.append("")
    if document["inactive_constraints"]:
        lines.append("Inactive platform constraints:")
        lines.extend(f"  {requirement}" for requirement in document["inactive_constraints"])
        lines.append("")
    return "\n".join(lines)


def _source_for_requirement(
    requirement: Requirement,
    providers: dict[str, dict],
) -> tuple[str, dict]:
    canonical_name = canonicalize_name(requirement.name)
    source = providers.get(canonical_name)
    if source is None:
        raise RuntimeError(f"release source lock is missing provider: {canonical_name}")
    pinned_version = next(iter(requirement.specifier)).version
    if source["version"] != pinned_version:
        raise RuntimeError(
            f"release source version mismatch for {canonical_name}: " f"{source['version']} != {pinned_version}"
        )
    return canonical_name, source


def generate_inventory(
    lock_path: Path,
    source_lock_path: Path,
    output_directory: Path,
    *,
    installation_root: Path | None = None,
) -> Path:
    if lock_path.is_symlink() or not lock_path.is_file():
        raise RuntimeError("dependency lock must be a regular non-symlink file")
    lock_path = lock_path.resolve(strict=True)
    if source_lock_path.is_symlink() or not source_lock_path.is_file():
        raise RuntimeError("source lock must be a regular non-symlink file")
    source_lock_path = source_lock_path.resolve(strict=True)
    all_requirements, active_requirements = _read_lock(lock_path)
    providers = _source_providers(source_lock_path)
    installed_root = _installation_root(installation_root)
    output_directory = output_directory.resolve()
    if output_directory.is_symlink():
        raise RuntimeError("license inventory output must not be a symlink")
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.",
            dir=output_directory.parent,
        )
    )
    try:
        package_documents = []
        total_license_size = 0
        total_installed_size = 0
        total_installed_files = 0
        for requirement in sorted(
            active_requirements,
            key=lambda item: canonicalize_name(item.name),
        ):
            canonical_name, source = _source_for_requirement(requirement, providers)
            distribution, installed_name, pinned_version, provenance = _installed_distribution(
                requirement,
                installed_root,
            )
            total_installed_files += provenance["files_manifest"]["file_count"]
            total_installed_size += provenance["files_manifest"]["total_size"]
            if (
                total_installed_files > _MAX_TOTAL_DISTRIBUTION_FILES
                or total_installed_size > _MAX_TOTAL_INSTALLED_BYTES
            ):
                raise RuntimeError("installed dependency inventory exceeds its global bound")
            metadata_document = _license_metadata(distribution)
            declared = _declared_license_files(distribution)
            candidates = _candidate_license_paths(distribution, declared)
            if not candidates:
                raise RuntimeError("installed distribution has no discoverable license text: " f"{requirement.name}")
            package_directory = Path("packages").joinpath(
                f"{_safe_output_component(canonical_name)}-" f"{_safe_output_component(pinned_version)}"
            )
            copied_files = []
            for index, (package_path, declared_match) in enumerate(candidates, start=1):
                content = _read_distribution_file(distribution, package_path)
                total_license_size += len(content)
                if total_license_size > _MAX_LICENSE_TOTAL_SIZE:
                    raise RuntimeError("dependency license inventory exceeds its size limit")
                leaf = _safe_output_component(package_path.name)
                copied_path = package_directory.joinpath(f"{index:02d}-{leaf}")
                destination = temporary_root.joinpath(copied_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                copied_files.append(
                    {
                        "copied_path": copied_path.as_posix(),
                        "declared_by_metadata": declared_match,
                        "sha256": _sha256_bytes(content),
                        "size": len(content),
                        "source_path": package_path.as_posix(),
                    }
                )
            warnings = []
            if metadata_document["license_expression"] is None:
                warnings.append("License-Expression metadata is absent")
            if not declared:
                warnings.append("License-File metadata is absent; dist-info text was discovered")
            package_documents.append(
                {
                    "canonical_name": canonical_name,
                    "files": copied_files,
                    "installed_provenance": provenance,
                    "license": metadata_document["license"],
                    "license_classifiers": metadata_document["license_classifiers"],
                    "license_expression": metadata_document["license_expression"],
                    "license_files_declared": declared,
                    "metadata_warnings": warnings,
                    "name": installed_name,
                    "source_archive": source,
                    "source_provider": canonical_name,
                    "version": pinned_version,
                }
            )
        active_names = {canonicalize_name(requirement.name) for requirement in active_requirements}
        document = {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "generated_at": _timestamp(),
            "lock": {
                "path": lock_path.name,
                "sha256": _sha256_file(lock_path),
            },
            "source_lock": {
                "path": source_lock_path.name,
                "sha256": _sha256_file(source_lock_path),
            },
            "platform": {
                "machine": platform.machine().lower(),
                "system": platform.system(),
            },
            "packages": package_documents,
            "inactive_constraints": sorted(
                str(requirement)
                for requirement in all_requirements
                if canonicalize_name(requirement.name) not in active_names
            ),
        }
        temporary_root.joinpath("index.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary_root.joinpath("index.txt").write_text(
            _render_text(document),
            encoding="utf-8",
            newline="\n",
        )
        if output_directory.exists():
            if not output_directory.is_dir() or output_directory.is_symlink():
                raise RuntimeError("license inventory output is not a safe directory")
            shutil.rmtree(output_directory)
        os.replace(temporary_root, output_directory)
    except BaseException:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise
    verify_inventory(
        output_directory,
        lock_path,
        source_lock_path,
        installation_root=installed_root,
    )
    return output_directory


def _validate_provenance_shape(provenance: object, canonical_name: str) -> None:
    if not isinstance(provenance, dict) or set(provenance) != {
        "files_manifest",
        "record",
    }:
        raise RuntimeError(f"invalid installed provenance: {canonical_name}")
    manifest = provenance.get("files_manifest")
    record = provenance.get("record")
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "algorithm",
            "file_count",
            "sha256",
            "total_size",
        }
        or manifest.get("algorithm") != _MANIFEST_ALGORITHM
        or isinstance(manifest.get("file_count"), bool)
        or not isinstance(manifest.get("file_count"), int)
        or not 0 < manifest["file_count"] <= _MAX_DISTRIBUTION_FILES
        or isinstance(manifest.get("total_size"), bool)
        or not isinstance(manifest.get("total_size"), int)
        or not 0 <= manifest["total_size"] <= _MAX_DISTRIBUTION_BYTES
        or not isinstance(manifest.get("sha256"), str)
        or _SHA256.fullmatch(manifest["sha256"]) is None
    ):
        raise RuntimeError(f"invalid installed files manifest: {canonical_name}")
    if (
        not isinstance(record, dict)
        or set(record) != {"path", "sha256", "size"}
        or not isinstance(record.get("path"), str)
        or not record["path"]
        or len(record["path"].encode("utf-8")) > _MAX_DISTRIBUTION_PATH_BYTES
        or not isinstance(record.get("sha256"), str)
        or _SHA256.fullmatch(record["sha256"]) is None
        or isinstance(record.get("size"), bool)
        or not isinstance(record.get("size"), int)
        or not 0 < record["size"] <= _MAX_RECORD_SIZE
    ):
        raise RuntimeError(f"invalid installed RECORD provenance: {canonical_name}")


def verify_inventory(
    inventory_directory: Path,
    lock_path: Path,
    source_lock_path: Path,
    *,
    expected_system: str | None = None,
    installation_root: Path | None = None,
) -> None:
    if inventory_directory.is_symlink() or not inventory_directory.is_dir():
        raise RuntimeError("license inventory must be a regular directory")
    if lock_path.is_symlink() or not lock_path.is_file():
        raise RuntimeError("dependency lock must be a regular non-symlink file")
    if source_lock_path.is_symlink() or not source_lock_path.is_file():
        raise RuntimeError("source lock must be a regular non-symlink file")
    inventory_directory = inventory_directory.resolve(strict=True)
    lock_path = lock_path.resolve(strict=True)
    source_lock_path = source_lock_path.resolve(strict=True)
    installed_root = _installation_root(installation_root)
    inventory_paths = [inventory_directory, *inventory_directory.rglob("*")]
    if len(inventory_paths) > _MAX_INVENTORY_FILES:
        raise RuntimeError("license inventory contains too many paths")
    for path in inventory_paths:
        if _is_link(path):
            raise RuntimeError(f"license inventory contains a symlink or junction: {path}")
        if path != inventory_directory and not (path.is_file() or path.is_dir()):
            raise RuntimeError(f"license inventory contains a special file: {path}")
    document = _load_json(inventory_directory.joinpath("index.json"))
    if set(document) != {
        "generated_at",
        "inactive_constraints",
        "lock",
        "packages",
        "platform",
        "schema",
        "schema_version",
        "source_lock",
    }:
        raise RuntimeError("license inventory has unsupported or missing top-level fields")
    if document.get("schema") != _SCHEMA or document.get("schema_version") != _SCHEMA_VERSION:
        raise RuntimeError("license inventory schema is unsupported")
    if document.get("generated_at") != _timestamp():
        raise RuntimeError("license inventory timestamp does not match SOURCE_DATE_EPOCH")
    lock = document.get("lock")
    if not isinstance(lock, dict) or set(lock) != {"path", "sha256"}:
        raise RuntimeError("license inventory dependency lock metadata is invalid")
    if lock.get("path") != lock_path.name:
        raise RuntimeError("license inventory names the wrong dependency lock")
    if lock.get("sha256") != _sha256_file(lock_path):
        raise RuntimeError("license inventory dependency-lock digest mismatch")
    source_lock = document.get("source_lock")
    if not isinstance(source_lock, dict) or set(source_lock) != {"path", "sha256"}:
        raise RuntimeError("license inventory source lock metadata is invalid")
    if source_lock.get("path") != source_lock_path.name:
        raise RuntimeError("license inventory names the wrong source lock")
    if source_lock.get("sha256") != _sha256_file(source_lock_path):
        raise RuntimeError("license inventory source-lock digest mismatch")
    providers = _source_providers(source_lock_path)
    platform_document = document.get("platform")
    if (
        not isinstance(platform_document, dict)
        or set(platform_document) != {"machine", "system"}
        or not isinstance(platform_document.get("machine"), str)
        or not platform_document["machine"]
        or not isinstance(platform_document.get("system"), str)
    ):
        raise RuntimeError("license inventory platform must be a complete object")
    system = platform_document["system"]
    _marker_environment(system)
    if expected_system is not None and system != expected_system:
        raise RuntimeError(f"license inventory platform mismatch: expected {expected_system}, found {system}")
    _, active_requirements = _read_lock(lock_path, system=system)
    requirements = {canonicalize_name(requirement.name): requirement for requirement in active_requirements}
    expected_versions = {name: next(iter(requirement.specifier)).version for name, requirement in requirements.items()}
    packages = document.get("packages")
    if not isinstance(packages, list) or not packages or len(packages) > _MAX_SOURCE_ENTRIES:
        raise RuntimeError("license inventory packages must be a bounded non-empty array")
    found_versions = {}
    expected_paths = {"index.json", "index.txt"}
    total_installed_files = 0
    total_installed_size = 0
    total_license_size = 0
    for package in packages:
        if not isinstance(package, dict) or set(package) != _PACKAGE_KEYS:
            raise RuntimeError("license inventory package entry has unsupported or missing fields")
        canonical_name = package.get("canonical_name")
        version = package.get("version")
        if not isinstance(canonical_name, str) or canonicalize_name(canonical_name) != canonical_name:
            raise RuntimeError("license inventory contains a non-canonical package name")
        if canonical_name in found_versions:
            raise RuntimeError(f"duplicate license inventory package: {canonical_name}")
        if canonical_name not in expected_versions:
            raise RuntimeError(f"unexpected license inventory package: {canonical_name}")
        if version != expected_versions[canonical_name]:
            raise RuntimeError(f"license inventory version mismatch: {canonical_name}")
        found_versions[canonical_name] = version
        expected_provider, expected_source = _source_for_requirement(
            requirements[canonical_name],
            providers,
        )
        if package.get("source_provider") != expected_provider:
            raise RuntimeError(f"license inventory source provider mismatch: {canonical_name}")
        if package.get("source_archive") != expected_source:
            raise RuntimeError(f"license inventory source archive mismatch: {canonical_name}")
        distribution, installed_name, _, installed_provenance = _installed_distribution(
            requirements[canonical_name],
            installed_root,
        )
        if package.get("name") != installed_name:
            raise RuntimeError(f"license inventory installed name mismatch: {canonical_name}")
        _validate_provenance_shape(package.get("installed_provenance"), canonical_name)
        if package["installed_provenance"] != installed_provenance:
            raise RuntimeError(f"installed distribution provenance mismatch: {canonical_name}")
        manifest = installed_provenance["files_manifest"]
        total_installed_files += manifest["file_count"]
        total_installed_size += manifest["total_size"]
        if total_installed_files > _MAX_TOTAL_DISTRIBUTION_FILES or total_installed_size > _MAX_TOTAL_INSTALLED_BYTES:
            raise RuntimeError("installed dependency inventory exceeds its global bound")
        if canonicalize_name(distribution.metadata["Name"]) != canonical_name:
            raise RuntimeError(f"installed distribution identity mismatch: {canonical_name}")
        metadata_document = _license_metadata(distribution)
        for field in ("license", "license_classifiers", "license_expression"):
            if package.get(field) != metadata_document[field]:
                raise RuntimeError(f"installed license metadata mismatch: {canonical_name}")
        declared = _declared_license_files(distribution)
        if package.get("license_files_declared") != declared:
            raise RuntimeError(f"declared license-file metadata mismatch: {canonical_name}")
        warnings = package.get("metadata_warnings")
        if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
            raise RuntimeError(f"invalid license metadata warnings: {canonical_name}")
        expected_warnings = []
        if package.get("license_expression") is None:
            expected_warnings.append("License-Expression metadata is absent")
        if not declared:
            expected_warnings.append("License-File metadata is absent; dist-info text was discovered")
        if warnings != expected_warnings:
            raise RuntimeError(f"license metadata warning mismatch: {canonical_name}")
        files = package.get("files")
        if not isinstance(files, list) or not files:
            raise RuntimeError(f"license texts are absent: {canonical_name}")
        expected_license_files = []
        package_directory = Path("packages").joinpath(
            f"{_safe_output_component(canonical_name)}-" f"{_safe_output_component(expected_versions[canonical_name])}"
        )
        candidates = _candidate_license_paths(distribution, declared)
        if not candidates:
            raise RuntimeError(f"installed license texts are absent: {canonical_name}")
        for index, (package_path, declared_match) in enumerate(candidates, start=1):
            content = _read_distribution_file(distribution, package_path)
            total_license_size += len(content)
            if total_license_size > _MAX_LICENSE_TOTAL_SIZE:
                raise RuntimeError("dependency license inventory exceeds its size limit")
            copied_path = package_directory.joinpath(f"{index:02d}-{_safe_output_component(package_path.name)}")
            expected_license_files.append(
                {
                    "copied_path": copied_path.as_posix(),
                    "declared_by_metadata": declared_match,
                    "sha256": _sha256_bytes(content),
                    "size": len(content),
                    "source_path": package_path.as_posix(),
                }
            )
        if files != expected_license_files:
            raise RuntimeError(f"installed license-file evidence mismatch: {canonical_name}")
        for file_document in files:
            if not isinstance(file_document, dict) or set(file_document) != {
                "copied_path",
                "declared_by_metadata",
                "sha256",
                "size",
                "source_path",
            }:
                raise RuntimeError("license inventory file entry is invalid")
            copied_path = _safe_relative_path(file_document.get("copied_path", ""))
            _safe_relative_path(file_document.get("source_path", ""))
            if not isinstance(file_document.get("declared_by_metadata"), bool):
                raise RuntimeError(f"license declaration flag is invalid: {canonical_name}")
            rendered_path = copied_path.as_posix()
            if not rendered_path.startswith("packages/"):
                raise RuntimeError(f"license text is outside packages/: {rendered_path}")
            if rendered_path in expected_paths:
                raise RuntimeError(f"duplicate license inventory path: {rendered_path}")
            expected_paths.add(rendered_path)
            path = inventory_directory.joinpath(*copied_path.parts)
            if _is_link(path) or not path.is_file():
                raise RuntimeError(f"license inventory file is missing: {rendered_path}")
            size = file_document.get("size")
            digest = file_document.get("sha256")
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or not 0 < size <= _MAX_LICENSE_FILE_SIZE
                or not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None
            ):
                raise RuntimeError(f"license inventory file metadata is invalid: {rendered_path}")
            if path.stat().st_size != size:
                raise RuntimeError(f"license inventory size mismatch: {rendered_path}")
            if _sha256_file(path) != digest:
                raise RuntimeError(f"license inventory digest mismatch: {rendered_path}")
    if found_versions != expected_versions:
        missing = sorted(set(expected_versions) - set(found_versions))
        raise RuntimeError(f"license inventory is missing pinned packages: {missing}")
    active_names = set(expected_versions)
    all_requirements, _ = _read_lock(lock_path, system=system)
    expected_inactive = sorted(
        str(requirement) for requirement in all_requirements if canonicalize_name(requirement.name) not in active_names
    )
    if document.get("inactive_constraints") != expected_inactive:
        raise RuntimeError("license inventory inactive constraints do not match the lock")
    actual_paths = {
        path.relative_to(inventory_directory).as_posix() for path in inventory_directory.rglob("*") if path.is_file()
    }
    if actual_paths != expected_paths:
        raise RuntimeError(
            "license inventory file set mismatch; missing={} unexpected={}".format(
                sorted(expected_paths - actual_paths),
                sorted(actual_paths - expected_paths),
            )
        )
    index_text = inventory_directory.joinpath("index.txt")
    if not 0 < index_text.stat().st_size <= _MAX_INVENTORY_INDEX_SIZE:
        raise RuntimeError("human-readable license inventory has an invalid size")
    expected_text = _render_text(document)
    if index_text.read_text(encoding="utf-8") != expected_text:
        raise RuntimeError("human-readable license inventory does not match index.json")


def _parser() -> ArgumentParser:
    parser = ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--lock", type=Path, required=True)
    generate.add_argument("--source-lock", type=Path, required=True)
    generate.add_argument("--output-directory", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--lock", type=Path, required=True)
    verify.add_argument("--source-lock", type=Path, required=True)
    verify.add_argument("--directory", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "generate":
        print(generate_inventory(args.lock, args.source_lock, args.output_directory))
    elif args.command == "verify":
        verify_inventory(args.directory, args.lock, args.source_lock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
