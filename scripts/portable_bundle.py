#!/usr/bin/env python3

"""Build, archive, and validate explicitly unsigned portable bundles."""

from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import zipfile

if __package__:
    from .dependency_license_inventory import generate_inventory, verify_inventory
    from .frozen_runtime_license_inventory import (
        generate_inventory as generate_frozen_runtime_inventory,
        verify_inventory as verify_frozen_runtime_inventory,
    )
else:
    from dependency_license_inventory import generate_inventory, verify_inventory
    from frozen_runtime_license_inventory import (
        generate_inventory as generate_frozen_runtime_inventory,
        verify_inventory as verify_frozen_runtime_inventory,
    )

_ARCHIVE_NAME = re.compile(
    r"^dupeguru-neo-(?P<version>[0-9A-Za-z][0-9A-Za-z._+-]*)-"
    r"(?P<platform>windows|macos|linux)-(?P<architecture>[0-9a-z_]+)-"
    r"unsigned-portable(?P<extension>\.zip|\.tar\.gz)$"
)
_SAFE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")
_DISALLOWED_NATIVE_SUFFIXES = {
    ".app",
    ".appimage",
    ".deb",
    ".dmg",
    ".exe",
    ".msi",
    ".msix",
    ".pkg",
    ".rpm",
}
_NATIVE_TRUST_CLAIMS = ("authenticode", "developer-id", "notarized", "signed-installer")
_ARCHIVE_SUFFIXES = (
    ".tar",
    ".tar.bz2",
    ".tar.gz",
    ".tar.xz",
    ".tbz2",
    ".tgz",
    ".txz",
    ".zip",
)
_PORTABLE_NAME_HINTS = (
    "appimage",
    "bundle",
    "linux",
    "macos",
    "osx",
    "portable",
    "standalone",
    "win32",
    "win64",
    "windows",
)
_PORTABLE_EXECUTABLE_NAMES = {
    "dupeguru",
    "dupeguru.exe",
    "dupeguru-neo",
    "dupeguru-neo.exe",
}
_NATIVE_EXECUTABLE_MAGICS = {
    b"\x7fELF",
    b"\xca\xfe\xba\xbe",
    b"\xca\xfe\xba\xbf",
    b"\xbe\xba\xfe\xca",
    b"\xbf\xba\xfe\xca",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
}
_MAX_EMBEDDED_LICENSE_FILE_SIZE = 16 * 1024 * 1024
_MAX_EMBEDDED_LICENSE_TOTAL_SIZE = 64 * 1024 * 1024
_MAX_EMBEDDED_LICENSE_MEMBERS = 4096
_MAX_PORTABLE_ARCHIVE_INPUT_SIZE = 512 * 1024 * 1024
_MAX_PORTABLE_ARCHIVE_MEMBERS = 100_000
_MAX_PORTABLE_ARCHIVE_MEMBER_NAME_BYTES = 4096
_MAX_PORTABLE_ARCHIVE_MEMBER_SIZE = 256 * 1024 * 1024
_MAX_PORTABLE_ARCHIVE_TOTAL_SIZE = 1024 * 1024 * 1024
_MAX_PORTABLE_ARCHIVE_COMPRESSION_RATIO = 200
_MAX_PORTABLE_TAR_STREAM_SIZE = 2 * 1024 * 1024 * 1024
_MAX_PORTABLE_TAR_METADATA_MEMBER_SIZE = 16 * 1024 * 1024
_MAX_PORTABLE_ZIP_CENTRAL_DIRECTORY_SIZE = 16 * 1024 * 1024
_MAX_RELEASE_ARCHIVE_DEPTH = 3
_MAX_RELEASE_ARCHIVE_MEMBERS = 100_000
_MAX_RELEASE_ARCHIVE_MEMBER_NAME_BYTES = 4096
_MAX_RELEASE_ARCHIVE_MEMBER_SIZE = 256 * 1024 * 1024
_MAX_RELEASE_ARCHIVE_TOTAL_SIZE = 1024 * 1024 * 1024
_MAX_RELEASE_ARCHIVE_INPUT_SIZE = 512 * 1024 * 1024
_MAX_RELEASE_ARCHIVE_COMPRESSION_RATIO = 200
_MAX_RELEASE_JSON_INSPECTION_SIZE = 8 * 1024 * 1024
_RELEASE_MEMBER_PREFIX_SIZE = 512
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_COMPRESSED_TAR_MAGICS = (b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00")
_SOURCE_COMPANION_SCHEMAS = (
    b"dupeguru.source-companion-manifest",
    b"dupeguru.source-companion-proof",
)
_ALLOWED_WHEEL_NATIVE_EXTENSION = re.compile(
    r"^(?:core/pe/(?:_block|_cache)|qt/pe/_block_qt)" r"(?:\.[A-Za-z0-9_-]+)+\.(?:pyd|so|dylib)$"
)
_WINDOWS_RESERVED_DEVICE = re.compile(r"^(?:aux|clock\$|con|conin\$|conout\$|nul|prn|com[1-9¹²³]|lpt[1-9¹²³])$")
_WINDOWS_FORBIDDEN_NAME_CHARACTERS = frozenset('<>:"|?*')


def _source_date_epoch() -> int:
    raw_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if raw_epoch is None:
        raise RuntimeError("SOURCE_DATE_EPOCH is required for portable archives")
    try:
        epoch = int(raw_epoch)
    except ValueError as error:
        raise RuntimeError("SOURCE_DATE_EPOCH must be an integer") from error
    if not 0 <= epoch <= 0xFFFFFFFF:
        raise RuntimeError("SOURCE_DATE_EPOCH is outside the portable archive range")
    return epoch


def _validate_version(version: str) -> None:
    if _SAFE_VERSION.fullmatch(version) is None:
        raise RuntimeError(f"unsafe release version: {version!r}")


def _platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    raise RuntimeError(f"portable bundles are unsupported on {sys.platform!r}")


def _architecture_name() -> str:
    machine = platform.machine().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    try:
        return aliases[machine]
    except KeyError as error:
        raise RuntimeError(f"unsupported portable architecture: {machine!r}") from error


def portable_archive_name(version: str, platform_name: str, architecture: str) -> str:
    _validate_version(version)
    if platform_name not in {"windows", "macos", "linux"}:
        raise RuntimeError(f"unsupported portable platform: {platform_name!r}")
    if re.fullmatch(r"[0-9a-z_]+", architecture) is None:
        raise RuntimeError(f"unsafe portable architecture: {architecture!r}")
    extension = ".zip" if platform_name == "windows" else ".tar.gz"
    return f"dupeguru-neo-{version}-{platform_name}-{architecture}" f"-unsigned-portable{extension}"


def _iter_tree(root: Path):
    yield root
    yield from sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())


def _archive_path(root: Path, path: Path) -> str:
    if path == root:
        return root.name
    return f"{root.name}/{path.relative_to(root).as_posix()}"


def _normalized_mode(path: Path) -> int:
    if path.is_symlink():
        return 0o777
    if path.is_dir():
        return 0o755
    return 0o755 if path.stat().st_mode & 0o111 else 0o644


def _validate_source_tree(root: Path, *, allow_symlinks: bool) -> None:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError(f"portable bundle root is not a directory: {root}")
    for path in _iter_tree(root):
        if path.is_symlink():
            if not allow_symlinks:
                raise RuntimeError(f"portable ZIP cannot contain a symlink: {path}")
            target = os.readlink(path)
            if Path(target).is_absolute():
                raise RuntimeError(f"portable bundle has an absolute symlink: {path}")
            resolved_target = path.parent.joinpath(target).resolve(strict=True)
            if root not in (resolved_target, *resolved_target.parents):
                raise RuntimeError(f"portable bundle symlink escapes its root: {path}")
        elif not path.is_dir() and not path.is_file():
            raise RuntimeError(f"portable bundle has an unsupported file type: {path}")


def _atomic_target(output_path: Path):
    if output_path.is_symlink():
        raise RuntimeError("portable archive output must not be a symlink")
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() or output_path.is_symlink():
        raise FileExistsError(f"refusing to overwrite portable archive: {output_path}")
    temporary = tempfile.NamedTemporaryFile(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary.close()
    return output_path, Path(temporary.name)


def create_deterministic_zip(root: Path, output_path: Path) -> Path:
    root = root.resolve(strict=True)
    _validate_source_tree(root, allow_symlinks=False)
    output_path, temporary_path = _atomic_target(output_path)
    timestamp = datetime.fromtimestamp(_source_date_epoch(), timezone.utc)
    timestamp = timestamp.replace(
        microsecond=0,
        second=timestamp.second - (timestamp.second % 2),
    )
    if timestamp.year < 1980:
        timestamp = timestamp.replace(year=1980, month=1, day=1, hour=0, minute=0, second=0)
    if timestamp.year > 2107:
        timestamp = timestamp.replace(year=2107, month=12, day=31, hour=23, minute=59, second=58)
    date_time = (
        timestamp.year,
        timestamp.month,
        timestamp.day,
        timestamp.hour,
        timestamp.minute,
        timestamp.second,
    )
    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            for path in _iter_tree(root):
                archive_name = _archive_path(root, path)
                if path.is_dir():
                    archive_name += "/"
                info = zipfile.ZipInfo(archive_name, date_time=date_time)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (_normalized_mode(path) & 0xFFFF) << 16
                if path.is_dir():
                    info.external_attr |= 0x10
                    archive.writestr(info, b"", compresslevel=9)
                else:
                    info.file_size = path.stat().st_size
                    with path.open("rb") as source:
                        with archive.open(
                            info,
                            mode="w",
                            force_zip64=info.file_size >= zipfile.ZIP64_LIMIT,
                        ) as destination:
                            shutil.copyfileobj(
                                source,
                                destination,
                                length=1024 * 1024,
                            )
        os.replace(temporary_path, output_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return output_path


def create_deterministic_tar(root: Path, output_path: Path) -> Path:
    root = root.resolve(strict=True)
    _validate_source_tree(root, allow_symlinks=True)
    output_path, temporary_path = _atomic_target(output_path)
    epoch = _source_date_epoch()
    try:
        with temporary_path.open("wb") as raw_stream:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw_stream,
                mtime=epoch,
            ) as compressed_stream:
                with tarfile.open(
                    mode="w",
                    fileobj=compressed_stream,
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    for path in _iter_tree(root):
                        info = archive.gettarinfo(
                            str(path),
                            arcname=_archive_path(root, path),
                        )
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mtime = epoch
                        info.mode = _normalized_mode(path)
                        info.pax_headers = {}
                        if info.isreg():
                            with path.open("rb") as source:
                                archive.addfile(info, source)
                        else:
                            archive.addfile(info)
        os.replace(temporary_path, output_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return output_path


def _safe_member_name(name: str) -> PurePosixPath:
    if not name or "\0" in name or "\\" in name or name.startswith("/"):
        raise RuntimeError(f"unsafe archive member: {name!r}")
    candidate = PurePosixPath(name.rstrip("/"))
    if (
        not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or candidate.as_posix() != name.rstrip("/")
    ):
        raise RuntimeError(f"unsafe archive member: {name!r}")
    return candidate


def _archive_member_name_size(name: str) -> int:
    try:
        return len(name.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise RuntimeError(f"archive member name is not valid UTF-8: {name!r}") from error


def _target_member_key(member: PurePosixPath, platform_name: str) -> tuple[str, ...]:
    if platform_name == "windows":
        key = []
        for part in member.parts:
            if (
                part[-1] in {".", " "}
                or any(character in _WINDOWS_FORBIDDEN_NAME_CHARACTERS for character in part)
                or any(ord(character) < 32 for character in part)
            ):
                raise RuntimeError(f"unsafe Windows archive member: {member.as_posix()!r}")
            device_stem = part.split(".", 1)[0].casefold()
            if _WINDOWS_RESERVED_DEVICE.fullmatch(device_stem) is not None:
                raise RuntimeError(f"reserved Windows archive member: {member.as_posix()!r}")
            key.append(part.casefold())
        return tuple(key)
    if platform_name == "macos":
        return tuple(
            unicodedata.normalize(
                "NFD",
                unicodedata.normalize("NFD", part).casefold(),
            )
            for part in member.parts
        )
    if platform_name == "linux":
        return member.parts
    raise RuntimeError(f"unsupported portable platform: {platform_name!r}")


def _record_target_member(
    entries: dict[tuple[str, ...], tuple[str, bool]],
    required_directories: set[tuple[str, ...]],
    *,
    member: PurePosixPath,
    canonical_name: str,
    is_directory: bool,
    platform_name: str,
) -> None:
    key = _target_member_key(member, platform_name)
    previous = entries.get(key)
    if previous is not None:
        raise RuntimeError(
            "portable archive members collide on {}: {!r} and {!r}".format(
                platform_name,
                previous[0],
                canonical_name,
            )
        )
    if not is_directory and key in required_directories:
        raise RuntimeError(f"portable archive file conflicts with an existing directory: {canonical_name!r}")
    for index in range(1, len(key)):
        parent = entries.get(key[:index])
        if parent is not None and not parent[1]:
            raise RuntimeError(
                "portable archive member descends from a non-directory: " f"{canonical_name!r} below {parent[0]!r}"
            )
        required_directories.add(key[:index])
    entries[key] = (canonical_name, is_directory)


@dataclass
class _PortableArchiveBudget:
    members: int = 0
    total_uncompressed: int = 0

    def add_member(
        self,
        *,
        name: str,
        size: int,
        compressed_size: int | None,
    ) -> None:
        if _archive_member_name_size(name) > _MAX_PORTABLE_ARCHIVE_MEMBER_NAME_BYTES:
            raise RuntimeError(f"portable archive member name exceeds the safety limit: {name!r}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RuntimeError(f"portable archive member has an invalid size: {name}")
        if size > _MAX_PORTABLE_ARCHIVE_MEMBER_SIZE:
            raise RuntimeError(f"portable archive member exceeds the size limit: {name}")
        self.members += 1
        if self.members > _MAX_PORTABLE_ARCHIVE_MEMBERS:
            raise RuntimeError("portable archive member count exceeds the safety limit")
        self.total_uncompressed += size
        if self.total_uncompressed > _MAX_PORTABLE_ARCHIVE_TOTAL_SIZE:
            raise RuntimeError("portable archive uncompressed bytes exceed the safety limit")
        if compressed_size is not None:
            if isinstance(compressed_size, bool) or not isinstance(compressed_size, int) or compressed_size < 0:
                raise RuntimeError(f"portable archive member has an invalid compressed size: {name}")
            if size and compressed_size == 0:
                raise RuntimeError(f"portable archive member has an invalid compressed size: {name}")
            if size > compressed_size * _MAX_PORTABLE_ARCHIVE_COMPRESSION_RATIO:
                raise RuntimeError(f"portable archive member exceeds the compression-ratio limit: {name}")

    def verify_container_ratio(self, input_size: int) -> None:
        if isinstance(input_size, bool) or not isinstance(input_size, int) or input_size <= 0:
            raise RuntimeError("portable archive has an invalid input size")
        if self.total_uncompressed > input_size * _MAX_PORTABLE_ARCHIVE_COMPRESSION_RATIO:
            raise RuntimeError("portable archive exceeds the compression-ratio limit")


def _preflight_portable_zip(archive_path: Path) -> int:
    try:
        with archive_path.open("rb") as stream:
            end_record = zipfile._EndRecData(stream)
    except (OSError, zipfile.BadZipFile) as error:
        raise RuntimeError("portable ZIP has an unreadable central directory") from error
    if end_record is None:
        raise RuntimeError("portable ZIP has no valid central directory")
    declared_members = end_record[zipfile._ECD_ENTRIES_TOTAL]
    central_directory_size = end_record[zipfile._ECD_SIZE]
    if (
        isinstance(declared_members, bool)
        or not isinstance(declared_members, int)
        or declared_members < 0
        or declared_members > _MAX_PORTABLE_ARCHIVE_MEMBERS
    ):
        raise RuntimeError("portable archive member count exceeds the safety limit")
    if (
        isinstance(central_directory_size, bool)
        or not isinstance(central_directory_size, int)
        or central_directory_size < 0
        or central_directory_size > _MAX_PORTABLE_ZIP_CENTRAL_DIRECTORY_SIZE
    ):
        raise RuntimeError("portable ZIP central directory exceeds the safety limit")
    return declared_members


def _verify_portable_gzip_stream(archive_path: Path, input_size: int) -> None:
    total_size = 0
    physical_members = 0

    def account(chunk: bytes) -> None:
        nonlocal total_size
        total_size += len(chunk)
        if total_size > _MAX_PORTABLE_TAR_STREAM_SIZE:
            raise RuntimeError("portable TAR stream exceeds the safety limit")
        if total_size > input_size * _MAX_PORTABLE_ARCHIVE_COMPRESSION_RATIO:
            raise RuntimeError("portable archive exceeds the compression-ratio limit")

    try:
        with gzip.open(archive_path, "rb") as stream:
            zero_headers = 0
            while True:
                header = stream.read(512)
                if not header:
                    raise RuntimeError("portable TAR stream has no complete end marker")
                account(header)
                if len(header) != 512:
                    raise RuntimeError("portable TAR stream has a truncated header")
                if not any(header):
                    zero_headers += 1
                    if zero_headers < 2:
                        continue
                    for trailing in iter(lambda: stream.read(1024 * 1024), b""):
                        account(trailing)
                        if any(trailing):
                            raise RuntimeError("portable TAR stream has data after its end marker")
                    break
                if zero_headers:
                    raise RuntimeError("portable TAR stream has an invalid end marker")
                physical_members += 1
                if physical_members > _MAX_PORTABLE_ARCHIVE_MEMBERS:
                    raise RuntimeError("portable archive member count exceeds the safety limit")
                try:
                    member_size = tarfile.nti(header[124:136])
                except tarfile.InvalidHeaderError as error:
                    raise RuntimeError("portable TAR stream has an invalid member size") from error
                if (
                    isinstance(member_size, bool)
                    or not isinstance(member_size, int)
                    or member_size < 0
                    or member_size > _MAX_PORTABLE_ARCHIVE_MEMBER_SIZE
                ):
                    raise RuntimeError("portable TAR physical member exceeds the size limit")
                if header[156:157] in {b"g", b"x", b"K", b"L"} and member_size > _MAX_PORTABLE_TAR_METADATA_MEMBER_SIZE:
                    raise RuntimeError("portable TAR metadata member exceeds the safety limit")
                remaining = ((member_size + 511) // 512) * 512
                while remaining:
                    chunk = stream.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise RuntimeError("portable TAR stream has a truncated member")
                    account(chunk)
                    remaining -= len(chunk)
    except (EOFError, OSError, gzip.BadGzipFile) as error:
        raise RuntimeError("portable TAR has an invalid gzip stream") from error


def _safe_link_target(member_name: str, link_name: str, *, symbolic: bool) -> None:
    if not link_name or "\0" in link_name or "\\" in link_name or link_name.startswith("/"):
        raise RuntimeError(f"unsafe archive link target: {link_name!r}")
    base = posixpath.dirname(member_name) if symbolic else ""
    normalized = posixpath.normpath(posixpath.join(base, link_name))
    target = _safe_member_name(normalized)
    member_root = _safe_member_name(member_name).parts[0]
    if target.parts[0] != member_root:
        raise RuntimeError(f"archive link escapes its root: {member_name!r}")


def _expected_archive_members(
    platform_name: str,
) -> tuple[str, ...]:
    if platform_name == "macos":
        root = "dupeguru-neo.app"
        data_root = f"{root}/Contents/Resources"
        executable = f"{root}/Contents/MacOS/dupeguru-neo"
        executables = (executable,)
    else:
        root = "dupeguru-neo"
        data_root = f"{root}/_internal"
        suffix = ".exe" if platform_name == "windows" else ""
        executable = f"{root}/dupeguru-neo{suffix}"
        executables = (executable,)
        if platform_name == "windows":
            executables += (f"{root}/dupeguru.exe",)
    return (
        *executables,
        f"{data_root}/LICENSE",
        f"{data_root}/PORTABLE-NOTICE.txt",
        f"{data_root}/THIRD_PARTY_NOTICES.md",
        f"{data_root}/hscommon/LICENSE",
        f"{data_root}/requirements-release.txt",
        f"{data_root}/release-sources.json",
        f"{data_root}/THIRD-PARTY-LICENSES/index.json",
        f"{data_root}/THIRD-PARTY-LICENSES/index.txt",
        f"{data_root}/FROZEN-RUNTIME-LICENSES/index.json",
        f"{data_root}/FROZEN-RUNTIME-LICENSES/index.txt",
    )


def _portable_data_root(platform_name: str) -> str:
    if platform_name == "macos":
        return "dupeguru-neo.app/Contents/Resources"
    return "dupeguru-neo/_internal"


def _verify_embedded_license_inventory(
    archive_path: Path,
    *,
    platform_name: str,
    extension: str,
    lock_path: Path,
    installation_root: Path | None = None,
) -> None:
    if lock_path.is_symlink() or not lock_path.is_file():
        raise RuntimeError("portable verification lock must be a regular file")
    lock_path = lock_path.resolve(strict=True)
    data_root = _portable_data_root(platform_name)
    inventory_prefix = f"{data_root}/THIRD-PARTY-LICENSES/"
    frozen_runtime_prefix = f"{data_root}/FROZEN-RUNTIME-LICENSES/"
    embedded_lock_name = f"{data_root}/requirements-release.txt"
    embedded_source_lock_name = f"{data_root}/release-sources.json"
    source_lock_path = lock_path.with_name("release-sources.json")
    if source_lock_path.is_symlink() or not source_lock_path.is_file():
        raise RuntimeError("portable verification source lock must be a regular file")
    source_lock_path = source_lock_path.resolve(strict=True)
    system = {
        "linux": "Linux",
        "macos": "Darwin",
        "windows": "Windows",
    }[platform_name]
    with tempfile.TemporaryDirectory(prefix="dupeguru-license-inventory-") as temporary:
        temporary_root = Path(temporary)
        inventory_root = temporary_root.joinpath("THIRD-PARTY-LICENSES")
        frozen_runtime_root = temporary_root.joinpath("FROZEN-RUNTIME-LICENSES")
        embedded_lock = None
        embedded_source_lock = None
        total_size = 0
        selected_members = set()

        def selected_member(raw_name: str) -> str | None:
            name = raw_name.rstrip("/")
            if (
                name not in {embedded_lock_name, embedded_source_lock_name}
                and not name.startswith(inventory_prefix)
                and not name.startswith(frozen_runtime_prefix)
            ):
                return None
            if _archive_member_name_size(raw_name) > _MAX_RELEASE_ARCHIVE_MEMBER_NAME_BYTES:
                raise RuntimeError(f"portable license inventory member name is too long: {name}")
            _safe_member_name(raw_name)
            if name in selected_members:
                raise RuntimeError(f"duplicate portable license inventory member: {name}")
            if len(selected_members) >= _MAX_EMBEDDED_LICENSE_MEMBERS:
                raise RuntimeError("portable license inventory contains too many members")
            selected_members.add(name)
            return name

        def accept_directory(name: str, size: int) -> None:
            if name in {embedded_lock_name, embedded_source_lock_name}:
                raise RuntimeError(f"portable license inventory member is not a file: {name}")
            if size != 0:
                raise RuntimeError(f"portable license inventory directory has a non-zero size: {name}")

        def accept_file(name: str, content: bytes) -> None:
            nonlocal embedded_lock, embedded_source_lock, total_size
            if not 0 < len(content) <= _MAX_EMBEDDED_LICENSE_FILE_SIZE:
                raise RuntimeError(f"portable license inventory member has an invalid size: {name}")
            total_size += len(content)
            if total_size > _MAX_EMBEDDED_LICENSE_TOTAL_SIZE:
                raise RuntimeError("portable license inventory exceeds its size limit")
            if name == embedded_lock_name:
                embedded_lock = content
                return
            if name == embedded_source_lock_name:
                embedded_source_lock = content
                return
            if name.startswith(inventory_prefix):
                relative_name = name.removeprefix(inventory_prefix)
                destination_root = inventory_root
            elif name.startswith(frozen_runtime_prefix):
                relative_name = name.removeprefix(frozen_runtime_prefix)
                destination_root = frozen_runtime_root
            else:
                return
            relative = _safe_member_name(relative_name)
            destination = destination_root.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)

        if extension == ".zip":
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    name = selected_member(info.filename)
                    if name is None:
                        continue
                    mode = info.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if info.is_dir():
                        if file_type not in {0, stat.S_IFDIR}:
                            raise RuntimeError(
                                "portable license inventory directory has an " f"invalid file type: {name}"
                            )
                        accept_directory(name, info.file_size)
                        continue
                    if file_type not in {0, stat.S_IFREG}:
                        raise RuntimeError(f"portable license inventory member is not a regular file: {name}")
                    if not 0 < info.file_size <= _MAX_EMBEDDED_LICENSE_FILE_SIZE:
                        raise RuntimeError("portable license inventory member has an invalid " f"size: {name}")
                    accept_file(name, archive.read(info))
        else:
            with tarfile.open(archive_path, mode="r:gz") as archive:
                for member in archive:
                    name = selected_member(member.name)
                    if name is None:
                        continue
                    if member.isdir():
                        accept_directory(name, member.size)
                        continue
                    if not member.isfile():
                        raise RuntimeError(f"portable license inventory member is not a file: {name}")
                    if not 0 < member.size <= _MAX_EMBEDDED_LICENSE_FILE_SIZE:
                        raise RuntimeError("portable license inventory member has an invalid " f"size: {name}")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise RuntimeError(f"cannot read portable license inventory member: {name}")
                    accept_file(name, stream.read())
        if embedded_lock is None:
            raise RuntimeError("portable bundle does not contain its dependency lock")
        if embedded_lock != lock_path.read_bytes():
            raise RuntimeError("portable dependency lock differs from the release lock")
        if embedded_source_lock is None:
            raise RuntimeError("portable bundle does not contain its upstream source lock")
        if embedded_source_lock != source_lock_path.read_bytes():
            raise RuntimeError("portable source lock differs from the release source lock")
        verify_inventory(
            inventory_root,
            lock_path,
            source_lock_path,
            expected_system=system,
            installation_root=installation_root,
        )
        verify_frozen_runtime_inventory(
            frozen_runtime_root,
            source_lock_path,
            expected_system=system,
        )


def verify_portable_archive(
    archive_path: Path,
    lock_path: Path | None = None,
    *,
    installation_root: Path | None = None,
) -> None:
    if archive_path.is_symlink():
        raise RuntimeError(f"portable archive must not be a symlink: {archive_path}")
    archive_path = archive_path.resolve(strict=True)
    if not archive_path.is_file():
        raise RuntimeError(f"portable archive must be a regular file: {archive_path}")
    archive_size = archive_path.stat().st_size
    if not 0 < archive_size <= _MAX_PORTABLE_ARCHIVE_INPUT_SIZE:
        raise RuntimeError("portable archive input size is outside the safety limit: " f"{archive_size} bytes")
    match = _ARCHIVE_NAME.fullmatch(archive_path.name)
    if match is None:
        raise RuntimeError(f"unexpected portable archive name: {archive_path.name}")
    platform_name = match.group("platform")
    extension = match.group("extension")
    expected_extension = ".zip" if platform_name == "windows" else ".tar.gz"
    if extension != expected_extension:
        raise RuntimeError(f"wrong archive format for {platform_name}")
    expected_members = _expected_archive_members(platform_name)
    epoch = _source_date_epoch()
    names = set()
    roots = set()
    target_entries = {}
    required_target_directories = set()
    budget = _PortableArchiveBudget()
    if extension == ".zip":
        timestamp = datetime.fromtimestamp(epoch, timezone.utc)
        timestamp = timestamp.replace(
            microsecond=0,
            second=timestamp.second - (timestamp.second % 2),
        )
        if timestamp.year < 1980:
            timestamp = timestamp.replace(year=1980, month=1, day=1, hour=0, minute=0, second=0)
        if timestamp.year > 2107:
            timestamp = timestamp.replace(year=2107, month=12, day=31, hour=23, minute=59, second=58)
        expected_date_time = (
            timestamp.year,
            timestamp.month,
            timestamp.day,
            timestamp.hour,
            timestamp.minute,
            timestamp.second,
        )
        declared_members = _preflight_portable_zip(archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) != declared_members:
                raise RuntimeError("portable ZIP central-directory member count is inconsistent")
            for info in infos:
                member = _safe_member_name(info.filename)
                canonical_name = info.filename.rstrip("/")
                if canonical_name in names:
                    raise RuntimeError(f"duplicate archive member: {info.filename}")
                names.add(canonical_name)
                roots.add(member.parts[0])
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                is_directory = info.is_dir()
                if is_directory:
                    if file_type not in {0, stat.S_IFDIR}:
                        raise RuntimeError(f"portable ZIP directory has an invalid type: {info.filename}")
                    if info.file_size != 0:
                        raise RuntimeError(f"portable ZIP directory has a non-zero size: {info.filename}")
                    size = 0
                    compressed_size = None
                else:
                    if stat.S_ISLNK(mode):
                        raise RuntimeError(f"portable ZIP contains a symlink: {info.filename}")
                    if file_type not in {0, stat.S_IFREG}:
                        raise RuntimeError(f"portable ZIP contains an unsupported member: {info.filename}")
                    size = info.file_size
                    compressed_size = info.compress_size
                budget.add_member(
                    name=info.filename,
                    size=size,
                    compressed_size=compressed_size,
                )
                _record_target_member(
                    target_entries,
                    required_target_directories,
                    member=member,
                    canonical_name=canonical_name,
                    is_directory=is_directory,
                    platform_name=platform_name,
                )
                if info.flag_bits & 0x1:
                    raise RuntimeError(f"portable ZIP contains an encrypted member: {info.filename}")
                if info.date_time != expected_date_time:
                    raise RuntimeError(f"non-deterministic ZIP timestamp: {info.filename}")
            corrupt = archive.testzip()
            if corrupt is not None:
                raise RuntimeError(f"portable ZIP has a corrupt member: {corrupt}")
        budget.verify_container_ratio(archive_size)
    else:
        with archive_path.open("rb") as raw_archive:
            gzip_header = raw_archive.read(10)
        if (
            len(gzip_header) != 10
            or gzip_header[:2] != b"\x1f\x8b"
            or int.from_bytes(gzip_header[4:8], "little") != epoch
        ):
            raise RuntimeError("compressed tar has a non-deterministic gzip timestamp")
        _verify_portable_gzip_stream(archive_path, archive_size)
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member_info in archive:
                member = _safe_member_name(member_info.name)
                canonical_name = member_info.name.rstrip("/")
                if canonical_name in names:
                    raise RuntimeError(f"duplicate archive member: {member_info.name}")
                names.add(canonical_name)
                roots.add(member.parts[0])
                if member_info.isdir():
                    if member_info.size != 0:
                        raise RuntimeError(f"portable TAR directory has a non-zero size: {member_info.name}")
                    size = 0
                elif member_info.issym() or member_info.islnk():
                    if member_info.size != 0:
                        raise RuntimeError(f"portable TAR link has a non-zero size: {member_info.name}")
                    _safe_link_target(
                        member_info.name,
                        member_info.linkname,
                        symbolic=member_info.issym(),
                    )
                    size = 0
                elif member_info.isfile():
                    size = member_info.size
                else:
                    raise RuntimeError(f"unsupported archive member type: {member_info.name}")
                budget.add_member(
                    name=member_info.name,
                    size=size,
                    compressed_size=None,
                )
                _record_target_member(
                    target_entries,
                    required_target_directories,
                    member=member,
                    canonical_name=canonical_name,
                    is_directory=member_info.isdir(),
                    platform_name=platform_name,
                )
                if (
                    member_info.mtime != epoch
                    or member_info.uid != 0
                    or member_info.gid != 0
                    or member_info.uname
                    or member_info.gname
                ):
                    raise RuntimeError(f"non-deterministic tar metadata: {member_info.name}")
                if member_info.isfile():
                    stream = archive.extractfile(member_info)
                    if stream is None:
                        raise RuntimeError(f"cannot read archive member: {member_info.name}")
                    read_size = 0
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        read_size += len(chunk)
                    if read_size != member_info.size:
                        raise RuntimeError(f"incomplete archive member: {member_info.name}")
        budget.verify_container_ratio(archive_size)
    if len(roots) != 1:
        raise RuntimeError(f"portable archive must have one root, found {sorted(roots)}")
    missing = sorted(set(expected_members) - names)
    if missing:
        raise RuntimeError(f"portable archive is missing required members: {missing}")
    if lock_path is not None:
        _verify_embedded_license_inventory(
            archive_path,
            platform_name=platform_name,
            extension=extension,
            lock_path=lock_path,
            installation_root=installation_root,
        )


def _frozen_executable(bundle_root: Path, platform_name: str) -> Path:
    if platform_name == "windows":
        return bundle_root.joinpath("dupeguru-neo.exe")
    if platform_name == "macos":
        return bundle_root.joinpath("Contents", "MacOS", "dupeguru-neo")
    return bundle_root.joinpath("dupeguru-neo")


def _frozen_windows_cli(bundle_root: Path) -> Path:
    return bundle_root.joinpath("dupeguru.exe")


def _run_frozen(command: list[Path | str], *, env: dict[str, str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [str(item) for item in command],
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"frozen smoke timed out: {command[0]}") from error


def _smoke_frozen_windows_cli(
    bundle_root: Path,
    version: str,
    *,
    environment: dict[str, str],
) -> None:
    executable = _frozen_windows_cli(bundle_root).resolve(strict=True)
    if not executable.is_file():
        raise RuntimeError(f"frozen CLI executable is not a file: {executable}")
    version_result = _run_frozen([executable, "--version"], env=environment)
    if version_result.returncode != 0:
        raise RuntimeError(
            f"frozen CLI --version failed ({version_result.returncode}): " f"{version_result.stderr[-2000:]}"
        )
    if version_result.stdout.strip() != version:
        raise RuntimeError(
            "frozen CLI --version did not report the release version: " f"{version_result.stdout.strip()!r}"
        )

    doctor_result = _run_frozen([executable, "doctor"], env=environment)
    if doctor_result.returncode != 0:
        raise RuntimeError(f"frozen CLI doctor failed ({doctor_result.returncode}): " f"{doctor_result.stderr[-2000:]}")
    try:
        doctor = json.loads(doctor_result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("frozen CLI doctor did not emit JSON") from error
    if doctor.get("schema") != "dupeguru.doctor-report" or doctor.get("pyqt_imported") is not False:
        raise RuntimeError("frozen CLI doctor did not prove the Qt-free CLI boundary")

    schema_result = _run_frozen(
        [executable, "schema", "deletion-plan"],
        env=environment,
    )
    if schema_result.returncode != 0:
        raise RuntimeError(f"frozen CLI schema failed ({schema_result.returncode}): " f"{schema_result.stderr[-2000:]}")
    try:
        schema = json.loads(schema_result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("frozen CLI schema did not emit JSON") from error
    if (
        schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        or schema.get("$id") != "urn:dupeguru-neo:schema:deletion-plan:1"
    ):
        raise RuntimeError("frozen CLI schema did not emit the bundled deletion-plan schema")


def smoke_frozen_bundle(bundle_root: Path, platform_name: str, version: str) -> None:
    executable = _frozen_executable(bundle_root, platform_name).resolve(strict=True)
    if not executable.is_file():
        raise RuntimeError(f"frozen executable is not a file: {executable}")
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONUTF8"] = "1"
    version_result = _run_frozen([executable, "--version"], env=environment)
    if version_result.returncode != 0:
        raise RuntimeError(
            f"frozen --version failed ({version_result.returncode}): " f"{version_result.stderr[-2000:]}"
        )
    rendered_version = version_result.stdout.strip()
    if platform_name != "windows" and version not in rendered_version.splitlines():
        raise RuntimeError(f"frozen --version did not report {version!r}: {rendered_version!r}")
    self_test_result = _run_frozen([executable, "--self-test"], env=environment)
    if self_test_result.returncode != 0:
        raise RuntimeError(
            f"frozen --self-test failed ({self_test_result.returncode}): " f"{self_test_result.stderr[-2000:]}"
        )
    if platform_name == "windows":
        _smoke_frozen_windows_cli(
            bundle_root,
            version,
            environment=environment,
        )


def verify_unsigned_native_trust(bundle_root: Path, platform_name: str) -> None:
    if platform_name == "windows":
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is None:
            raise RuntimeError("PowerShell is required to verify Authenticode absence")
        for executable in (
            _frozen_executable(bundle_root, platform_name),
            _frozen_windows_cli(bundle_root),
        ):
            executable = executable.resolve(strict=True)
            environment = os.environ.copy()
            environment["DUPEGURU_PORTABLE_EXECUTABLE"] = str(executable)
            result = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    ("(Get-AuthenticodeSignature -LiteralPath " "$env:DUPEGURU_PORTABLE_EXECUTABLE).Status.ToString()"),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=environment,
            )
            if result.returncode != 0 or result.stdout.strip() != "NotSigned":
                raise RuntimeError(
                    "portable Windows executable unexpectedly has or cannot prove absence "
                    "of Authenticode trust: "
                    f"{result.stdout.strip()!r} {result.stderr[-1000:]}"
                )
    elif platform_name == "macos":
        executable = _frozen_executable(bundle_root, platform_name).resolve(strict=True)
        result = subprocess.run(
            ["codesign", "--display", "--verbose=4", str(bundle_root)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        details = f"{result.stdout}\n{result.stderr}"
        if result.returncode != 0 or "Signature=adhoc" not in details or "Authority=" in details:
            raise RuntimeError("portable macOS application is not provably ad-hoc-only: " f"{details[-2000:]}")


def _pyinstaller_arguments(
    project_root: Path,
    build_root: Path,
    platform_name: str,
    license_inventory: Path,
    frozen_runtime_inventory: Path,
) -> list[str]:
    data_separator = os.pathsep
    arguments = [
        "--name=dupeguru-neo",
        "--onedir",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--noupx",
        "--log-level=WARN",
        f"--distpath={build_root.joinpath('dist')}",
        f"--workpath={build_root.joinpath('work')}",
        f"--specpath={build_root.joinpath('spec')}",
        f"--add-data={project_root.joinpath('build', 'locale')}{data_separator}locale",
        f"--add-data={project_root.joinpath('build', 'help')}{data_separator}help",
        f"--add-data={project_root.joinpath('LICENSE')}{data_separator}.",
        f"--add-data={project_root.joinpath('docs', 'PORTABLE-NOTICE.txt')}{data_separator}.",
        f"--add-data={project_root.joinpath('THIRD_PARTY_NOTICES.md')}{data_separator}.",
        (f"--add-data={project_root.joinpath('hscommon', 'LICENSE')}" f"{data_separator}hscommon"),
        f"--add-data={project_root.joinpath('requirements-release.txt')}{data_separator}.",
        f"--add-data={project_root.joinpath('release-sources.json')}{data_separator}.",
        f"--add-data={license_inventory}{data_separator}THIRD-PARTY-LICENSES",
        (f"--add-data={frozen_runtime_inventory}" f"{data_separator}FROZEN-RUNTIME-LICENSES"),
        "--collect-data=images",
        "--hidden-import=qt.app",
    ]
    if platform_name == "windows":
        arguments.append(f"--icon={project_root.joinpath('images', 'dgse_logo.ico')}")
    elif platform_name == "macos":
        arguments.extend(
            [
                f"--icon={project_root.joinpath('images', 'dupeguru.icns')}",
                "--osx-bundle-identifier=io.github.AiWithYou.dupeguru_neo",
            ]
        )
    arguments.append(str(project_root.joinpath("run.py")))
    return arguments


def _windows_cli_pyinstaller_arguments(
    project_root: Path,
    build_root: Path,
) -> list[str]:
    return [
        "--name=dupeguru",
        "--onefile",
        "--console",
        "--noconfirm",
        "--clean",
        "--noupx",
        "--log-level=WARN",
        f"--distpath={build_root.joinpath('cli-dist')}",
        f"--workpath={build_root.joinpath('cli-work')}",
        f"--specpath={build_root.joinpath('cli-spec')}",
        "--exclude-module=PyQt6",
        "--exclude-module=qt",
        f"--icon={project_root.joinpath('images', 'dgse_logo.ico')}",
        str(project_root.joinpath("run_cli.py")),
    ]


def build_portable_bundle(version: str, output_directory: Path, build_root: Path) -> Path:
    _validate_version(version)
    _source_date_epoch()
    platform_name = _platform_name()
    architecture = _architecture_name()
    project_root = Path.cwd().resolve()
    for required in (
        Path("run.py"),
        Path("run_cli.py"),
        Path("LICENSE"),
        Path("THIRD_PARTY_NOTICES.md"),
        Path("hscommon/LICENSE"),
        Path("requirements-release.txt"),
        Path("release-sources.json"),
        Path("docs/PORTABLE-NOTICE.txt"),
        Path("build/locale"),
        Path("build/help"),
    ):
        if not required.exists():
            raise RuntimeError(f"portable build input is missing: {required}")
    build_root = build_root.resolve()
    build_root.mkdir(parents=True, exist_ok=True)
    for child in ("dist", "work", "spec"):
        build_root.joinpath(child).mkdir(parents=True, exist_ok=True)
    if platform_name == "windows":
        for child in ("cli-dist", "cli-work", "cli-spec"):
            build_root.joinpath(child).mkdir(parents=True, exist_ok=True)
    lock_path = project_root.joinpath("requirements-release.txt")
    source_lock_path = project_root.joinpath("release-sources.json")
    license_inventory = generate_inventory(
        lock_path,
        source_lock_path,
        build_root.joinpath("THIRD-PARTY-LICENSES"),
    )
    frozen_runtime_inventory = generate_frozen_runtime_inventory(
        source_lock_path,
        build_root.joinpath("FROZEN-RUNTIME-LICENSES"),
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            *_pyinstaller_arguments(
                project_root,
                build_root,
                platform_name,
                license_inventory,
                frozen_runtime_inventory,
            ),
        ],
        check=True,
    )
    if platform_name == "windows":
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                *_windows_cli_pyinstaller_arguments(project_root, build_root),
            ],
            check=True,
        )
    if platform_name == "macos":
        bundle_root = build_root.joinpath("dist", "dupeguru-neo.app")
    else:
        bundle_root = build_root.joinpath("dist", "dupeguru-neo")
    bundle_root = bundle_root.resolve(strict=True)
    if platform_name == "windows":
        cli_source = build_root.joinpath("cli-dist", "dupeguru.exe")
        if cli_source.is_symlink() or not cli_source.is_file():
            raise RuntimeError(f"frozen CLI output is not a regular file: {cli_source}")
        cli_destination = bundle_root.joinpath("dupeguru.exe")
        if cli_destination.exists() or cli_destination.is_symlink():
            raise RuntimeError(f"refusing to overwrite frozen CLI destination: {cli_destination}")
        shutil.copy2(cli_source, cli_destination)
    if platform_name == "macos":
        bundle_data_root = bundle_root.joinpath("Contents", "Resources")
    else:
        bundle_data_root = bundle_root.joinpath("_internal")
    verify_inventory(
        bundle_data_root.joinpath("THIRD-PARTY-LICENSES"),
        lock_path,
        source_lock_path,
        expected_system={
            "linux": "Linux",
            "macos": "Darwin",
            "windows": "Windows",
        }[platform_name],
    )
    verify_frozen_runtime_inventory(
        bundle_data_root.joinpath("FROZEN-RUNTIME-LICENSES"),
        source_lock_path,
        expected_system={
            "linux": "Linux",
            "macos": "Darwin",
            "windows": "Windows",
        }[platform_name],
    )
    if bundle_data_root.joinpath("requirements-release.txt").read_bytes() != (lock_path.read_bytes()):
        raise RuntimeError("frozen dependency lock differs from the release lock")
    if bundle_data_root.joinpath("release-sources.json").read_bytes() != (source_lock_path.read_bytes()):
        raise RuntimeError("frozen upstream source lock differs from the release lock")
    verify_unsigned_native_trust(bundle_root, platform_name)
    smoke_frozen_bundle(bundle_root, platform_name, version)
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory.joinpath(portable_archive_name(version, platform_name, architecture))
    if platform_name == "windows":
        create_deterministic_zip(bundle_root, output_path)
    else:
        create_deterministic_tar(bundle_root, output_path)
    verify_portable_archive(output_path, lock_path)
    return output_path


def _archive_suffix(name: str) -> str | None:
    lowered = name.lower()
    return next((suffix for suffix in _ARCHIVE_SUFFIXES if lowered.endswith(suffix)), None)


def _portable_name_hint(name: str) -> bool:
    lowered = name.lower()
    normalized = re.sub(r"[^a-z0-9]+", "", lowered)
    return "dupeguru" in normalized and any(hint in lowered for hint in _PORTABLE_NAME_HINTS)


def _source_companion_name_hint(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", name.lower())
    return "sourcecompanion" in normalized or normalized == "sourcecompanionsha256sums"


def _has_tar_header(prefix: bytes) -> bool:
    if len(prefix) < 512 or not prefix[:100].rstrip(b"\0"):
        return False
    raw_checksum = prefix[148:156].strip(b"\0 ")
    if not raw_checksum or any(byte not in b"01234567" for byte in raw_checksum):
        return False
    expected = int(raw_checksum, 8)
    header = bytearray(prefix[:512])
    header[148:156] = b" " * 8
    return sum(header) == expected


def _archive_magic_kind(prefix: bytes) -> str | None:
    if prefix.startswith(_ZIP_MAGICS):
        return "zip"
    if prefix.startswith(_COMPRESSED_TAR_MAGICS):
        return "tar"
    if _has_tar_header(prefix):
        return "tar"
    return None


def _archive_kind(path: Path) -> str | None:
    if path.stat().st_size > _MAX_RELEASE_ARCHIVE_INPUT_SIZE:
        raise RuntimeError(f"release artifact exceeds the archive inspection limit: {path.name}")
    with path.open("rb") as stream:
        prefix = stream.read(_RELEASE_MEMBER_PREFIX_SIZE)
    magic_kind = _archive_magic_kind(prefix)
    if zipfile.is_zipfile(path):
        return "zip"
    if tarfile.is_tarfile(path):
        return "tar"
    if magic_kind is not None or _archive_suffix(path.name) is not None:
        raise RuntimeError(f"cannot prove release archive is portable-free: {path.name}")
    return None


def _member_parts(name: str) -> tuple[str, ...]:
    if not name or "\0" in name or "\\" in name or name.startswith("/"):
        raise RuntimeError(f"cannot prove release archive member is safe: {name!r}")
    try:
        encoded_name = name.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RuntimeError(f"cannot prove release archive member is UTF-8: {name!r}") from error
    if len(encoded_name) > _MAX_RELEASE_ARCHIVE_MEMBER_NAME_BYTES:
        raise RuntimeError("release archive member name exceeds the safety limit")
    candidate = PurePosixPath(name.rstrip("/"))
    if (
        not candidate.parts
        or candidate.as_posix() != name.rstrip("/")
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise RuntimeError(f"cannot prove release archive member is safe: {name!r}")
    return tuple(part.casefold() for part in candidate.parts)


@dataclass
class _ReleaseArchiveBudget:
    members: int = 0
    total_uncompressed: int = 0

    def add_member(
        self,
        *,
        name: str,
        size: int,
        compressed_size: int | None,
    ) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RuntimeError(f"release archive member has an invalid size: {name}")
        if size > _MAX_RELEASE_ARCHIVE_MEMBER_SIZE:
            raise RuntimeError(f"release archive member exceeds the size limit: {name}")
        self.members += 1
        if self.members > _MAX_RELEASE_ARCHIVE_MEMBERS:
            raise RuntimeError("release archive member count exceeds the safety limit")
        self.total_uncompressed += size
        if self.total_uncompressed > _MAX_RELEASE_ARCHIVE_TOTAL_SIZE:
            raise RuntimeError("release archive uncompressed bytes exceed the safety limit")
        if size and compressed_size is not None:
            if compressed_size <= 0:
                raise RuntimeError(f"release archive member has an invalid compressed size: {name}")
            if size > compressed_size * _MAX_RELEASE_ARCHIVE_COMPRESSION_RATIO:
                raise RuntimeError(f"release archive member exceeds the compression-ratio limit: {name}")


def _read_member_prefix(stream, size: int, name: str) -> bytes:
    expected = min(size, _RELEASE_MEMBER_PREFIX_SIZE)
    chunks = []
    remaining = expected
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    prefix = b"".join(chunks)
    if len(prefix) != expected:
        raise RuntimeError(f"cannot prove release archive member is complete: {name}")
    return prefix


def _read_complete_member(stream, size: int, prefix: bytes, name: str) -> bytes:
    remaining = size - len(prefix)
    chunks = [prefix]
    while remaining:
        chunk = stream.read(min(remaining, 1024 * 1024))
        if not chunk:
            raise RuntimeError(f"cannot prove release archive member is complete: {name}")
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    if len(content) != size:
        raise RuntimeError(f"cannot prove release archive member is complete: {name}")
    return content


def _contains_source_companion_document(content: bytes) -> bool:
    if not any(schema in content for schema in _SOURCE_COMPANION_SCHEMAS):
        return False
    try:
        document = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    schema = document.get("schema") if isinstance(document, dict) else None
    return isinstance(schema, str) and schema.encode("utf-8") in _SOURCE_COMPANION_SCHEMAS


def _allowed_top_level_wheel_native(name: str, artifact_name: str) -> bool:
    return artifact_name.lower().endswith(".whl") and _ALLOWED_WHEEL_NATIVE_EXTENSION.fullmatch(name) is not None


def _native_magic_is_forbidden(
    prefix: bytes,
    *,
    member_name: str,
    artifact_name: str,
    depth: int,
) -> bool:
    if not (prefix[:2] == b"MZ" or prefix[:4] in _NATIVE_EXECUTABLE_MAGICS):
        return False
    return depth > 0 or not _allowed_top_level_wheel_native(member_name, artifact_name)


def _nested_archive_kind(content: bytes, name: str) -> str:
    stream = io.BytesIO(content)
    if zipfile.is_zipfile(stream):
        return "zip"
    stream.seek(0)
    try:
        with tarfile.open(fileobj=stream, mode="r:*"):
            return "tar"
    except (EOFError, OSError, tarfile.TarError):
        pass
    raise RuntimeError(f"cannot prove nested release archive is portable-free: {name}")


def _archive_like_member(name: str, prefix: bytes) -> bool:
    return _archive_magic_kind(prefix) is not None or _archive_suffix(PurePosixPath(name).name) is not None


def _members_contain_portable_bundle(
    members: list[tuple[str, bool, int]],
) -> bool:
    normalized_members = []
    casefolded_names = set()
    for name, is_directory, mode in members:
        parts = _member_parts(name)
        casefolded_name = "/".join(parts)
        if casefolded_name in casefolded_names:
            raise RuntimeError(f"release archive has a case-insensitive member collision: {name}")
        casefolded_names.add(casefolded_name)
        normalized_members.append((parts, is_directory, mode))
        if parts[-1] == "source-manifest.json":
            return True
        if any(_source_companion_name_hint(part) for part in parts[:-1]):
            return True
        if _archive_suffix(parts[-1]) is not None and _source_companion_name_hint(parts[-1]):
            return True
        if _archive_suffix(parts[-1]) is not None and _portable_name_hint(parts[-1]):
            return True
        if any(part.endswith(".app") for part in parts):
            return True
        if Path(parts[-1]).suffix.lower() in _DISALLOWED_NATIVE_SUFFIXES:
            return True

    for parts, is_directory, mode in normalized_members:
        if is_directory:
            continue
        basename = parts[-1]
        if basename in {"dupeguru.exe", "dupeguru-neo.exe"}:
            return True
        if any(parts[index : index + 2] == ("contents", "macos") for index in range(len(parts) - 1)):
            return True
        if basename in _PORTABLE_EXECUTABLE_NAMES and (
            len(parts) == 2 or "_internal" in parts or "macos" in parts or mode & 0o111
        ):
            return True

    return any(len(parts) > 1 and "_internal" in parts[1:] for parts, _is_directory, _mode in normalized_members)


def _inspect_archive_member(
    *,
    name: str,
    mode: int,
    size: int,
    stream,
    artifact_name: str,
    depth: int,
    budget: _ReleaseArchiveBudget,
) -> bool:
    prefix = _read_member_prefix(stream, size, name)
    if _native_magic_is_forbidden(
        prefix,
        member_name=name,
        artifact_name=artifact_name,
        depth=depth,
    ):
        return True
    archive_like = _archive_like_member(name, prefix)
    inspect_json = size <= _MAX_RELEASE_JSON_INSPECTION_SIZE
    if not archive_like and not inspect_json:
        return False
    content = _read_complete_member(stream, size, prefix, name)
    if inspect_json and _contains_source_companion_document(content):
        return True
    if not archive_like:
        return False
    if depth >= _MAX_RELEASE_ARCHIVE_DEPTH:
        raise RuntimeError(f"nested release archive exceeds the recursion-depth limit: {name}")
    kind = _nested_archive_kind(content, name)
    return _scan_release_archive(
        io.BytesIO(content),
        kind=kind,
        artifact_name=artifact_name,
        label=name,
        depth=depth + 1,
        budget=budget,
        container_size=len(content),
    )


def _scan_release_zip(
    source,
    *,
    artifact_name: str,
    label: str,
    depth: int,
    budget: _ReleaseArchiveBudget,
    container_size: int,
) -> bool:
    members = []
    archive_uncompressed = 0
    try:
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                name = info.filename.rstrip("/")
                _member_parts(name)
                is_directory = info.is_dir()
                mode = (info.external_attr >> 16) & 0o7777
                members.append((name, is_directory, mode))
                if info.flag_bits & 0x1:
                    raise RuntimeError(f"encrypted release archive member is forbidden: {name}")
                size = 0 if is_directory else info.file_size
                compressed_size = None if is_directory else info.compress_size
                budget.add_member(
                    name=name,
                    size=size,
                    compressed_size=compressed_size,
                )
                archive_uncompressed += size
                if is_directory:
                    continue
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise RuntimeError(f"release ZIP contains a symlink: {name}")
                with archive.open(info) as stream:
                    if _inspect_archive_member(
                        name=name,
                        mode=mode,
                        size=size,
                        stream=stream,
                        artifact_name=artifact_name,
                        depth=depth,
                        budget=budget,
                    ):
                        return True
            corrupt = archive.testzip()
            if corrupt is not None:
                raise RuntimeError(f"release ZIP has a corrupt member: {corrupt}")
    except (EOFError, OSError, RuntimeError, zipfile.BadZipFile):
        raise
    if container_size and archive_uncompressed > container_size * _MAX_RELEASE_ARCHIVE_COMPRESSION_RATIO:
        raise RuntimeError(f"release archive exceeds the compression-ratio limit: {label}")
    return _members_contain_portable_bundle(members)


def _scan_release_tar(
    source,
    *,
    artifact_name: str,
    label: str,
    depth: int,
    budget: _ReleaseArchiveBudget,
    container_size: int,
) -> bool:
    members = []
    archive_uncompressed = 0
    kwargs = {"fileobj": source} if hasattr(source, "read") else {"name": source}
    try:
        with tarfile.open(mode="r:*", **kwargs) as archive:
            for member in archive:
                name = member.name.rstrip("/")
                _member_parts(name)
                is_directory = member.isdir()
                members.append((name, is_directory, member.mode))
                size = member.size if member.isfile() else 0
                budget.add_member(
                    name=name,
                    size=size,
                    compressed_size=None,
                )
                archive_uncompressed += size
                if is_directory:
                    continue
                if member.issym() or member.islnk():
                    raise RuntimeError(f"release TAR contains a link: {name}")
                if not member.isfile():
                    raise RuntimeError(f"release TAR contains an unsupported member: {name}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"cannot read release TAR member: {name}")
                with stream:
                    if _inspect_archive_member(
                        name=name,
                        mode=member.mode,
                        size=size,
                        stream=stream,
                        artifact_name=artifact_name,
                        depth=depth,
                        budget=budget,
                    ):
                        return True
    except (EOFError, OSError, tarfile.TarError) as error:
        raise RuntimeError(f"cannot prove release archive is portable-free: {label}") from error
    if container_size and archive_uncompressed > container_size * _MAX_RELEASE_ARCHIVE_COMPRESSION_RATIO:
        raise RuntimeError(f"release archive exceeds the compression-ratio limit: {label}")
    return _members_contain_portable_bundle(members)


def _scan_release_archive(
    source,
    *,
    kind: str,
    artifact_name: str,
    label: str,
    depth: int,
    budget: _ReleaseArchiveBudget,
    container_size: int,
) -> bool:
    if kind == "zip":
        return _scan_release_zip(
            source,
            artifact_name=artifact_name,
            label=label,
            depth=depth,
            budget=budget,
            container_size=container_size,
        )
    if kind == "tar":
        return _scan_release_tar(
            source,
            artifact_name=artifact_name,
            label=label,
            depth=depth,
            budget=budget,
            container_size=container_size,
        )
    raise RuntimeError(f"unsupported release archive kind: {kind}")


def _archive_contains_portable_bundle(path: Path, kind: str) -> bool:
    return _scan_release_archive(
        path,
        kind=kind,
        artifact_name=path.name,
        label=path.name,
        depth=0,
        budget=_ReleaseArchiveBudget(),
        container_size=path.stat().st_size,
    )


def _has_native_executable_magic(path: Path) -> bool:
    with path.open("rb") as stream:
        magic = stream.read(4)
    return magic[:2] == b"MZ" or magic in _NATIVE_EXECUTABLE_MAGICS


def enforce_release_policy(
    directory: Path,
    version: str,
    lock_path: Path | None = None,
) -> None:
    _validate_version(version)
    directory = directory.resolve(strict=True)
    if not directory.is_dir():
        raise RuntimeError(f"release payload is not a directory: {directory}")
    for path in directory.iterdir():
        if path.is_symlink():
            raise RuntimeError(f"release payload must not contain symlinks: {path.name}")
        if not path.is_file():
            raise RuntimeError(f"release payload must be flat files only: {path.name}")
        lowered = path.name.lower()
        if path.suffix.lower() in _DISALLOWED_NATIVE_SUFFIXES:
            raise RuntimeError(f"native installer/application is forbidden without a platform trust gate: {path.name}")
        if any(claim in lowered for claim in _NATIVE_TRUST_CLAIMS):
            raise RuntimeError(f"unverified native trust claim in artifact name: {path.name}")
        if _source_companion_name_hint(path.name):
            raise RuntimeError(f"source-companion release artifacts are disabled: {path.name}")
        if _portable_name_hint(path.name):
            raise RuntimeError(f"portable release artifacts are disabled: {path.name}")
        if _has_native_executable_magic(path):
            raise RuntimeError(f"native executable is forbidden without a platform trust gate: {path.name}")
        kind = _archive_kind(path)
        if kind is not None and _archive_contains_portable_bundle(path, kind):
            raise RuntimeError(f"portable release artifacts are disabled: {path.name}")


def _parser() -> ArgumentParser:
    parser = ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--version")
    build.add_argument("--output-directory", type=Path, required=True)
    build.add_argument("--build-root", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument(
        "--lock",
        type=Path,
        default=Path("requirements-release.txt"),
    )
    policy = subparsers.add_parser("enforce-release-policy")
    policy.add_argument("--directory", type=Path, required=True)
    policy.add_argument("--version", required=True)
    policy.add_argument(
        "--lock",
        type=Path,
        default=Path("requirements-release.txt"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build":
        version = args.version
        if version is None:
            from core import __version__

            version = __version__
        archive = build_portable_bundle(
            version,
            args.output_directory,
            args.build_root,
        )
        print(archive)
    elif args.command == "verify":
        verify_portable_archive(args.archive, args.lock)
    elif args.command == "enforce-release-policy":
        enforce_release_policy(args.directory, args.version, args.lock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
