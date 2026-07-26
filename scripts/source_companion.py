#!/usr/bin/env python3

"""Build and verify an experimental local portable source set.

The official release policy forbids publishing this output until every native
component in the frozen applications has a proven source and license mapping.
"""

from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit
import zipfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

if __package__:
    from .portable_bundle import verify_portable_archive
    from .release_metadata import verify_corresponding_source
else:
    from portable_bundle import verify_portable_archive
    from release_metadata import verify_corresponding_source


_SOURCE_LOCK_SCHEMA = "dupeguru.release-source-lock"
_SOURCE_LOCK_SCHEMA_VERSION = 1
_MANIFEST_SCHEMA = "dupeguru.source-companion-manifest"
_MANIFEST_SCHEMA_VERSION = 1
_PROOF_SCHEMA = "dupeguru.source-companion-proof"
_PROOF_SCHEMA_VERSION = 1
_SAFE_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_SAFE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_PORTABLE_ARCHIVE = re.compile(
    r"^dupeguru-neo-(?P<version>[0-9A-Za-z][0-9A-Za-z._+-]*)-"
    r"(?P<platform>windows|macos|linux)-(?P<architecture>[0-9a-z_]+)-"
    r"unsigned-portable(?P<extension>\.zip|\.tar\.gz)$"
)
_ALLOWED_SOURCE_HOSTS = {
    "codeload.github.com",
    "download.qt.io",
    "files.pythonhosted.org",
    "www.python.org",
}
_SOURCE_KINDS = {
    "git-commit-archive",
    "pypi-sdist",
    "python-official-source",
    "qt-official-source",
}
_EXTRA_PROVIDERS = {
    "cpython-runtime",
    "pyinstaller-bootloader",
}
_MAX_SOURCE_COUNT = 64
_MAX_COMPANION_SIZE = 2 * 1024 * 1024 * 1024 - 1
_MAX_MANIFEST_SIZE = 8 * 1024 * 1024
_MAX_INVENTORY_FILE_SIZE = 16 * 1024 * 1024
_MAX_INVENTORY_TOTAL_SIZE = 64 * 1024 * 1024


@dataclass(frozen=True)
class SourceRecord:
    filename: str
    kind: str
    name: str
    provides: tuple[str, ...]
    sha256: str
    size: int
    url: str
    version: str
    revision: str | None = None
    tag: str | None = None

    def manifest_entry(self) -> dict:
        entry = {
            "companion_path": f"upstream/{self.filename}",
            "filename": self.filename,
            "kind": self.kind,
            "name": self.name,
            "provides": list(self.provides),
            "sha256": self.sha256,
            "size": self.size,
            "url": self.url,
            "version": self.version,
        }
        if self.revision is not None:
            entry["revision"] = self.revision
        if self.tag is not None:
            entry["tag"] = self.tag
        return entry


def _source_date_epoch() -> int:
    raw_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if raw_epoch is None:
        raise RuntimeError("SOURCE_DATE_EPOCH is required for the source companion")
    try:
        epoch = int(raw_epoch)
    except ValueError as error:
        raise RuntimeError("SOURCE_DATE_EPOCH must be an integer") from error
    if not 0 <= epoch <= 0x7FFFFFFF:
        raise RuntimeError("SOURCE_DATE_EPOCH is outside the supported tar range")
    return epoch


def _load_json(path: Path, label: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} must be a regular non-symlink file")

    def reject_duplicate_keys(pairs):
        document = {}
        for key, value in pairs:
            if key in document:
                raise RuntimeError(f"duplicate JSON key in {label}: {key}")
            document[key] = value
        return document

    try:
        text = path.read_text(encoding="utf-8")
        if text.startswith("\ufeff"):
            raise RuntimeError(f"{label} must be UTF-8 without a BOM")
        document = json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except RuntimeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid {label}: {path}") from error
    if not isinstance(document, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return document


def _json_bytes(document: dict) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(raw_path: str) -> PurePosixPath:
    if not raw_path or "\0" in raw_path or "\\" in raw_path or raw_path.startswith("/"):
        raise RuntimeError(f"unsafe source-companion path: {raw_path!r}")
    path = PurePosixPath(raw_path)
    if not path.parts or path.as_posix() != raw_path or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(f"unsafe source-companion path: {raw_path!r}")
    return path


def _safe_artifact_name(name: str, label: str) -> None:
    if _SAFE_ARTIFACT_NAME.fullmatch(name) is None or Path(name).name != name or ".." in PurePosixPath(name).parts:
        raise RuntimeError(f"unsafe {label}: {name!r}")


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} must be a regular non-symlink file")
    return path.resolve(strict=True)


def _exact_requirements(lock_path: Path) -> dict[str, tuple[str, str]]:
    lock_path = _regular_file(lock_path, "dependency lock")
    if lock_path.name != "requirements-release.txt":
        raise RuntimeError("dependency lock must be named requirements-release.txt")
    try:
        text = lock_path.read_text(encoding="utf-8")
    except UnicodeError as error:
        raise RuntimeError("dependency lock must be UTF-8") from error
    if text.startswith("\ufeff"):
        raise RuntimeError("dependency lock must be UTF-8 without a BOM")
    requirements = {}
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
            raise RuntimeError(f"dependency lock line {line_number} must contain one exact version pin")
        name = canonicalize_name(requirement.name)
        if name in requirements:
            raise RuntimeError(f"duplicate dependency lock entry: {name}")
        requirements[name] = (requirement.name, specifiers[0].version)
    if not requirements:
        raise RuntimeError("dependency lock contains no requirements")
    return requirements


def _validate_source_url(raw_url: str) -> None:
    try:
        url = urlsplit(raw_url)
        port = url.port
    except ValueError as error:
        raise RuntimeError(f"invalid upstream source URL: {raw_url!r}") from error
    if (
        url.scheme != "https"
        or url.hostname not in _ALLOWED_SOURCE_HOSTS
        or url.username is not None
        or url.password is not None
        or port is not None
        or not url.path.startswith("/")
        or url.query
        or url.fragment
    ):
        raise RuntimeError(f"unapproved upstream source URL: {raw_url!r}")


def validate_source_lock(
    source_lock_path: Path,
    dependency_lock_path: Path,
) -> tuple[dict, tuple[SourceRecord, ...]]:
    source_lock_path = _regular_file(source_lock_path, "upstream source lock")
    if source_lock_path.name != "release-sources.json":
        raise RuntimeError("upstream source lock must be named release-sources.json")
    dependency_requirements = _exact_requirements(dependency_lock_path)
    document = _load_json(source_lock_path, "upstream source lock")
    if set(document) != {
        "portable_builder",
        "portable_python_version",
        "schema",
        "schema_version",
        "sources",
    }:
        raise RuntimeError("upstream source lock has unexpected or missing top-level fields")
    if (
        document["schema"] != _SOURCE_LOCK_SCHEMA
        or type(document["schema_version"]) is not int
        or document["schema_version"] != _SOURCE_LOCK_SCHEMA_VERSION
    ):
        raise RuntimeError("unsupported upstream source lock schema")
    python_version = document["portable_python_version"]
    if not isinstance(python_version, str) or _SAFE_VERSION.fullmatch(python_version) is None:
        raise RuntimeError("invalid portable Python version in upstream source lock")
    builder = document["portable_builder"]
    if (
        not isinstance(builder, dict)
        or set(builder) != {"name", "version"}
        or builder["name"] != "PyInstaller"
        or not isinstance(builder["version"], str)
        or _SAFE_VERSION.fullmatch(builder["version"]) is None
    ):
        raise RuntimeError("invalid portable builder in upstream source lock")
    raw_sources = document["sources"]
    if not isinstance(raw_sources, list) or not 0 < len(raw_sources) <= _MAX_SOURCE_COUNT:
        raise RuntimeError("upstream source lock has an invalid source count")

    records = []
    filenames = set()
    casefolded_filenames = set()
    providers = {}
    for index, raw_source in enumerate(raw_sources):
        if not isinstance(raw_source, dict):
            raise RuntimeError(f"upstream source entry {index} must be an object")
        required_keys = {
            "filename",
            "kind",
            "name",
            "provides",
            "sha256",
            "size",
            "url",
            "version",
        }
        optional_keys = {"revision", "tag"}
        if not required_keys <= set(raw_source) or set(raw_source) - required_keys - optional_keys:
            raise RuntimeError(f"upstream source entry {index} has unexpected or missing fields")
        filename = raw_source["filename"]
        if not isinstance(filename, str):
            raise RuntimeError(f"upstream source entry {index} has an invalid filename")
        _safe_artifact_name(filename, "upstream source filename")
        if filename in filenames or filename.casefold() in casefolded_filenames:
            raise RuntimeError(f"duplicate upstream source filename: {filename}")
        filenames.add(filename)
        casefolded_filenames.add(filename.casefold())
        kind = raw_source["kind"]
        if kind not in _SOURCE_KINDS:
            raise RuntimeError(f"unsupported upstream source kind: {kind!r}")
        name = raw_source["name"]
        version = raw_source["version"]
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(version, str)
            or _SAFE_VERSION.fullmatch(version) is None
        ):
            raise RuntimeError(f"upstream source entry {index} has invalid identity fields")
        raw_provides = raw_source["provides"]
        if not isinstance(raw_provides, list) or not raw_provides:
            raise RuntimeError(f"upstream source entry {index} provides no component")
        normalized_provides = []
        for raw_provider in raw_provides:
            if not isinstance(raw_provider, str) or not raw_provider:
                raise RuntimeError(f"upstream source entry {index} has an invalid provider")
            provider = canonicalize_name(raw_provider)
            if provider in normalized_provides:
                raise RuntimeError(f"duplicate provider in upstream source entry {index}: {provider}")
            if provider in providers:
                raise RuntimeError(f"upstream source provider appears more than once: {provider}")
            normalized_provides.append(provider)
            providers[provider] = (filename, version)
        digest = raw_source["sha256"]
        size = raw_source["size"]
        url = raw_source["url"]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise RuntimeError(f"upstream source entry {index} has an invalid SHA-256")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size < _MAX_COMPANION_SIZE:
            raise RuntimeError(f"upstream source entry {index} has an invalid size")
        if not isinstance(url, str):
            raise RuntimeError(f"upstream source entry {index} has an invalid URL")
        _validate_source_url(url)
        revision = raw_source.get("revision")
        if revision is not None and (not isinstance(revision, str) or _COMMIT.fullmatch(revision) is None):
            raise RuntimeError(f"upstream source entry {index} has an invalid revision")
        tag = raw_source.get("tag")
        if tag is not None and (not isinstance(tag, str) or _SAFE_VERSION.fullmatch(tag) is None or len(tag) > 128):
            raise RuntimeError(f"upstream source entry {index} has an invalid tag")
        if kind == "git-commit-archive" and (revision is None or tag is None):
            raise RuntimeError("git-commit source entries require both revision and tag")
        if kind != "git-commit-archive" and (revision is not None or tag is not None):
            raise RuntimeError("only git-commit source entries may declare revision or tag")
        records.append(
            SourceRecord(
                filename=filename,
                kind=kind,
                name=name,
                provides=tuple(normalized_provides),
                sha256=digest,
                size=size,
                url=url,
                version=version,
                revision=revision,
                tag=tag,
            )
        )

    required_providers = set(dependency_requirements) | _EXTRA_PROVIDERS
    if set(providers) != required_providers:
        raise RuntimeError(
            "upstream source coverage mismatch; missing={} unexpected={}".format(
                sorted(required_providers - set(providers)),
                sorted(set(providers) - required_providers),
            )
        )
    for provider, (_, required_version) in dependency_requirements.items():
        filename, source_version = providers[provider]
        if source_version != required_version:
            raise RuntimeError(
                f"upstream source version mismatch for {provider}: "
                f"{filename} has {source_version}, lock requires {required_version}"
            )
    if providers["cpython-runtime"][1] != python_version:
        raise RuntimeError("CPython source version does not match portable_python_version")
    if providers["pyinstaller-bootloader"][1] != builder["version"]:
        raise RuntimeError("PyInstaller source version does not match portable_builder")
    return document, tuple(records)


def _verify_source_file(path: Path, source: SourceRecord) -> None:
    path = _regular_file(path, f"upstream source {source.filename}")
    size = path.stat().st_size
    if size != source.size:
        raise RuntimeError(
            f"upstream source size mismatch for {source.filename}: " f"expected {source.size}, found {size}"
        )
    digest = _sha256_file(path)
    if digest != source.sha256:
        raise RuntimeError(
            f"upstream source digest mismatch for {source.filename}: " f"expected {source.sha256}, found {digest}"
        )


def _curl_executable() -> str:
    candidates = ("curl.exe", "curl") if sys.platform == "win32" else ("curl",)
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable is not None:
            return executable
    raise RuntimeError("curl is required to fetch pinned upstream source archives")


def _run_curl(url: str, output_path: Path) -> None:
    command = [
        _curl_executable(),
        "--fail",
        "--location",
        "--show-error",
        "--silent",
        "--continue-at",
        "-",
        "--retry",
        "5",
        "--retry-all-errors",
        "--connect-timeout",
        "30",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--tlsv1.2",
        "--output",
        str(output_path),
        url,
    ]
    result = subprocess.run(command, check=False, timeout=7200)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed with exit code {result.returncode}: {url}")


def fetch_sources(
    source_lock_path: Path,
    dependency_lock_path: Path,
    output_directory: Path,
) -> tuple[Path, ...]:
    _, sources = validate_source_lock(source_lock_path, dependency_lock_path)
    if output_directory.is_symlink():
        raise RuntimeError("upstream source directory must not be a symlink")
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    expected_names = {source.filename for source in sources}
    for path in output_directory.iterdir():
        if path.name not in expected_names:
            raise RuntimeError(f"unexpected file in upstream source directory: {path.name}")
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"upstream source path is not a regular file: {path.name}")
    fetched = []
    for source in sources:
        destination = output_directory.joinpath(source.filename)
        if destination.exists():
            current_size = destination.stat().st_size
            if current_size > source.size:
                raise RuntimeError(f"upstream source is larger than pinned size: {source.filename}")
            if current_size == source.size:
                _verify_source_file(destination, source)
                fetched.append(destination)
                continue
        _run_curl(source.url, destination)
        _verify_source_file(destination, source)
        fetched.append(destination)
    return tuple(fetched)


def _portable_data_root(platform_name: str) -> str:
    if platform_name == "macos":
        return "dupeguru-neo.app/Contents/Resources"
    return "dupeguru-neo/_internal"


def _portable_archives(
    portable_directory: Path,
    *,
    version: str,
    dependency_lock_path: Path,
) -> list[tuple[Path, str, str]]:
    if portable_directory.is_symlink() or not portable_directory.is_dir():
        raise RuntimeError("portable input directory must be a regular directory")
    portable_directory = portable_directory.resolve(strict=True)
    archives = []
    for path in portable_directory.iterdir():
        match = _PORTABLE_ARCHIVE.fullmatch(path.name)
        if match is None:
            raise RuntimeError(f"unexpected file in portable input directory: {path.name}")
        if match.group("version") != version:
            raise RuntimeError("portable source input release version mismatch")
        path = _regular_file(path, "portable archive")
        verify_portable_archive(path, dependency_lock_path)
        archives.append(
            (
                path,
                match.group("platform"),
                match.group("architecture"),
            )
        )
    identities = {(platform_name, architecture) for _, platform_name, architecture in archives}
    expected_identities = {
        ("linux", "x86_64"),
        ("macos", "arm64"),
        ("windows", "x86_64"),
    }
    if len(archives) != 3 or identities != expected_identities:
        raise RuntimeError("portable source input must contain exactly one archive for every release target")
    return sorted(archives, key=lambda item: item[1])


def _read_inventory_member(stream, size: int, name: str) -> bytes:
    if not 0 < size <= _MAX_INVENTORY_FILE_SIZE:
        raise RuntimeError(f"portable license inventory member has an invalid size: {name}")
    content = stream.read(size + 1)
    if len(content) != size:
        raise RuntimeError(f"portable license inventory member is truncated: {name}")
    return content


def _extract_license_inventory(
    archive_path: Path,
    platform_name: str,
) -> dict[str, bytes]:
    data_root = _portable_data_root(platform_name)
    prefixes = {
        f"{data_root}/FROZEN-RUNTIME-LICENSES/": "frozen-runtime",
        f"{data_root}/THIRD-PARTY-LICENSES/": "third-party",
    }
    files = {}
    total_size = 0

    def accept(name: str, size: int, stream) -> None:
        nonlocal total_size
        matched = [(prefix, category) for prefix, category in prefixes.items() if name.startswith(prefix)]
        if not matched:
            return
        prefix, category = matched[0]
        relative = _safe_relative_path(name.removeprefix(prefix)).as_posix()
        companion_path = f"{category}/{relative}"
        if companion_path in files:
            raise RuntimeError(f"duplicate portable license inventory member: {name}")
        content = _read_inventory_member(stream, size, name)
        total_size += len(content)
        if total_size > _MAX_INVENTORY_TOTAL_SIZE:
            raise RuntimeError("portable license inventory exceeds its size limit")
        files[companion_path] = content

    if archive_path.name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                name = info.filename.rstrip("/")
                if info.is_dir() or not any(name.startswith(prefix) for prefix in prefixes):
                    continue
                with archive.open(info) as stream:
                    accept(name, info.file_size, stream)
    else:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive:
                name = member.name.rstrip("/")
                if not any(name.startswith(prefix) for prefix in prefixes):
                    continue
                if not member.isfile():
                    raise RuntimeError(f"portable license inventory member is not a regular file: {name}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"cannot read portable license inventory member: {name}")
                with stream:
                    accept(name, member.size, stream)
    for category in ("frozen-runtime", "third-party"):
        required_indexes = {
            f"{category}/index.json",
            f"{category}/index.txt",
        }
        if not required_indexes <= set(files):
            raise RuntimeError(f"portable {platform_name} {category} license inventory " "is missing its indexes")
    return files


def _read_static_file(path: Path, label: str) -> bytes:
    path = _regular_file(path, label)
    size = path.stat().st_size
    if not 0 < size <= _MAX_INVENTORY_FILE_SIZE:
        raise RuntimeError(f"{label} has an invalid size")
    return path.read_bytes()


def _payload_entry(path: str, kind: str, *, content: bytes | None = None, source: Path | None = None) -> dict:
    _safe_relative_path(path)
    if (content is None) == (source is None):
        raise RuntimeError("source-companion payload must have exactly one data source")
    if content is not None:
        size = len(content)
        digest = _sha256_bytes(content)
    else:
        assert source is not None
        source = _regular_file(source, f"source-companion payload {path}")
        size = source.stat().st_size
        digest = _sha256_file(source)
    return {
        "content": content,
        "kind": kind,
        "path": path,
        "sha256": digest,
        "size": size,
        "source": source,
    }


def _public_payload_entry(entry: dict) -> dict:
    return {
        "kind": entry["kind"],
        "path": entry["path"],
        "sha256": entry["sha256"],
        "size": entry["size"],
    }


def _portable_metadata(
    portable_directory: Path,
    *,
    version: str,
    dependency_lock_path: Path,
) -> tuple[list[dict], list[dict]]:
    payload_entries = []
    portable_entries = []
    for archive_path, platform_name, architecture in _portable_archives(
        portable_directory,
        version=version,
        dependency_lock_path=dependency_lock_path,
    ):
        inventory = _extract_license_inventory(archive_path, platform_name)
        inventory_prefix = f"license-inventories/{platform_name}-{architecture}"
        portable_entries.append(
            {
                "architecture": architecture,
                "inventory_prefix": inventory_prefix,
                "name": archive_path.name,
                "platform": platform_name,
                "sha256": _sha256_file(archive_path),
                "size": archive_path.stat().st_size,
            }
        )
        for relative_name, content in sorted(inventory.items()):
            payload_entries.append(
                _payload_entry(
                    f"{inventory_prefix}/{relative_name}",
                    "portable-license-inventory",
                    content=content,
                )
            )
    return portable_entries, payload_entries


def _validate_version_and_commit(version: str, commit: str) -> None:
    if _SAFE_VERSION.fullmatch(version) is None:
        raise RuntimeError(f"unsafe source-companion version: {version!r}")
    if _COMMIT.fullmatch(commit) is None:
        raise RuntimeError(f"invalid source-companion commit: {commit!r}")


def _expected_companion_name(version: str) -> str:
    return f"dupeguru-neo-{version}-source-companion.tar"


def _archive_directories(root_name: str, file_paths: list[str]) -> list[str]:
    directories = {root_name}
    for raw_path in file_paths:
        parts = _safe_relative_path(raw_path).parts
        for length in range(1, len(parts)):
            directories.add(f"{root_name}/" + PurePosixPath(*parts[:length]).as_posix())
    return sorted(directories, key=lambda name: (name.count("/"), name))


def _tar_member(name: str, *, size: int = 0, directory: bool = False) -> tarfile.TarInfo:
    member = tarfile.TarInfo(f"{name}/" if directory else name)
    member.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    member.mode = 0o755 if directory else 0o644
    member.uid = 0
    member.gid = 0
    member.uname = ""
    member.gname = ""
    member.mtime = _source_date_epoch()
    member.size = 0 if directory else size
    member.pax_headers = {}
    return member


def build_source_companion(
    *,
    version: str,
    commit: str,
    source_lock_path: Path,
    dependency_lock_path: Path,
    source_directory: Path,
    application_source_path: Path,
    portable_directory: Path,
    output_path: Path,
    proof_path: Path,
    recipe_path: Path = Path("docs/SOURCE-COMPANION.md"),
    notice_path: Path = Path("THIRD_PARTY_NOTICES.md"),
    license_path: Path = Path("LICENSE"),
    hscommon_license_path: Path = Path("hscommon/LICENSE"),
) -> tuple[Path, Path]:
    _validate_version_and_commit(version, commit)
    epoch = _source_date_epoch()
    source_document, sources = validate_source_lock(
        source_lock_path,
        dependency_lock_path,
    )
    if platform.python_version() != source_document["portable_python_version"]:
        raise RuntimeError(
            "source companion must be built with CPython "
            f"{source_document['portable_python_version']}, found {platform.python_version()}"
        )
    if source_directory.is_symlink() or not source_directory.is_dir():
        raise RuntimeError("upstream source directory must be a regular directory")
    source_directory = source_directory.resolve(strict=True)
    expected_source_names = {source.filename for source in sources}
    found_source_names = {path.name for path in source_directory.iterdir()}
    if found_source_names != expected_source_names:
        raise RuntimeError(
            "upstream source directory mismatch; missing={} unexpected={}".format(
                sorted(expected_source_names - found_source_names),
                sorted(found_source_names - expected_source_names),
            )
        )
    for source in sources:
        _verify_source_file(source_directory.joinpath(source.filename), source)

    application_source_path = _regular_file(
        application_source_path,
        "dupeGuru Neo application source archive",
    )
    expected_application_name = f"dupeguru-neo-{version}-source.tar.gz"
    if application_source_path.name != expected_application_name:
        raise RuntimeError(f"application source archive must be named {expected_application_name}")
    verify_corresponding_source(
        application_source_path,
        commit=commit,
        version=version,
    )

    expected_output_name = _expected_companion_name(version)
    _safe_artifact_name(output_path.name, "source-companion archive name")
    if output_path.name != expected_output_name:
        raise RuntimeError(f"source companion must be named {expected_output_name}")
    if output_path.is_symlink() or output_path.exists():
        raise FileExistsError(f"refusing to overwrite source companion: {output_path}")
    _safe_artifact_name(proof_path.name, "source-companion proof name")
    if proof_path.suffix != ".json":
        raise RuntimeError("source-companion proof must be a JSON file")
    if proof_path.is_symlink() or proof_path.exists():
        raise FileExistsError(f"refusing to overwrite source-companion proof: {proof_path}")
    output_path = output_path.resolve()
    proof_path = proof_path.resolve()
    if output_path == proof_path:
        raise RuntimeError("source companion and proof paths must differ")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    proof_path.parent.mkdir(parents=True, exist_ok=True)

    dependency_lock_path = _regular_file(dependency_lock_path, "dependency lock")
    source_lock_path = _regular_file(source_lock_path, "upstream source lock")
    dependency_lock_content = dependency_lock_path.read_bytes()
    source_lock_content = source_lock_path.read_bytes()
    payload = [
        _payload_entry(
            "BUILD-RECIPE.md",
            "build-recipe",
            content=_read_static_file(recipe_path, "source-companion build recipe"),
        ),
        _payload_entry(
            "HSCOMMON-BSD-3-CLAUSE.txt",
            "license",
            content=_read_static_file(hscommon_license_path, "hscommon license"),
        ),
        _payload_entry(
            "LICENSE",
            "license",
            content=_read_static_file(license_path, "dupeGuru Neo license"),
        ),
        _payload_entry(
            "THIRD_PARTY_NOTICES.md",
            "license-notice",
            content=_read_static_file(notice_path, "third-party notices"),
        ),
        _payload_entry(
            "release-sources.json",
            "source-lock",
            content=source_lock_content,
        ),
        _payload_entry(
            "requirements-release.txt",
            "dependency-lock",
            content=dependency_lock_content,
        ),
        _payload_entry(
            f"application/{application_source_path.name}",
            "application-source",
            source=application_source_path,
        ),
    ]
    for source in sources:
        payload.append(
            _payload_entry(
                f"upstream/{source.filename}",
                "upstream-source",
                source=source_directory.joinpath(source.filename),
            )
        )
    portable_entries, portable_payload = _portable_metadata(
        portable_directory,
        version=version,
        dependency_lock_path=dependency_lock_path,
    )
    payload.extend(portable_payload)
    paths = [entry["path"] for entry in payload]
    if len(paths) != len(set(paths)) or len(paths) != len({path.casefold() for path in paths}):
        raise RuntimeError("source-companion payload contains duplicate paths")

    application_payload_entry = next(entry for entry in payload if entry["kind"] == "application-source")
    application_entry = {
        "name": application_source_path.name,
        "sha256": application_payload_entry["sha256"],
        "size": application_payload_entry["size"],
    }
    manifest = {
        "application_source": application_entry,
        "commit": commit,
        "dependency_lock": {
            "path": "requirements-release.txt",
            "sha256": _sha256_bytes(dependency_lock_content),
        },
        "frozen_runtime_sources": [
            source.manifest_entry() for source in sources if set(source.provides) & _EXTRA_PROVIDERS
        ],
        "payload": [_public_payload_entry(entry) for entry in sorted(payload, key=lambda item: item["path"])],
        "portable_archives": portable_entries,
        "schema": _MANIFEST_SCHEMA,
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "source_date_epoch": epoch,
        "source_lock": {
            "path": "release-sources.json",
            "sha256": _sha256_bytes(source_lock_content),
        },
        "upstream_sources": [source.manifest_entry() for source in sources],
        "version": version,
    }
    manifest_content = _json_bytes(manifest)
    if len(manifest_content) > _MAX_MANIFEST_SIZE:
        raise RuntimeError("source-companion manifest exceeds its size limit")
    manifest_entry = _payload_entry(
        "SOURCE-MANIFEST.json",
        "source-manifest",
        content=manifest_content,
    )
    archive_payload = payload + [manifest_entry]
    projected_size = sum(entry["size"] for entry in archive_payload)
    if projected_size >= _MAX_COMPANION_SIZE:
        raise RuntimeError("source-companion payload exceeds the GitHub asset size limit")

    temporary = tempfile.NamedTemporaryFile(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary.close()
    temporary_path = Path(temporary.name)
    root_name = f"dupeguru-neo-{version}-source-companion"
    try:
        with tarfile.open(
            temporary_path,
            mode="w",
            format=tarfile.PAX_FORMAT,
        ) as archive:
            for directory in _archive_directories(
                root_name,
                [entry["path"] for entry in archive_payload],
            ):
                archive.addfile(_tar_member(directory, directory=True))
            for entry in sorted(archive_payload, key=lambda item: item["path"]):
                member = _tar_member(
                    f"{root_name}/{entry['path']}",
                    size=entry["size"],
                )
                if entry["content"] is not None:
                    stream = io.BytesIO(entry["content"])
                    archive.addfile(member, stream)
                else:
                    with entry["source"].open("rb") as stream:
                        archive.addfile(member, stream)
        if temporary_path.stat().st_size >= _MAX_COMPANION_SIZE:
            raise RuntimeError("source companion exceeds the GitHub asset size limit")
        archive_digest = _sha256_file(temporary_path)
        proof = {
            "application_source": {
                "name": application_source_path.name,
                "sha256": application_entry["sha256"],
                "size": application_entry["size"],
            },
            "archive": {
                "name": output_path.name,
                "sha256": archive_digest,
                "size": temporary_path.stat().st_size,
            },
            "commit": commit,
            "dependency_lock": manifest["dependency_lock"],
            "manifest": {
                "path": "SOURCE-MANIFEST.json",
                "sha256": manifest_entry["sha256"],
                "size": manifest_entry["size"],
            },
            "portable_archives": portable_entries,
            "schema": _PROOF_SCHEMA,
            "schema_version": _PROOF_SCHEMA_VERSION,
            "source_date_epoch": epoch,
            "source_lock": manifest["source_lock"],
            "version": version,
        }
        proof_content = _json_bytes(proof)
        proof_temporary = tempfile.NamedTemporaryFile(
            dir=proof_path.parent,
            prefix=f".{proof_path.name}.",
            suffix=".tmp",
            delete=False,
        )
        proof_temporary_path = Path(proof_temporary.name)
        try:
            proof_temporary.write(proof_content)
            proof_temporary.flush()
            os.fsync(proof_temporary.fileno())
        finally:
            proof_temporary.close()
        os.replace(temporary_path, output_path)
        os.replace(proof_temporary_path, proof_path)
        verify_source_companion(
            archive_path=output_path,
            proof_path=proof_path,
            source_lock_path=source_lock_path,
            dependency_lock_path=dependency_lock_path,
            version=version,
            commit=commit,
        )
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        proof_path.unlink(missing_ok=True)
        try:
            proof_temporary_path.unlink(missing_ok=True)
        except UnboundLocalError:
            pass
        raise
    return output_path, proof_path


def _relative_archive_member(name: str, root_name: str) -> str | None:
    path = _safe_relative_path(name.rstrip("/"))
    if path.parts[0] != root_name:
        raise RuntimeError(f"source-companion member is outside its root: {name!r}")
    if len(path.parts) == 1:
        return None
    return PurePosixPath(*path.parts[1:]).as_posix()


def _validate_proof(
    proof: dict,
    *,
    version: str,
    commit: str,
    source_lock_digest: str,
    dependency_lock_digest: str,
) -> None:
    expected_fields = {
        "application_source",
        "archive",
        "commit",
        "dependency_lock",
        "manifest",
        "portable_archives",
        "schema",
        "schema_version",
        "source_date_epoch",
        "source_lock",
        "version",
    }
    if set(proof) != expected_fields:
        raise RuntimeError("source-companion proof has unexpected or missing fields")
    if proof["schema"] != _PROOF_SCHEMA or proof["schema_version"] != _PROOF_SCHEMA_VERSION:
        raise RuntimeError("unsupported source-companion proof schema")
    if proof["version"] != version or proof["commit"] != commit:
        raise RuntimeError("source-companion proof release identity mismatch")
    if proof["source_date_epoch"] != _source_date_epoch():
        raise RuntimeError("source-companion proof epoch mismatch")
    if proof["source_lock"] != {
        "path": "release-sources.json",
        "sha256": source_lock_digest,
    }:
        raise RuntimeError("source-companion proof source-lock mismatch")
    if proof["dependency_lock"] != {
        "path": "requirements-release.txt",
        "sha256": dependency_lock_digest,
    }:
        raise RuntimeError("source-companion proof dependency-lock mismatch")
    for field_name in ("archive", "application_source", "manifest"):
        value = proof[field_name]
        if not isinstance(value, dict):
            raise RuntimeError(f"source-companion proof {field_name} must be an object")
        expected = (
            {"name", "sha256", "size"}
            if field_name != "manifest"
            else {
                "path",
                "sha256",
                "size",
            }
        )
        if set(value) != expected:
            raise RuntimeError(f"source-companion proof {field_name} has invalid fields")
        name_key = "path" if field_name == "manifest" else "name"
        if not isinstance(value[name_key], str):
            raise RuntimeError(f"source-companion proof {field_name} has an invalid name")
        if not isinstance(value["sha256"], str) or _SHA256.fullmatch(value["sha256"]) is None:
            raise RuntimeError(f"source-companion proof {field_name} has an invalid digest")
        if isinstance(value["size"], bool) or not isinstance(value["size"], int) or value["size"] <= 0:
            raise RuntimeError(f"source-companion proof {field_name} has an invalid size")
    if proof["manifest"]["path"] != "SOURCE-MANIFEST.json":
        raise RuntimeError("source-companion proof has an unexpected manifest path")
    if not isinstance(proof["portable_archives"], list):
        raise RuntimeError("source-companion proof portable_archives must be a list")


def _validate_portable_proof_entries(entries: list, version: str) -> None:
    platforms = set()
    names = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "architecture",
            "inventory_prefix",
            "name",
            "platform",
            "sha256",
            "size",
        }:
            raise RuntimeError("source-companion proof has an invalid portable archive entry")
        if not all(
            isinstance(entry[field], str)
            for field in (
                "architecture",
                "inventory_prefix",
                "name",
                "platform",
                "sha256",
            )
        ):
            raise RuntimeError("source-companion proof portable archive fields must be strings")
        match = _PORTABLE_ARCHIVE.fullmatch(entry["name"])
        if (
            match is None
            or match.group("version") != version
            or match.group("platform") != entry["platform"]
            or match.group("architecture") != entry["architecture"]
        ):
            raise RuntimeError("source-companion proof portable archive identity mismatch")
        if entry["name"] in names or entry["platform"] in platforms:
            raise RuntimeError("source-companion proof contains duplicate portable archives")
        names.add(entry["name"])
        platforms.add(entry["platform"])
        expected_prefix = f"license-inventories/{entry['platform']}-{entry['architecture']}"
        if entry["inventory_prefix"] != expected_prefix:
            raise RuntimeError("source-companion proof portable inventory prefix mismatch")
        if not isinstance(entry["sha256"], str) or _SHA256.fullmatch(entry["sha256"]) is None:
            raise RuntimeError("source-companion proof portable archive has an invalid digest")
        if isinstance(entry["size"], bool) or not isinstance(entry["size"], int) or entry["size"] <= 0:
            raise RuntimeError("source-companion proof portable archive has an invalid size")
    if platforms != {"linux", "macos", "windows"}:
        raise RuntimeError("source-companion proof does not cover all portable platforms")


def _validate_manifest(
    manifest: dict,
    *,
    proof: dict,
    sources: tuple[SourceRecord, ...],
    found_files: dict[str, dict],
) -> None:
    expected_fields = {
        "application_source",
        "commit",
        "dependency_lock",
        "frozen_runtime_sources",
        "payload",
        "portable_archives",
        "schema",
        "schema_version",
        "source_date_epoch",
        "source_lock",
        "upstream_sources",
        "version",
    }
    if set(manifest) != expected_fields:
        raise RuntimeError("source-companion manifest has unexpected or missing fields")
    if manifest["schema"] != _MANIFEST_SCHEMA or manifest["schema_version"] != _MANIFEST_SCHEMA_VERSION:
        raise RuntimeError("unsupported source-companion manifest schema")
    for field_name in (
        "application_source",
        "commit",
        "dependency_lock",
        "portable_archives",
        "source_date_epoch",
        "source_lock",
        "version",
    ):
        if manifest[field_name] != proof[field_name]:
            raise RuntimeError(f"source-companion manifest {field_name} differs from proof")
    expected_upstream = [source.manifest_entry() for source in sources]
    if manifest["upstream_sources"] != expected_upstream:
        raise RuntimeError("source-companion manifest upstream source mapping mismatch")
    expected_frozen_runtime = [source.manifest_entry() for source in sources if set(source.provides) & _EXTRA_PROVIDERS]
    if manifest["frozen_runtime_sources"] != expected_frozen_runtime:
        raise RuntimeError("source-companion manifest frozen runtime mapping mismatch")
    payload = manifest["payload"]
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("source-companion manifest payload must be a non-empty list")
    payload_entries = {}
    for entry in payload:
        if not isinstance(entry, dict) or set(entry) != {"kind", "path", "sha256", "size"}:
            raise RuntimeError("source-companion manifest has an invalid payload entry")
        raw_path = entry["path"]
        if not isinstance(raw_path, str):
            raise RuntimeError("source-companion manifest payload path must be a string")
        _safe_relative_path(raw_path)
        if raw_path == "SOURCE-MANIFEST.json":
            raise RuntimeError("source-companion manifest must not hash itself")
        if raw_path in payload_entries or raw_path.casefold() in {path.casefold() for path in payload_entries}:
            raise RuntimeError(f"duplicate source-companion manifest path: {raw_path}")
        if not isinstance(entry["kind"], str) or not entry["kind"]:
            raise RuntimeError("source-companion manifest payload kind is invalid")
        if not isinstance(entry["sha256"], str) or _SHA256.fullmatch(entry["sha256"]) is None:
            raise RuntimeError("source-companion manifest payload digest is invalid")
        if isinstance(entry["size"], bool) or not isinstance(entry["size"], int) or entry["size"] <= 0:
            raise RuntimeError("source-companion manifest payload size is invalid")
        payload_entries[raw_path] = entry
    if [entry["path"] for entry in payload] != sorted(payload_entries):
        raise RuntimeError("source-companion manifest payload is not sorted")
    expected_found = set(payload_entries) | {"SOURCE-MANIFEST.json"}
    if set(found_files) != expected_found:
        raise RuntimeError(
            "source-companion archive payload mismatch; missing={} unexpected={}".format(
                sorted(expected_found - set(found_files)),
                sorted(set(found_files) - expected_found),
            )
        )
    for path, entry in payload_entries.items():
        found = found_files[path]
        if found["sha256"] != entry["sha256"] or found["size"] != entry["size"]:
            raise RuntimeError(f"source-companion payload mismatch: {path}")

    fixed_paths = {
        "BUILD-RECIPE.md",
        "HSCOMMON-BSD-3-CLAUSE.txt",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "release-sources.json",
        "requirements-release.txt",
        f"application/{proof['application_source']['name']}",
    }
    upstream_paths = {f"upstream/{source.filename}" for source in sources}
    if not fixed_paths | upstream_paths <= set(payload_entries):
        raise RuntimeError("source-companion manifest omits required source or legal files")
    for source in sources:
        path = f"upstream/{source.filename}"
        entry = payload_entries[path]
        if entry["kind"] != "upstream-source" or entry["sha256"] != source.sha256 or entry["size"] != source.size:
            raise RuntimeError(f"source-companion upstream payload mismatch: {source.filename}")
    application_path = f"application/{proof['application_source']['name']}"
    application = payload_entries[application_path]
    if (
        application["kind"] != "application-source"
        or application["sha256"] != proof["application_source"]["sha256"]
        or application["size"] != proof["application_source"]["size"]
    ):
        raise RuntimeError("source-companion application source payload mismatch")
    for portable in proof["portable_archives"]:
        prefix = portable["inventory_prefix"]
        for category in ("frozen-runtime", "third-party"):
            for index_name in ("index.json", "index.txt"):
                path = f"{prefix}/{category}/{index_name}"
                if path not in payload_entries or payload_entries[path]["kind"] != "portable-license-inventory":
                    raise RuntimeError(
                        f"source-companion omits {portable['platform']} " f"{category} license inventory index"
                    )
    for path, entry in payload_entries.items():
        if path.startswith("license-inventories/") and entry["kind"] != "portable-license-inventory":
            raise RuntimeError(f"source-companion license inventory has a wrong kind: {path}")


def verify_source_companion(
    *,
    archive_path: Path,
    proof_path: Path,
    source_lock_path: Path,
    dependency_lock_path: Path,
    version: str,
    commit: str,
) -> None:
    _validate_version_and_commit(version, commit)
    _, sources = validate_source_lock(source_lock_path, dependency_lock_path)
    source_lock_path = _regular_file(source_lock_path, "upstream source lock")
    dependency_lock_path = _regular_file(dependency_lock_path, "dependency lock")
    proof_path = _regular_file(proof_path, "source-companion proof")
    if proof_path.stat().st_size > _MAX_MANIFEST_SIZE:
        raise RuntimeError("source-companion proof exceeds its size limit")
    proof = _load_json(proof_path, "source-companion proof")
    _validate_proof(
        proof,
        version=version,
        commit=commit,
        source_lock_digest=_sha256_file(source_lock_path),
        dependency_lock_digest=_sha256_file(dependency_lock_path),
    )
    _validate_portable_proof_entries(proof["portable_archives"], version)
    archive_path = _regular_file(archive_path, "source companion")
    expected_name = _expected_companion_name(version)
    if archive_path.name != expected_name or proof["archive"]["name"] != expected_name:
        raise RuntimeError("source-companion archive name mismatch")
    archive_size = archive_path.stat().st_size
    if archive_size >= _MAX_COMPANION_SIZE:
        raise RuntimeError("source companion exceeds the GitHub asset size limit")
    if archive_size != proof["archive"]["size"]:
        raise RuntimeError("source-companion archive size differs from proof")
    if _sha256_file(archive_path) != proof["archive"]["sha256"]:
        raise RuntimeError("source-companion archive digest differs from proof")

    root_name = f"dupeguru-neo-{version}-source-companion"
    found_files = {}
    found_directories = set()
    manifest_content = None
    member_order = []
    with tarfile.open(archive_path, mode="r:") as archive:
        for member in archive:
            relative = _relative_archive_member(member.name, root_name)
            canonical_name = member.name.rstrip("/")
            if canonical_name in member_order:
                raise RuntimeError(f"duplicate source-companion archive member: {canonical_name}")
            member_order.append(canonical_name)
            if (
                member.mtime != _source_date_epoch()
                or member.uid != 0
                or member.gid != 0
                or member.uname
                or member.gname
            ):
                raise RuntimeError(f"non-deterministic source-companion metadata: {member.name}")
            if member.isdir():
                if member.mode != 0o755 or member.size != 0:
                    raise RuntimeError(f"invalid source-companion directory metadata: {member.name}")
                found_directories.add(relative)
                continue
            if not member.isfile() or relative is None:
                raise RuntimeError(f"unsupported source-companion member: {member.name}")
            if member.mode != 0o644 or member.size <= 0:
                raise RuntimeError(f"invalid source-companion file metadata: {member.name}")
            if relative in found_files:
                raise RuntimeError(f"duplicate source-companion archive member: {relative}")
            stream = archive.extractfile(member)
            if stream is None:
                raise RuntimeError(f"cannot read source-companion member: {member.name}")
            if relative == "SOURCE-MANIFEST.json":
                if member.size > _MAX_MANIFEST_SIZE:
                    raise RuntimeError("source-companion manifest exceeds its size limit")
                manifest_content = stream.read(member.size + 1)
                if len(manifest_content) != member.size:
                    raise RuntimeError("source-companion manifest is truncated")
                digest = _sha256_bytes(manifest_content)
            else:
                digest_object = hashlib.sha256()
                read_size = 0
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest_object.update(chunk)
                    read_size += len(chunk)
                if read_size != member.size:
                    raise RuntimeError(f"source-companion member is truncated: {member.name}")
                digest = digest_object.hexdigest()
            found_files[relative] = {
                "sha256": digest,
                "size": member.size,
            }
    if manifest_content is None:
        raise RuntimeError("source companion has no SOURCE-MANIFEST.json")
    if (
        len(manifest_content) != proof["manifest"]["size"]
        or _sha256_bytes(manifest_content) != proof["manifest"]["sha256"]
    ):
        raise RuntimeError("source-companion manifest differs from proof")
    try:
        manifest = json.loads(
            manifest_content.decode("utf-8"),
            object_pairs_hook=lambda pairs: _reject_duplicate_manifest_pairs(pairs),
        )
    except RuntimeError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("invalid source-companion manifest JSON") from error
    if not isinstance(manifest, dict):
        raise RuntimeError("source-companion manifest must be an object")
    _validate_manifest(
        manifest,
        proof=proof,
        sources=sources,
        found_files=found_files,
    )
    expected_directories = {
        None,
        *{
            PurePosixPath(*_safe_relative_path(path).parts[:length]).as_posix()
            for path in found_files
            for length in range(1, len(_safe_relative_path(path).parts))
        },
    }
    if found_directories != expected_directories:
        raise RuntimeError(
            "source-companion directory set mismatch; missing={} unexpected={}".format(
                sorted(
                    (expected_directories - found_directories),
                    key=lambda item: "" if item is None else item,
                ),
                sorted(
                    (found_directories - expected_directories),
                    key=lambda item: "" if item is None else item,
                ),
            )
        )
    expected_order = [name.rstrip("/") for name in _archive_directories(root_name, list(found_files))] + [
        f"{root_name}/{path}" for path in sorted(found_files)
    ]
    if member_order != expected_order:
        raise RuntimeError("source-companion archive members are not in deterministic order")


def _reject_duplicate_manifest_pairs(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise RuntimeError(f"duplicate JSON key in source-companion manifest: {key}")
        document[key] = value
    return document


def _parser() -> ArgumentParser:
    parser = ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument(
        "--source-lock",
        type=Path,
        default=Path("release-sources.json"),
    )
    validate.add_argument(
        "--dependency-lock",
        type=Path,
        default=Path("requirements-release.txt"),
    )

    fetch = subparsers.add_parser("fetch")
    fetch.add_argument(
        "--source-lock",
        type=Path,
        default=Path("release-sources.json"),
    )
    fetch.add_argument(
        "--dependency-lock",
        type=Path,
        default=Path("requirements-release.txt"),
    )
    fetch.add_argument("--directory", type=Path, required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--version", required=True)
    build.add_argument("--commit", required=True)
    build.add_argument(
        "--source-lock",
        type=Path,
        default=Path("release-sources.json"),
    )
    build.add_argument(
        "--dependency-lock",
        type=Path,
        default=Path("requirements-release.txt"),
    )
    build.add_argument("--source-directory", type=Path, required=True)
    build.add_argument("--application-source", type=Path, required=True)
    build.add_argument("--portable-directory", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--proof", type=Path, required=True)
    build.add_argument(
        "--recipe",
        type=Path,
        default=Path("docs/SOURCE-COMPANION.md"),
    )
    build.add_argument(
        "--notice",
        type=Path,
        default=Path("THIRD_PARTY_NOTICES.md"),
    )
    build.add_argument("--license", type=Path, default=Path("LICENSE"))
    build.add_argument(
        "--hscommon-license",
        type=Path,
        default=Path("hscommon/LICENSE"),
    )

    verify = subparsers.add_parser("verify")
    verify.add_argument("--version", required=True)
    verify.add_argument("--commit", required=True)
    verify.add_argument(
        "--source-lock",
        type=Path,
        default=Path("release-sources.json"),
    )
    verify.add_argument(
        "--dependency-lock",
        type=Path,
        default=Path("requirements-release.txt"),
    )
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--proof", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        document, sources = validate_source_lock(
            args.source_lock,
            args.dependency_lock,
        )
        print(f"validated {len(sources)} source archives for CPython " f"{document['portable_python_version']}")
    elif args.command == "fetch":
        for path in fetch_sources(
            args.source_lock,
            args.dependency_lock,
            args.directory,
        ):
            print(path)
    elif args.command == "build":
        archive, proof = build_source_companion(
            version=args.version,
            commit=args.commit,
            source_lock_path=args.source_lock,
            dependency_lock_path=args.dependency_lock,
            source_directory=args.source_directory,
            application_source_path=args.application_source,
            portable_directory=args.portable_directory,
            output_path=args.output,
            proof_path=args.proof,
            recipe_path=args.recipe,
            notice_path=args.notice,
            license_path=args.license,
            hscommon_license_path=args.hscommon_license,
        )
        print(archive)
        print(proof)
    elif args.command == "verify":
        verify_source_companion(
            archive_path=args.archive,
            proof_path=args.proof,
            source_lock_path=args.source_lock,
            dependency_lock_path=args.dependency_lock,
            version=args.version,
            commit=args.commit,
        )
        print(args.archive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
