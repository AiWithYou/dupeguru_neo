#!/usr/bin/env python3

"""Build and verify easy-launch Windows EXE and macOS APP artifacts."""

from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

from packaging.version import InvalidVersion, Version

if __package__:
    from . import portable_bundle
    from .dependency_license_inventory import verify_inventory
    from .frozen_runtime_license_inventory import verify_inventory as verify_frozen_runtime_inventory
else:
    import portable_bundle
    from dependency_license_inventory import verify_inventory
    from frozen_runtime_license_inventory import verify_inventory as verify_frozen_runtime_inventory


_WINDOWS_ARTIFACT = re.compile(
    r"^dupeguru-neo-(?P<version>[0-9A-Za-z][0-9A-Za-z._+-]*)-" r"windows-(?P<architecture>[0-9a-z_]+)-unsigned\.exe$"
)
_MACOS_ARTIFACT = re.compile(
    r"^dupeguru-neo-(?P<version>[0-9A-Za-z][0-9A-Za-z._+-]*)-" r"macos-(?P<architecture>[0-9a-z_]+)-adhoc\.app\.zip$"
)
_WINDOWS_PRODUCT_NAME = "dupeGuru Neo"
_WINDOWS_ORIGINAL_FILENAME = "dupeguru-neo.exe"
_MACOS_APP_NAME = "dupeguru-neo.app"
_MAX_CARCHIVE_MEMBERS = 100_000
_MAX_CARCHIVE_LEGAL_MEMBER_SIZE = 16 * 1024 * 1024
_MAX_CARCHIVE_LEGAL_TOTAL_SIZE = 64 * 1024 * 1024
_LEGAL_FILES = {
    "LICENSE": Path("LICENSE"),
    "PORTABLE-NOTICE.txt": Path("docs", "PORTABLE-NOTICE.txt"),
    "THIRD_PARTY_NOTICES.md": Path("THIRD_PARTY_NOTICES.md"),
    "hscommon/LICENSE": Path("hscommon", "LICENSE"),
    "release-sources.json": Path("release-sources.json"),
    "requirements-release.txt": Path("requirements-release.txt"),
}
_INVENTORY_PREFIXES = (
    "THIRD-PARTY-LICENSES/",
    "FROZEN-RUNTIME-LICENSES/",
)


def desktop_artifact_name(version: str, platform_name: str, architecture: str) -> str:
    portable_bundle._validate_version(version)
    if re.fullmatch(r"[0-9a-z_]+", architecture) is None:
        raise RuntimeError(f"unsafe desktop architecture: {architecture!r}")
    if platform_name == "windows":
        return f"dupeguru-neo-{version}-windows-{architecture}-unsigned.exe"
    if platform_name == "macos":
        return f"dupeguru-neo-{version}-macos-{architecture}-adhoc.app.zip"
    raise RuntimeError(f"easy-launch desktop artifacts are unsupported on {platform_name!r}")


def _numeric_version(version: str) -> tuple[int, int, int, int]:
    try:
        parsed = Version(version)
    except InvalidVersion as error:
        raise RuntimeError(f"desktop version is not PEP 440 compatible: {version!r}") from error
    release = (*parsed.release[:4], 0, 0, 0, 0)[:4]
    if len(parsed.release) > 4 or any(component > 65535 for component in release):
        raise RuntimeError(f"desktop version cannot be represented by Windows: {version!r}")
    return release


def _windows_version_file(output_path: Path, version: str) -> Path:
    major, minor, patch, build = _numeric_version(version)
    document = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, {build}),
    prodvers=({major}, {minor}, {patch}, {build}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'040904B0',
        [
          StringStruct(u'CompanyName', u'AiWithYou'),
          StringStruct(u'FileDescription', u'{_WINDOWS_PRODUCT_NAME}'),
          StringStruct(u'FileVersion', u'{major}.{minor}.{patch}.{build}'),
          StringStruct(u'InternalName', u'dupeguru-neo'),
          StringStruct(u'LegalCopyright', u'Copyright (c) AiWithYou contributors'),
          StringStruct(u'OriginalFilename', u'{_WINDOWS_ORIGINAL_FILENAME}'),
          StringStruct(u'ProductName', u'{_WINDOWS_PRODUCT_NAME}'),
          StringStruct(u'ProductVersion', u'{version}')
        ]
      )
    ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(document, encoding="utf-8", newline="\n")
    return output_path


def _windows_pyinstaller_arguments(
    project_root: Path,
    build_root: Path,
    license_inventory: Path,
    frozen_runtime_inventory: Path,
    version_file: Path,
) -> list[str]:
    arguments = portable_bundle._pyinstaller_arguments(
        project_root,
        build_root,
        "windows",
        license_inventory,
        frozen_runtime_inventory,
    )
    arguments[arguments.index("--onedir")] = "--onefile"
    arguments.insert(-1, f"--version-file={version_file}")
    return arguments


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        raise RuntimeError("PowerShell is required to verify the Windows desktop executable")
    return executable


def _run_powershell_for_executable(executable: Path, command: str) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["DUPEGURU_DESKTOP_EXECUTABLE"] = str(executable.resolve(strict=True))
    return subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )


def _verify_windows_unsigned(executable: Path) -> None:
    result = _run_powershell_for_executable(
        executable,
        "(Get-AuthenticodeSignature -LiteralPath " "$env:DUPEGURU_DESKTOP_EXECUTABLE).Status.ToString()",
    )
    if result.returncode != 0 or result.stdout.strip() != "NotSigned":
        raise RuntimeError(
            "desktop Windows executable unexpectedly has or cannot prove absence "
            "of Authenticode trust: "
            f"{result.stdout.strip()!r} {result.stderr[-1000:]}"
        )


def _verify_windows_version_resource(executable: Path, version: str) -> None:
    result = _run_powershell_for_executable(
        executable,
        "$info = (Get-Item -LiteralPath $env:DUPEGURU_DESKTOP_EXECUTABLE).VersionInfo; "
        "[pscustomobject]@{"
        "ProductName=$info.ProductName;"
        "FileDescription=$info.FileDescription;"
        "OriginalFilename=$info.OriginalFilename;"
        "FileMajorPart=$info.FileMajorPart;"
        "FileMinorPart=$info.FileMinorPart;"
        "FileBuildPart=$info.FileBuildPart;"
        "FilePrivatePart=$info.FilePrivatePart"
        "} | ConvertTo-Json -Compress",
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot read desktop Windows version resource: {result.stderr[-1000:]}")
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("desktop Windows version resource did not produce JSON") from error
    expected_version = _numeric_version(version)
    actual_version = tuple(
        metadata.get(field)
        for field in (
            "FileMajorPart",
            "FileMinorPart",
            "FileBuildPart",
            "FilePrivatePart",
        )
    )
    if (
        metadata.get("ProductName") != _WINDOWS_PRODUCT_NAME
        or metadata.get("FileDescription") != _WINDOWS_PRODUCT_NAME
        or metadata.get("OriginalFilename") != _WINDOWS_ORIGINAL_FILENAME
        or actual_version != expected_version
    ):
        raise RuntimeError(f"desktop Windows version resource is unexpected: {metadata!r}")


def _canonical_carchive_members(executable: Path):
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as error:
        raise RuntimeError("PyInstaller is required to inspect a desktop executable") from error
    try:
        reader = CArchiveReader(str(executable))
    except Exception as error:
        raise RuntimeError("desktop Windows executable has no readable PyInstaller archive") from error
    if not 0 < len(reader.toc) <= _MAX_CARCHIVE_MEMBERS:
        raise RuntimeError("desktop Windows executable has an invalid archive member count")
    members = {}
    target_keys = set()
    for raw_name, entry in reader.toc.items():
        if not isinstance(raw_name, str):
            raise RuntimeError("desktop Windows executable has a non-text archive member")
        normalized_name = raw_name.replace("\\", "/")
        member = portable_bundle._safe_member_name(normalized_name)
        target_key = portable_bundle._target_member_key(member, "windows")
        if target_key in target_keys:
            raise RuntimeError(f"desktop Windows executable has colliding archive members: {normalized_name!r}")
        target_keys.add(target_key)
        if (
            not isinstance(entry, tuple)
            or len(entry) != 5
            or isinstance(entry[2], bool)
            or not isinstance(entry[2], int)
            or entry[2] < 0
        ):
            raise RuntimeError(f"desktop Windows executable has invalid archive metadata: {normalized_name!r}")
        members[member.as_posix()] = (raw_name, entry)
    return reader, members


def _verify_windows_embedded_legal_data(
    executable: Path,
    project_root: Path,
    *,
    installation_root: Path | None = None,
) -> None:
    reader, members = _canonical_carchive_members(executable)
    required = {
        *_LEGAL_FILES,
        "THIRD-PARTY-LICENSES/index.json",
        "THIRD-PARTY-LICENSES/index.txt",
        "FROZEN-RUNTIME-LICENSES/index.json",
        "FROZEN-RUNTIME-LICENSES/index.txt",
    }
    missing = sorted(required - members.keys())
    if missing:
        raise RuntimeError(f"desktop Windows executable is missing legal data: {missing}")
    with tempfile.TemporaryDirectory(prefix="dupeguru-desktop-legal-") as temporary:
        temporary_root = Path(temporary)
        total_size = 0
        for name, (raw_name, entry) in members.items():
            if name not in _LEGAL_FILES and not name.startswith(_INVENTORY_PREFIXES):
                continue
            uncompressed_size = entry[2]
            if not 0 < uncompressed_size <= _MAX_CARCHIVE_LEGAL_MEMBER_SIZE:
                raise RuntimeError(f"desktop Windows legal member has an invalid size: {name}")
            total_size += uncompressed_size
            if total_size > _MAX_CARCHIVE_LEGAL_TOTAL_SIZE:
                raise RuntimeError("desktop Windows legal data exceeds its size limit")
            try:
                content = reader.extract(raw_name)
            except Exception as error:
                raise RuntimeError(f"cannot extract desktop Windows legal member: {name}") from error
            if not isinstance(content, bytes) or len(content) != uncompressed_size:
                raise RuntimeError(f"desktop Windows legal member is incomplete: {name}")
            destination = temporary_root.joinpath(*PurePosixPath(name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)

        for embedded_name, source_name in _LEGAL_FILES.items():
            source = project_root.joinpath(source_name)
            if temporary_root.joinpath(*PurePosixPath(embedded_name).parts).read_bytes() != source.read_bytes():
                raise RuntimeError(f"desktop Windows embedded file differs from its source: {embedded_name}")

        lock_path = project_root.joinpath("requirements-release.txt")
        source_lock_path = project_root.joinpath("release-sources.json")
        verify_inventory(
            temporary_root.joinpath("THIRD-PARTY-LICENSES"),
            lock_path,
            source_lock_path,
            expected_system="Windows",
            installation_root=installation_root,
        )
        verify_frozen_runtime_inventory(
            temporary_root.joinpath("FROZEN-RUNTIME-LICENSES"),
            source_lock_path,
            expected_system="Windows",
        )


def _smoke_windows_executable(executable: Path) -> None:
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONUTF8"] = "1"
    for argument in ("--version", "--self-test"):
        result = portable_bundle._run_frozen([executable, argument], env=environment)
        if result.returncode != 0:
            raise RuntimeError(f"desktop Windows {argument} failed ({result.returncode}): " f"{result.stderr[-2000:]}")


def verify_windows_executable(
    executable: Path,
    version: str,
    project_root: Path,
    *,
    installation_root: Path | None = None,
) -> None:
    portable_bundle._validate_version(version)
    if executable.is_symlink():
        raise RuntimeError("desktop Windows executable must not be a symlink")
    executable = executable.resolve(strict=True)
    if not executable.is_file() or executable.stat().st_size < 2:
        raise RuntimeError("desktop Windows executable must be a non-empty regular file")
    with executable.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise RuntimeError("desktop Windows executable has no PE header")
    _verify_windows_embedded_legal_data(
        executable,
        project_root.resolve(strict=True),
        installation_root=installation_root,
    )
    _verify_windows_unsigned(executable)
    _verify_windows_version_resource(executable, version)
    _smoke_windows_executable(executable)


def _zip_datetime() -> tuple[int, int, int, int, int, int]:
    timestamp = datetime.fromtimestamp(portable_bundle._source_date_epoch(), timezone.utc)
    timestamp = timestamp.replace(
        microsecond=0,
        second=timestamp.second - (timestamp.second % 2),
    )
    if timestamp.year < 1980:
        timestamp = timestamp.replace(year=1980, month=1, day=1, hour=0, minute=0, second=0)
    if timestamp.year > 2107:
        timestamp = timestamp.replace(year=2107, month=12, day=31, hour=23, minute=59, second=58)
    return (
        timestamp.year,
        timestamp.month,
        timestamp.day,
        timestamp.hour,
        timestamp.minute,
        timestamp.second,
    )


def create_macos_app_zip(app_root: Path, output_path: Path) -> Path:
    app_root = app_root.resolve(strict=True)
    if app_root.name != _MACOS_APP_NAME:
        raise RuntimeError(f"unexpected macOS application name: {app_root.name!r}")
    portable_bundle._validate_source_tree(app_root, allow_symlinks=True)
    output_path, temporary_path = portable_bundle._atomic_target(output_path)
    date_time = _zip_datetime()
    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            for path in portable_bundle._iter_tree(app_root):
                archive_name = portable_bundle._archive_path(app_root, path)
                path_mode = path.lstat().st_mode
                if stat.S_ISDIR(path_mode):
                    archive_name += "/"
                info = zipfile.ZipInfo(archive_name, date_time=date_time)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = path_mode << 16
                if stat.S_ISDIR(path_mode):
                    info.external_attr |= 0x10
                    archive.writestr(info, b"", compresslevel=9)
                elif stat.S_ISLNK(path_mode):
                    target = os.readlink(path)
                    portable_bundle._safe_link_target(
                        archive_name,
                        target,
                        symbolic=True,
                    )
                    archive.writestr(info, target.encode("utf-8"), compresslevel=9)
                elif stat.S_ISREG(path_mode):
                    info.file_size = path.stat().st_size
                    with path.open("rb") as source:
                        with archive.open(
                            info,
                            mode="w",
                            force_zip64=info.file_size >= zipfile.ZIP64_LIMIT,
                        ) as destination:
                            shutil.copyfileobj(source, destination, length=1024 * 1024)
                else:
                    raise RuntimeError(f"macOS application contains an unsupported file: {path}")
        os.replace(temporary_path, output_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return output_path


def _extract_verified_macos_zip(artifact: Path, destination_root: Path) -> Path:
    archive_size = artifact.stat().st_size
    if not 0 < archive_size <= portable_bundle._MAX_PORTABLE_ARCHIVE_INPUT_SIZE:
        raise RuntimeError("desktop macOS archive size is outside the safety limit")
    declared_members = portable_bundle._preflight_portable_zip(artifact)
    expected_members = set(portable_bundle._expected_archive_members("macos"))
    names = set()
    target_entries = {}
    required_target_directories = set()
    budget = portable_bundle._PortableArchiveBudget()
    records = []
    with zipfile.ZipFile(artifact) as archive:
        infos = archive.infolist()
        if len(infos) != declared_members:
            raise RuntimeError("desktop macOS archive member count differs from its central directory")
        for info in infos:
            if info.flag_bits & 0x1:
                raise RuntimeError(f"encrypted desktop macOS archive member is forbidden: {info.filename}")
            canonical_name = info.filename.rstrip("/")
            member = portable_bundle._safe_member_name(info.filename)
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            is_directory = info.is_dir()
            is_symlink = stat.S_ISLNK(mode)
            if is_directory:
                if file_type not in {0, stat.S_IFDIR}:
                    raise RuntimeError(f"desktop macOS directory has an invalid file type: {canonical_name}")
                size = 0
            elif is_symlink:
                if not 0 < info.file_size <= 4096:
                    raise RuntimeError(f"desktop macOS symlink has an invalid size: {canonical_name}")
                size = info.file_size
            else:
                if file_type not in {0, stat.S_IFREG}:
                    raise RuntimeError(f"desktop macOS archive has a special file: {canonical_name}")
                size = info.file_size
            budget.add_member(
                name=canonical_name,
                size=size,
                compressed_size=None if is_directory else info.compress_size,
            )
            portable_bundle._record_target_member(
                target_entries,
                required_target_directories,
                member=member,
                canonical_name=canonical_name,
                is_directory=is_directory,
                platform_name="macos",
            )
            names.add(canonical_name)
            link_target = None
            if is_symlink:
                try:
                    link_target = archive.read(info).decode("utf-8", errors="strict")
                except UnicodeDecodeError as error:
                    raise RuntimeError(f"desktop macOS symlink target is not UTF-8: {canonical_name}") from error
                portable_bundle._safe_link_target(
                    canonical_name,
                    link_target,
                    symbolic=True,
                )
            records.append((info, member, mode, is_directory, is_symlink, link_target))
        budget.verify_container_ratio(archive_size)
        missing = sorted(expected_members - names)
        if missing:
            raise RuntimeError(f"desktop macOS archive is missing required members: {missing}")
        roots = {record[1].parts[0] for record in records}
        if roots != {_MACOS_APP_NAME}:
            raise RuntimeError(f"desktop macOS archive must contain only {_MACOS_APP_NAME!r}")

        destination_root.mkdir(parents=True, exist_ok=False)
        for _, member, mode, is_directory, _, _ in sorted(
            records,
            key=lambda record: (len(record[1].parts), record[1].as_posix()),
        ):
            if not is_directory:
                continue
            destination = destination_root.joinpath(*member.parts)
            destination.mkdir(parents=False, exist_ok=False)
            destination.chmod(stat.S_IMODE(mode) or 0o755)
        for info, member, mode, is_directory, is_symlink, _ in records:
            if is_directory or is_symlink:
                continue
            destination = destination_root.joinpath(*member.parts)
            with archive.open(info) as source:
                with destination.open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
            destination.chmod(stat.S_IMODE(mode) or 0o644)
        for _, member, _, _, is_symlink, link_target in records:
            if not is_symlink:
                continue
            destination = destination_root.joinpath(*member.parts)
            os.symlink(link_target, destination)
    return destination_root.joinpath(_MACOS_APP_NAME)


def _verify_macos_code_signature(app_root: Path) -> None:
    result = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app_root)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"desktop macOS application code signature is invalid: {result.stderr[-2000:]}")


def verify_macos_app_zip(
    artifact: Path,
    version: str,
    project_root: Path,
    *,
    installation_root: Path | None = None,
) -> None:
    portable_bundle._validate_version(version)
    if artifact.is_symlink():
        raise RuntimeError("desktop macOS archive must not be a symlink")
    artifact = artifact.resolve(strict=True)
    if not artifact.is_file():
        raise RuntimeError("desktop macOS archive must be a regular file")
    lock_path = project_root.resolve(strict=True).joinpath("requirements-release.txt")
    portable_bundle._verify_embedded_license_inventory(
        artifact,
        platform_name="macos",
        extension=".zip",
        lock_path=lock_path,
        installation_root=installation_root,
    )
    with tempfile.TemporaryDirectory(prefix="dupeguru-desktop-app-") as temporary:
        extraction_root = Path(temporary).joinpath("extracted")
        app_root = _extract_verified_macos_zip(artifact, extraction_root)
        portable_bundle.verify_unsigned_native_trust(app_root, "macos")
        _verify_macos_code_signature(app_root)
        portable_bundle.smoke_frozen_bundle(app_root, "macos", version)


def verify_desktop_artifact(
    artifact: Path,
    project_root: Path,
    *,
    installation_root: Path | None = None,
) -> None:
    windows_match = _WINDOWS_ARTIFACT.fullmatch(artifact.name)
    macos_match = _MACOS_ARTIFACT.fullmatch(artifact.name)
    match = windows_match or macos_match
    if match is None:
        raise RuntimeError(f"unexpected desktop artifact name: {artifact.name}")
    platform_name = "windows" if windows_match is not None else "macos"
    if portable_bundle._platform_name() != platform_name:
        raise RuntimeError(f"desktop artifact for {platform_name} cannot be verified on this host")
    architecture = portable_bundle._architecture_name()
    if match.group("architecture") != architecture:
        raise RuntimeError(
            "desktop artifact architecture does not match this host: "
            f"{match.group('architecture')} != {architecture}"
        )
    if platform_name == "windows":
        verify_windows_executable(
            artifact,
            match.group("version"),
            project_root,
            installation_root=installation_root,
        )
    else:
        verify_macos_app_zip(
            artifact,
            match.group("version"),
            project_root,
            installation_root=installation_root,
        )


def _copy_without_overwrite(source: Path, destination: Path) -> Path:
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"desktop build output is not a regular file: {source}")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"refusing to overwrite desktop artifact: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        with source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, temporary, length=1024 * 1024)
    try:
        os.replace(temporary_path, destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return destination


def _verified_source_commit(project_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    commit = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise RuntimeError("cannot determine the exact desktop artifact source commit")
    environment_commit = os.environ.get("GITHUB_SHA")
    if environment_commit is not None and environment_commit != commit:
        raise RuntimeError("desktop artifact source commit differs from GITHUB_SHA")
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if status.returncode != 0 or status.stdout:
        raise RuntimeError("desktop artifacts require a clean tracked and untracked source tree")
    epoch = subprocess.run(
        ["git", "show", "-s", "--format=%ct", commit],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch.returncode != 0 or not epoch.stdout.strip().isdecimal() or source_date_epoch != epoch.stdout.strip():
        raise RuntimeError("SOURCE_DATE_EPOCH does not match the desktop source commit")
    return commit


def _write_sidecars(
    artifact: Path,
    version: str,
    platform_name: str,
    project_root: Path,
    commit: str,
) -> None:
    digest = hashlib.sha256()
    with artifact.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    checksum = artifact.with_name(f"{artifact.name}.sha256")
    if checksum.exists() or checksum.is_symlink():
        raise FileExistsError(f"refusing to overwrite desktop checksum: {checksum}")
    checksum.write_text(f"{digest.hexdigest()} *{artifact.name}\n", encoding="utf-8", newline="\n")

    repository = os.environ.get("GITHUB_REPOSITORY", "AiWithYou/dupeguru_neo")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise RuntimeError("desktop artifact repository identity is invalid")
    source_url = f"https://github.com/{repository}/tree/{commit}"
    if platform_name == "windows":
        filename = "README-WINDOWS.txt"
        instructions = (
            "使い方: EXEをダブルクリックしてください。インストールとPythonは不要です。\n"
            "Usage: Double-click the EXE. No Python installation is required.\n"
            "注意: この開発用EXEにはAuthenticode署名がありません。\n"
            "Warning: This development EXE is not Authenticode-signed.\n"
        )
    else:
        filename = "README-MACOS.txt"
        instructions = (
            "使い方: ZIPを展開し、dupeguru-neo.appをApplicationsへ移動して開いてください。\n"
            "Usage: Expand the ZIP, move dupeguru-neo.app to Applications, and open it.\n"
            "注意: この開発用APPはDeveloper ID署名・公証済みではありません。\n"
            "Warning: This development APP is not Developer ID signed or notarized.\n"
        )
    guide = artifact.parent.joinpath(filename)
    if guide.exists() or guide.is_symlink():
        raise FileExistsError(f"refusing to overwrite desktop guide: {guide}")
    guide.write_text(
        (
            f"dupeGuru Neo {version}\n\n"
            f"{instructions}\n"
            f"Exact source / 対応ソース:\n{source_url}\n\n"
            "GPLv3、第三者ライセンス、依存関係・ソース情報は成果物内に同梱されています。\n"
            "GPLv3, third-party notices, dependency inventory, and source mappings are embedded.\n"
            "公式リリース資産ではなく、短期保存されるCI開発成果物です。\n"
            "This is a short-retention CI development artifact, not an official release asset.\n"
        ),
        encoding="utf-8",
        newline="\n",
    )


def _verify_portable_build_root(
    portable_build_root: Path,
    platform_name: str,
    version: str,
    project_root: Path,
) -> Path:
    if platform_name == "macos":
        bundle_root = portable_build_root.joinpath("dist", _MACOS_APP_NAME)
    else:
        bundle_root = portable_build_root.joinpath("dist", "dupeguru-neo")
    bundle_root = bundle_root.resolve(strict=True)
    portable_bundle.verify_unsigned_native_trust(bundle_root, platform_name)
    portable_bundle.smoke_frozen_bundle(bundle_root, platform_name, version)
    data_root = (
        bundle_root.joinpath("Contents", "Resources") if platform_name == "macos" else bundle_root.joinpath("_internal")
    )
    if (
        data_root.joinpath("requirements-release.txt").read_bytes()
        != project_root.joinpath("requirements-release.txt").read_bytes()
    ):
        raise RuntimeError("verified portable build has the wrong dependency lock")
    if (
        data_root.joinpath("release-sources.json").read_bytes()
        != project_root.joinpath("release-sources.json").read_bytes()
    ):
        raise RuntimeError("verified portable build has the wrong source lock")
    return bundle_root


def build_desktop_artifact(
    version: str,
    output_directory: Path,
    portable_build_root: Path,
    build_root: Path,
) -> Path:
    portable_bundle._validate_version(version)
    portable_bundle._source_date_epoch()
    platform_name = portable_bundle._platform_name()
    if platform_name not in {"windows", "macos"}:
        raise RuntimeError(f"easy-launch desktop artifacts are unsupported on {platform_name!r}")
    architecture = portable_bundle._architecture_name()
    project_root = Path.cwd().resolve(strict=True)
    commit = _verified_source_commit(project_root)
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    portable_build_root = portable_build_root.resolve(strict=True)
    bundle_root = _verify_portable_build_root(
        portable_build_root,
        platform_name,
        version,
        project_root,
    )
    output_path = output_directory.joinpath(desktop_artifact_name(version, platform_name, architecture))
    if platform_name == "windows":
        build_root = build_root.resolve()
        for child in ("dist", "work", "spec"):
            build_root.joinpath(child).mkdir(parents=True, exist_ok=True)
        version_file = _windows_version_file(
            build_root.joinpath("dupeguru-neo-version.txt"),
            version,
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                *_windows_pyinstaller_arguments(
                    project_root,
                    build_root,
                    portable_build_root.joinpath("THIRD-PARTY-LICENSES"),
                    portable_build_root.joinpath("FROZEN-RUNTIME-LICENSES"),
                    version_file,
                ),
            ],
            check=True,
        )
        build_output = build_root.joinpath("dist", _WINDOWS_ORIGINAL_FILENAME)
        verify_windows_executable(build_output, version, project_root)
        _copy_without_overwrite(build_output, output_path)
        verify_windows_executable(output_path, version, project_root)
    else:
        create_macos_app_zip(bundle_root, output_path)
        verify_macos_app_zip(output_path, version, project_root)
    _write_sidecars(output_path, version, platform_name, project_root, commit)
    return output_path


def _parser() -> ArgumentParser:
    parser = ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--version")
    build.add_argument("--output-directory", type=Path, required=True)
    build.add_argument("--portable-build-root", type=Path, required=True)
    build.add_argument("--build-root", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--artifact", type=Path, required=True)
    verify.add_argument("--project-root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build":
        version = args.version
        if version is None:
            from core import __version__

            version = __version__
        artifact = build_desktop_artifact(
            version,
            args.output_directory,
            args.portable_build_root,
            args.build_root,
        )
        print(artifact)
    else:
        verify_desktop_artifact(args.artifact, args.project_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
