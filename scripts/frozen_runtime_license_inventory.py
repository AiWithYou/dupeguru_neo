#!/usr/bin/env python3

"""Inventory licenses for CPython and PyInstaller code in frozen bundles."""

from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import lzma
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

from packaging.utils import canonicalize_name

_SCHEMA = "dupeguru.frozen-runtime-license-inventory"
_SCHEMA_VERSION = 1
_SOURCE_LOCK_SCHEMA = "dupeguru.release-source-lock"
_SOURCE_LOCK_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_LICENSE_FILE_SIZE = 16 * 1024 * 1024
_MAX_LICENSE_TOTAL_SIZE = 32 * 1024 * 1024
_MAX_CPYTHON_SOURCE_ARCHIVE_SIZE = 128 * 1024 * 1024
_MAX_CPYTHON_SOURCE_MEMBER_COUNT = 100_000
_MAX_CPYTHON_SOURCE_MEMBER_SIZE = 256 * 1024 * 1024
_MAX_CPYTHON_SOURCE_TOTAL_SIZE = 1024 * 1024 * 1024
_MAX_CPYTHON_SOURCE_EXPANDED_SIZE = 1024 * 1024 * 1024
_MAX_CPYTHON_SOURCE_PATH_LENGTH = 4096
_COMPONENT_MARKERS = {
    "cpython-runtime": b"PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2",
    "pyinstaller-bootloader": b"Bootloader Exception",
}


class _CPythonLicenseAbsent(RuntimeError):
    """The active runtime contains no CPython license file."""


class _BoundedReader:
    def __init__(self, stream, maximum_size: int):
        self._stream = stream
        self._maximum_size = maximum_size
        self._consumed = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._maximum_size - self._consumed
        if size < 0 or size > remaining + 1:
            size = remaining + 1
        content = self._stream.read(size)
        self._consumed += len(content)
        if self._consumed > self._maximum_size:
            raise RuntimeError("CPython source archive exceeds its decompressed-size limit")
        return content


def _source_date_epoch() -> int:
    raw_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if raw_epoch is None:
        raise RuntimeError("SOURCE_DATE_EPOCH is required for the frozen-runtime inventory")
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


def _load_json(path: Path, label: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} must be a regular non-symlink file")

    def reject_duplicates(pairs):
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
        document = json.loads(text, object_pairs_hook=reject_duplicates)
    except RuntimeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid {label}: {path}") from error
    if not isinstance(document, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return document


def _safe_relative_path(raw_path: str) -> PurePosixPath:
    if not raw_path or "\0" in raw_path or "\\" in raw_path or raw_path.startswith("/"):
        raise RuntimeError(f"unsafe frozen-runtime inventory path: {raw_path!r}")
    path = PurePosixPath(raw_path)
    if not path.parts or path.as_posix() != raw_path or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(f"unsafe frozen-runtime inventory path: {raw_path!r}")
    return path


def _source_components(source_lock_path: Path) -> dict[str, dict]:
    if source_lock_path.is_symlink() or not source_lock_path.is_file():
        raise RuntimeError("frozen-runtime source lock must be a regular non-symlink file")
    source_lock_path = source_lock_path.resolve(strict=True)
    if source_lock_path.name != "release-sources.json":
        raise RuntimeError("frozen-runtime source lock must be named release-sources.json")
    document = _load_json(source_lock_path, "frozen-runtime source lock")
    if document.get("schema") != _SOURCE_LOCK_SCHEMA or document.get("schema_version") != _SOURCE_LOCK_SCHEMA_VERSION:
        raise RuntimeError("unsupported frozen-runtime source lock schema")
    raw_sources = document.get("sources")
    if not isinstance(raw_sources, list):
        raise RuntimeError("frozen-runtime source lock sources must be an array")
    found = {}
    for source in raw_sources:
        if not isinstance(source, dict):
            raise RuntimeError("frozen-runtime source lock entry must be an object")
        provides = source.get("provides")
        if not isinstance(provides, list):
            raise RuntimeError("frozen-runtime source provider list is invalid")
        for raw_provider in provides:
            if not isinstance(raw_provider, str):
                raise RuntimeError("frozen-runtime source provider is invalid")
            provider = canonicalize_name(raw_provider)
            if provider not in _COMPONENT_MARKERS:
                continue
            if provider in found:
                raise RuntimeError(f"duplicate frozen-runtime source provider: {provider}")
            required = {
                "filename": source.get("filename"),
                "name": source.get("name"),
                "sha256": source.get("sha256"),
                "url": source.get("url"),
                "version": source.get("version"),
            }
            size = source.get("size")
            if (
                not all(isinstance(value, str) and value for value in required.values())
                or _SHA256.fullmatch(required["sha256"]) is None
                or isinstance(size, bool)
                or not isinstance(size, int)
                or not 0 < size <= _MAX_CPYTHON_SOURCE_ARCHIVE_SIZE
            ):
                raise RuntimeError(f"invalid frozen-runtime source entry: {provider}")
            found[provider] = required
    if set(found) != set(_COMPONENT_MARKERS):
        raise RuntimeError(
            "frozen-runtime source coverage mismatch; missing={} unexpected={}".format(
                sorted(set(_COMPONENT_MARKERS) - set(found)),
                sorted(set(found) - set(_COMPONENT_MARKERS)),
            )
        )
    return found


def _cpython_license() -> tuple[str, bytes]:
    base_prefix = Path(sys.base_prefix)
    for name in ("LICENSE.txt", "LICENSE"):
        candidate = base_prefix.joinpath(name)
        if candidate.is_symlink():
            raise RuntimeError(f"CPython license path must not be a symlink: {candidate}")
        if candidate.is_file():
            content = candidate.read_bytes()
            if not 0 < len(content) <= _MAX_LICENSE_FILE_SIZE:
                raise RuntimeError("CPython license has an invalid size")
            if _COMPONENT_MARKERS["cpython-runtime"] not in content:
                raise RuntimeError("CPython license does not contain the expected PSF terms")
            return name, content
    raise _CPythonLicenseAbsent(f"CPython license is absent from sys.base_prefix: {base_prefix}")


def _cpython_source_pin(source_lock_path: Path, expected_source: dict) -> dict:
    document = _load_json(source_lock_path, "frozen-runtime source lock")
    raw_sources = document.get("sources")
    if not isinstance(raw_sources, list):
        raise RuntimeError("frozen-runtime source lock sources must be an array")
    matches = []
    for source in raw_sources:
        if not isinstance(source, dict):
            raise RuntimeError("frozen-runtime source lock entry must be an object")
        provides = source.get("provides")
        if not isinstance(provides, list):
            raise RuntimeError("frozen-runtime source provider list is invalid")
        if any(isinstance(provider, str) and canonicalize_name(provider) == "cpython-runtime" for provider in provides):
            matches.append(source)
    if len(matches) != 1:
        raise RuntimeError("frozen-runtime source lock must contain one CPython source")
    source = matches[0]
    public_source = {
        "filename": source.get("filename"),
        "name": source.get("name"),
        "sha256": source.get("sha256"),
        "url": source.get("url"),
        "version": source.get("version"),
    }
    if public_source != expected_source:
        raise RuntimeError("frozen-runtime CPython source mapping changed unexpectedly")
    version = expected_source["version"]
    if source.get("kind") != "python-official-source":
        raise RuntimeError("frozen-runtime CPython source kind is invalid")
    if source.get("filename") != f"Python-{version}.tar.xz":
        raise RuntimeError("frozen-runtime CPython source filename is invalid")
    size = source.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= _MAX_CPYTHON_SOURCE_ARCHIVE_SIZE:
        raise RuntimeError("frozen-runtime CPython source size is invalid")
    parsed_url = urlsplit(source["url"])
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "www.python.org"
        or parsed_url.netloc != "www.python.org"
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
        or PurePosixPath(parsed_url.path).name != source["filename"]
    ):
        raise RuntimeError("frozen-runtime CPython source URL is unsafe")
    return {
        **public_source,
        "size": size,
    }


def _curl_executable() -> str:
    candidates = ("curl.exe", "curl") if sys.platform == "win32" else ("curl",)
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable is not None:
            return executable
    raise RuntimeError("curl is required to fetch the pinned CPython source archive")


def _run_curl(url: str, output_path: Path, maximum_size: int) -> None:
    command = [
        _curl_executable(),
        "--fail",
        "--location",
        "--show-error",
        "--silent",
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
        "--max-filesize",
        str(maximum_size),
        "--output",
        str(output_path),
        url,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=1800,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("curl could not fetch the pinned CPython source archive") from error
    if result.returncode != 0:
        raise RuntimeError(f"curl failed with exit code {result.returncode} while fetching CPython source")


def _read_cpython_license_from_archive(archive_path: Path, source: dict) -> bytes:
    if archive_path.is_symlink() or not archive_path.is_file():
        raise RuntimeError("downloaded CPython source must be a regular non-symlink file")
    archive_size = archive_path.stat().st_size
    if archive_size != source["size"]:
        raise RuntimeError(
            "downloaded CPython source size mismatch: " f"expected {source['size']}, found {archive_size}"
        )
    archive_digest = _sha256_file(archive_path)
    if archive_digest != source["sha256"]:
        raise RuntimeError(
            "downloaded CPython source digest mismatch: " f"expected {source['sha256']}, found {archive_digest}"
        )
    expected_root = f"Python-{source['version']}"
    expected_license = f"{expected_root}/LICENSE"
    seen_paths = set()
    license_content = None
    member_count = 0
    total_size = 0
    try:
        with lzma.open(archive_path, mode="rb") as decompressed:
            bounded = _BoundedReader(
                decompressed,
                _MAX_CPYTHON_SOURCE_EXPANDED_SIZE,
            )
            with tarfile.open(fileobj=bounded, mode="r|") as archive:
                for member in archive:
                    member_count += 1
                    if member_count > _MAX_CPYTHON_SOURCE_MEMBER_COUNT:
                        raise RuntimeError("CPython source archive contains too many members")
                    member_name = member.name
                    if (
                        not member_name
                        or len(member_name) > _MAX_CPYTHON_SOURCE_PATH_LENGTH
                        or "\0" in member_name
                        or "\\" in member_name
                        or member_name.startswith("/")
                    ):
                        raise RuntimeError("CPython source archive contains an unsafe member path")
                    member_path = PurePosixPath(member_name)
                    if (
                        not member_path.parts
                        or member_path.parts[0] != expected_root
                        or any(part in {"", ".", ".."} for part in member_path.parts)
                    ):
                        raise RuntimeError("CPython source archive member is outside its expected root")
                    normalized_name = member_path.as_posix()
                    if normalized_name in seen_paths:
                        raise RuntimeError("CPython source archive contains a duplicate member path")
                    seen_paths.add(normalized_name)
                    if member.isfile():
                        if (
                            member.size < 0
                            or member.size > _MAX_CPYTHON_SOURCE_MEMBER_SIZE
                            or total_size > _MAX_CPYTHON_SOURCE_TOTAL_SIZE - member.size
                        ):
                            raise RuntimeError("CPython source archive exceeds its expanded-size limit")
                        total_size += member.size
                    if normalized_name != expected_license:
                        continue
                    if not member.isfile():
                        raise RuntimeError("CPython source LICENSE must be a regular archive member")
                    if not 0 < member.size <= _MAX_LICENSE_FILE_SIZE:
                        raise RuntimeError("CPython source LICENSE has an invalid size")
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise RuntimeError("CPython source LICENSE could not be read")
                    content = extracted.read(_MAX_LICENSE_FILE_SIZE + 1)
                    if len(content) != member.size:
                        raise RuntimeError("CPython source LICENSE size does not match its archive metadata")
                    license_content = content
    except RuntimeError:
        raise
    except (OSError, EOFError, lzma.LZMAError, tarfile.TarError) as error:
        raise RuntimeError("downloaded CPython source is not a valid tar.xz archive") from error
    if license_content is None:
        raise RuntimeError(f"CPython source archive is missing {expected_license}")
    if _COMPONENT_MARKERS["cpython-runtime"] not in license_content:
        raise RuntimeError("CPython source LICENSE does not contain the expected PSF terms")
    return license_content


def _download_cpython_license(source: dict) -> tuple[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="dupeguru-cpython-license-") as temporary:
        temporary_root = Path(temporary)
        archive_path = temporary_root / source["filename"]
        _run_curl(source["url"], archive_path, source["size"])
        return "LICENSE", _read_cpython_license_from_archive(archive_path, source)


def _resolved_cpython_license(
    source_lock_path: Path,
    expected_source: dict,
) -> tuple[str, bytes]:
    source = _cpython_source_pin(source_lock_path, expected_source)
    try:
        return _cpython_license()
    except _CPythonLicenseAbsent:
        return _download_cpython_license(source)


def _distribution_license(
    distribution_name: str,
    required_marker: bytes,
) -> tuple[metadata.Distribution, str, bytes]:
    distribution = metadata.distribution(distribution_name)
    declared = distribution.metadata.get_all("License-File") or ()
    if not declared:
        raise RuntimeError(f"{distribution_name} declares no License-File metadata")
    candidates = []
    for raw_name in declared:
        if not isinstance(raw_name, str):
            raise RuntimeError(f"{distribution_name} has invalid License-File metadata")
        relative = _safe_relative_path(raw_name.replace("\\", "/"))
        for package_path in distribution.files or ():
            rendered = str(package_path).replace("\\", "/")
            if (
                rendered.lower() == relative.as_posix().lower()
                or rendered.lower().endswith(f"/{relative.as_posix().lower()}")
                or rendered.lower().endswith(f"/licenses/{relative.as_posix().lower()}")
            ):
                candidates.append((rendered, package_path))
    if not candidates:
        raise RuntimeError(f"{distribution_name} declared license text is absent")
    for rendered, package_path in sorted(candidates, key=lambda item: item[0]):
        located = Path(distribution.locate_file(package_path))
        installation_root = Path(distribution.locate_file(""))
        if installation_root.is_symlink():
            raise RuntimeError(f"{distribution_name} installation root must not be a symlink")
        installation_root = installation_root.resolve(strict=True)
        if not located.is_absolute():
            located = installation_root.joinpath(located)
        if located.is_symlink() or not located.is_file():
            raise RuntimeError(f"{distribution_name} license is not a regular file")
        located = located.resolve(strict=True)
        if installation_root not in (located, *located.parents):
            raise RuntimeError(f"{distribution_name} license resolves outside its installation root")
        content = located.read_bytes()
        if not 0 < len(content) <= _MAX_LICENSE_FILE_SIZE:
            raise RuntimeError(f"{distribution_name} license has an invalid size")
        if required_marker in content:
            return distribution, PurePosixPath(rendered).name, content
    raise RuntimeError(f"{distribution_name} license does not contain the required bootloader terms")


def _component_document(
    *,
    component: str,
    name: str,
    version: str,
    designation: str,
    filename: str,
    content: bytes,
    source: dict,
) -> tuple[dict, str, bytes]:
    copied_path = f"components/{component}-{version}/" f"{PurePosixPath(filename).name}"
    document = {
        "component": component,
        "files": [
            {
                "copied_path": copied_path,
                "sha256": _sha256_bytes(content),
                "size": len(content),
                "source_path": filename,
            }
        ],
        "license_designation": designation,
        "name": name,
        "source_archive": source,
        "version": version,
    }
    return document, copied_path, content


def _render_text(document: dict) -> str:
    lines = [
        "dupeGuru Neo frozen-runtime license inventory",
        f"Generated: {document['generated_at']}",
        f"Platform: {document['platform']['system']} / {document['platform']['machine']}",
        (f"Source lock: {document['source_lock']['path']} " f"(SHA-256 {document['source_lock']['sha256']})"),
        "",
    ]
    for component in document["components"]:
        lines.append(f"{component['name']} {component['version']} — " f"{component['license_designation']}")
        source = component["source_archive"]
        lines.append(f"  corresponding source: {source['filename']} " f"(SHA-256 {source['sha256']})")
        for license_file in component["files"]:
            lines.append(
                f"  {license_file['copied_path']} "
                f"(from {license_file['source_path']}, "
                f"SHA-256 {license_file['sha256']})"
            )
        lines.append("")
    return "\n".join(lines)


def generate_inventory(
    source_lock_path: Path,
    output_directory: Path,
) -> Path:
    if source_lock_path.is_symlink() or not source_lock_path.is_file():
        raise RuntimeError("frozen-runtime source lock must be a regular non-symlink file")
    source_lock_path = source_lock_path.resolve(strict=True)
    source_components = _source_components(source_lock_path)
    output_directory = output_directory.resolve()
    if output_directory.is_symlink():
        raise RuntimeError("frozen-runtime inventory output must not be a symlink")
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.",
            dir=output_directory.parent,
        )
    )
    try:
        if platform.python_version() != source_components["cpython-runtime"]["version"]:
            raise RuntimeError("active CPython version does not match the frozen-runtime source lock")
        cpython_filename, cpython_content = _resolved_cpython_license(
            source_lock_path,
            source_components["cpython-runtime"],
        )
        pyinstaller, pyinstaller_filename, pyinstaller_content = _distribution_license(
            "PyInstaller",
            _COMPONENT_MARKERS["pyinstaller-bootloader"],
        )
        if pyinstaller.version != source_components["pyinstaller-bootloader"]["version"]:
            raise RuntimeError("installed PyInstaller version does not match the frozen-runtime source lock")
        components = [
            _component_document(
                component="cpython-runtime",
                name="CPython",
                version=platform.python_version(),
                designation="PSF-2.0 and historical terms in the included LICENSE",
                filename=cpython_filename,
                content=cpython_content,
                source=source_components["cpython-runtime"],
            ),
            _component_document(
                component="pyinstaller-bootloader",
                name="PyInstaller bootloader and related files",
                version=pyinstaller.version,
                designation=("GPL-2.0-or-later with the included PyInstaller " "Bootloader Exception"),
                filename=pyinstaller_filename,
                content=pyinstaller_content,
                source=source_components["pyinstaller-bootloader"],
            ),
        ]
        total_size = sum(len(content) for _, _, content in components)
        if total_size > _MAX_LICENSE_TOTAL_SIZE:
            raise RuntimeError("frozen-runtime licenses exceed their size limit")
        component_documents = []
        for document, copied_path, content in components:
            destination = temporary_root.joinpath(*_safe_relative_path(copied_path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            component_documents.append(document)
        inventory = {
            "components": component_documents,
            "generated_at": _timestamp(),
            "platform": {
                "machine": platform.machine().lower(),
                "system": platform.system(),
            },
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "source_lock": {
                "path": source_lock_path.name,
                "sha256": _sha256_file(source_lock_path),
            },
        }
        temporary_root.joinpath("index.json").write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary_root.joinpath("index.txt").write_text(
            _render_text(inventory),
            encoding="utf-8",
            newline="\n",
        )
        if output_directory.exists():
            if output_directory.is_symlink() or not output_directory.is_dir():
                raise RuntimeError("frozen-runtime inventory output is not a safe directory")
            shutil.rmtree(output_directory)
        os.replace(temporary_root, output_directory)
    except BaseException:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise
    verify_inventory(output_directory, source_lock_path)
    return output_directory


def verify_inventory(
    inventory_directory: Path,
    source_lock_path: Path,
    *,
    expected_system: str | None = None,
) -> None:
    if inventory_directory.is_symlink() or not inventory_directory.is_dir():
        raise RuntimeError("frozen-runtime inventory must be a regular directory")
    if source_lock_path.is_symlink() or not source_lock_path.is_file():
        raise RuntimeError("frozen-runtime source lock must be a regular non-symlink file")
    inventory_directory = inventory_directory.resolve(strict=True)
    source_lock_path = source_lock_path.resolve(strict=True)
    source_components = _source_components(source_lock_path)
    for path in [inventory_directory, *inventory_directory.rglob("*")]:
        if path.is_symlink():
            raise RuntimeError(f"frozen-runtime inventory contains a symlink: {path}")
        if path != inventory_directory and not (path.is_file() or path.is_dir()):
            raise RuntimeError(f"frozen-runtime inventory contains a special file: {path}")
    document = _load_json(inventory_directory / "index.json", "frozen-runtime inventory")
    if set(document) != {
        "components",
        "generated_at",
        "platform",
        "schema",
        "schema_version",
        "source_lock",
    }:
        raise RuntimeError("frozen-runtime inventory has unexpected or missing fields")
    if document.get("schema") != _SCHEMA or document.get("schema_version") != _SCHEMA_VERSION:
        raise RuntimeError("frozen-runtime inventory schema is unsupported")
    if document.get("source_lock") != {
        "path": source_lock_path.name,
        "sha256": _sha256_file(source_lock_path),
    }:
        raise RuntimeError("frozen-runtime inventory source-lock mismatch")
    platform_document = document.get("platform")
    if not isinstance(platform_document, dict):
        raise RuntimeError("frozen-runtime inventory platform is invalid")
    if expected_system is not None and platform_document.get("system") != expected_system:
        raise RuntimeError(
            "frozen-runtime inventory platform mismatch: "
            f"expected {expected_system}, found {platform_document.get('system')}"
        )
    components = document.get("components")
    if not isinstance(components, list):
        raise RuntimeError("frozen-runtime inventory components must be an array")
    found = set()
    expected_paths = {"index.json", "index.txt"}
    for component in components:
        if not isinstance(component, dict) or set(component) != {
            "component",
            "files",
            "license_designation",
            "name",
            "source_archive",
            "version",
        }:
            raise RuntimeError("frozen-runtime component entry must be an object")
        component_name = component.get("component")
        if component_name in found or component_name not in source_components:
            raise RuntimeError(f"unexpected frozen-runtime component: {component_name}")
        found.add(component_name)
        source = source_components[component_name]
        if component.get("version") != source["version"]:
            raise RuntimeError(f"frozen-runtime version mismatch: {component_name}")
        if component.get("source_archive") != source:
            raise RuntimeError(f"frozen-runtime source mapping mismatch: {component_name}")
        designation = component.get("license_designation")
        if not isinstance(designation, str) or not designation:
            raise RuntimeError(f"frozen-runtime license designation is absent: {component_name}")
        files = component.get("files")
        if not isinstance(files, list) or not files:
            raise RuntimeError(f"frozen-runtime license text is absent: {component_name}")
        marker_found = False
        for file_document in files:
            if not isinstance(file_document, dict) or set(file_document) != {
                "copied_path",
                "sha256",
                "size",
                "source_path",
            }:
                raise RuntimeError("frozen-runtime license file entry is invalid")
            copied_path = _safe_relative_path(file_document.get("copied_path", ""))
            if not copied_path.as_posix().startswith("components/"):
                raise RuntimeError("frozen-runtime license is outside components/")
            expected_paths.add(copied_path.as_posix())
            path = inventory_directory.joinpath(*copied_path.parts)
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"frozen-runtime license file is missing: {copied_path}")
            content = path.read_bytes()
            if len(content) != file_document.get("size") or _sha256_bytes(content) != file_document.get("sha256"):
                raise RuntimeError(f"frozen-runtime license digest mismatch: {copied_path}")
            if _COMPONENT_MARKERS[component_name] in content:
                marker_found = True
        if not marker_found:
            raise RuntimeError(f"frozen-runtime required license terms are absent: {component_name}")
    if found != set(source_components):
        raise RuntimeError(
            "frozen-runtime inventory is missing components: " f"{sorted(set(source_components) - found)}"
        )
    actual_paths = {
        path.relative_to(inventory_directory).as_posix() for path in inventory_directory.rglob("*") if path.is_file()
    }
    if actual_paths != expected_paths:
        raise RuntimeError(
            "frozen-runtime inventory file set mismatch; missing={} unexpected={}".format(
                sorted(expected_paths - actual_paths),
                sorted(actual_paths - expected_paths),
            )
        )
    if inventory_directory.joinpath("index.txt").read_text(encoding="utf-8") != _render_text(document):
        raise RuntimeError("frozen-runtime human-readable index does not match JSON")


def _parser() -> ArgumentParser:
    parser = ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--source-lock", type=Path, required=True)
    generate.add_argument("--output-directory", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--source-lock", type=Path, required=True)
    verify.add_argument("--directory", type=Path, required=True)
    verify.add_argument("--expected-system")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "generate":
        print(generate_inventory(args.source_lock, args.output_directory))
    elif args.command == "verify":
        verify_inventory(
            args.directory,
            args.source_lock,
            expected_system=args.expected_system,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
