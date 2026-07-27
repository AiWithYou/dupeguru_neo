#!/usr/bin/env python3

"""Install built artifacts into clean environments and exercise public entry points."""

from __future__ import annotations

from argparse import ArgumentParser
import csv
from email import policy
from email.parser import BytesParser
import gettext
import hashlib
from importlib import import_module, metadata
import io
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import venv
import zipfile

EXPECTED_HELP_LANGUAGES = ("de", "en", "fr", "hy", "ru", "uk")
EXPECTED_GETTEXT_CATALOGS = 63
MAX_COMMAND_DIAGNOSTIC_CHARACTERS = 4000
DARWIN_BUILD_UUID = re.compile(
    r"^UUID: (?P<uuid>[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}) " r"\((?P<architecture>[A-Za-z0-9_]+)\) .+$"
)


def _run(command, *, cwd=None, env=None, capture_output=False):
    command = [str(item) for item in command]
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=True,
            text=True,
            capture_output=capture_output,
        )
    except subprocess.CalledProcessError as error:
        if not capture_output:
            raise
        stdout = (error.stdout or "")[-MAX_COMMAND_DIAGNOSTIC_CHARACTERS:]
        stderr = (error.stderr or "")[-MAX_COMMAND_DIAGNOSTIC_CHARACTERS:]
        raise RuntimeError(
            "captured command failed with exit code {}: {!r}; stdout tail={!r}; "
            "stderr tail={!r}".format(
                error.returncode,
                command,
                stdout,
                stderr,
            )
        ) from error


def _extract_validated_tar(archive, destination: Path, *, members) -> None:
    kwargs = {"filter": "data"} if sys.version_info >= (3, 12) else {}
    archive.extractall(destination, members=members, **kwargs)


def _environment_python(environment: Path) -> Path:
    if os.name == "nt":
        return environment.joinpath("Scripts", "python.exe")
    return environment.joinpath("bin", "python")


def _console_script(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    scripts_directory = sysconfig.get_path("scripts")
    if not scripts_directory:
        raise RuntimeError("Python did not report an installed console-script directory")
    candidate = Path(scripts_directory).joinpath(name + suffix)
    if not candidate.is_file():
        raise RuntimeError(f"installed console script is missing: {candidate}")
    return candidate


def _installed_smoke() -> None:
    distribution = metadata.distribution("dupeguru-neo")
    installation_root = Path(distribution.locate_file("")).resolve()
    prefix = Path(sys.prefix).resolve()
    if prefix not in (installation_root, *installation_root.parents):
        raise RuntimeError(f"dupeguru-neo was imported outside the clean environment: {installation_root}")
    required_runtime_files = tuple(f"help/{language}/index.html" for language in EXPECTED_HELP_LANGUAGES) + (
        "locale/de/LC_MESSAGES/core.mo",
        "locale/fr/LC_MESSAGES/ui.mo",
    )
    missing_runtime_files = [
        name for name in required_runtime_files if not Path(distribution.locate_file(name)).is_file()
    ]
    if missing_runtime_files:
        raise RuntimeError(f"installed runtime data is missing: {missing_runtime_files}")
    from qt.platform import BASE_PATH, HELP_PATH

    locale_root = Path(BASE_PATH, "locale")
    installed_languages = {path.name for path in locale_root.iterdir() if path.is_dir() and not path.is_symlink()}
    if set(EXPECTED_HELP_LANGUAGES) - installed_languages:
        raise RuntimeError("installed locale directory cannot be enumerated")
    if not Path(HELP_PATH, "index.html").is_file():
        raise RuntimeError("Qt help path does not resolve to packaged English help")
    gettext.translation("core", localedir=locale_root, languages=["de"])
    for native_module in ("core.pe._block", "core.pe._cache", "qt.pe._block_qt"):
        import_module(native_module)

    command = _console_script("dupeguru")
    doctor = _run([command, "doctor"], capture_output=True)
    doctor_payload = json.loads(doctor.stdout)
    if doctor_payload.get("schema") != "dupeguru.doctor-report":
        raise RuntimeError("doctor did not emit a versioned doctor report")

    with tempfile.TemporaryDirectory(prefix="dupeguru-cli-smoke-") as temporary:
        scan_root = Path(temporary)
        scan_root.joinpath("first.bin").write_bytes(b"verified duplicate")
        scan_root.joinpath("second.bin").write_bytes(b"verified duplicate")
        scan = _run(
            [command, "scan", scan_root, "--quiet"],
            cwd=scan_root,
            capture_output=True,
        )
        records = [json.loads(line) for line in scan.stdout.splitlines()]
        if not records or records[-1].get("record_type") != "summary":
            raise RuntimeError("scan did not emit a JSONL summary")
        summary = records[-1].get("summary", {})
        if not summary.get("complete") or summary.get("verified_groups", 0) < 1:
            raise RuntimeError("scan smoke did not produce a complete verified group")

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PyQt6.QtGui import QPixmap
    from PyQt6.QtWidgets import QApplication
    from qt.app import DupeGuru
    from qt.resources import RESOURCE_FILES, resource_path

    application = QApplication.instance() or QApplication(["dupeguru-ci-smoke"])
    if not issubclass(DupeGuru, object):
        raise RuntimeError("Qt application class import failed")
    for alias in RESOURCE_FILES:
        if QPixmap(resource_path(alias)).isNull():
            raise RuntimeError(f"packaged Qt resource could not be decoded: {alias}")
    application.quit()


def _artifacts(directory: Path) -> tuple[Path, Path]:
    directory = directory.resolve(strict=True)
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError(
            f"expected exactly one wheel and one sdist, found {len(wheels)} wheel(s) " f"and {len(sdists)} sdist(s)"
        )
    if wheels[0].is_symlink() or sdists[0].is_symlink():
        raise RuntimeError("package artifacts must not be symlinks")
    return wheels[0], sdists[0]


def artifact_install_smoke(
    directory: Path,
    qt_requirement: str,
    constraints: Path,
) -> None:
    wheel, sdist = _artifacts(directory)
    script = Path(__file__).resolve()
    if constraints.is_symlink() or not constraints.is_file():
        raise RuntimeError("constraints must be a regular non-symlink file")
    constraints = constraints.resolve(strict=True)
    for artifact in (wheel, sdist):
        with tempfile.TemporaryDirectory(prefix=f"dupeguru-{artifact.name}-") as temporary:
            temporary_path = Path(temporary)
            environment = temporary_path.joinpath("venv")
            venv.EnvBuilder(with_pip=True, clear=True).create(environment)
            python = _environment_python(environment)
            clean_environment = os.environ.copy()
            clean_environment.pop("PYTHONPATH", None)
            clean_environment["PYTHONUTF8"] = "1"
            clean_environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
            _run(
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--no-input",
                    "--constraint",
                    constraints,
                    artifact,
                    qt_requirement,
                ],
                cwd=temporary_path,
                env=clean_environment,
            )
            _run(
                [python, "-m", "pip", "check"],
                cwd=temporary_path,
                env=clean_environment,
            )
            _run(
                [python, script, "--installed"],
                cwd=temporary_path,
                env=clean_environment,
            )


def artifact_twine_check(directory: Path) -> None:
    wheel, sdist = _artifacts(directory)
    _validate_wheel_license_files(wheel)
    _validate_wheel_runtime_data(wheel)
    _validate_darwin_wheel_build_uuids(wheel)
    _validate_sdist_build_inputs(sdist)
    _run(
        [
            sys.executable,
            "-m",
            "twine",
            "check",
            "--strict",
            wheel,
            sdist,
        ]
    )


def reproducible_wheel_check(directory: Path) -> None:
    wheel, sdist = _artifacts(directory)
    raw_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if raw_epoch is None or not raw_epoch.isdecimal():
        raise RuntimeError("a decimal SOURCE_DATE_EPOCH is required for the reproducible-wheel check")
    with tempfile.TemporaryDirectory(prefix="dupeguru-reproducible-wheel-") as temporary:
        temporary_path = Path(temporary)
        source_root = _extract_regular_sdist(sdist, temporary_path.joinpath("source"))
        output_directory = temporary_path.joinpath("dist")
        output_directory.mkdir()
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = "0"
        _run(
            [
                _console_script("pyproject-build"),
                "--wheel",
                "--outdir",
                output_directory,
                source_root,
            ],
            cwd=temporary_path,
            env=environment,
        )
        rebuilt_wheels = sorted(output_directory.glob("*.whl"))
        if len(rebuilt_wheels) != 1:
            raise RuntimeError(f"independent rebuild produced {len(rebuilt_wheels)} wheels")
        rebuilt = rebuilt_wheels[0]
        if rebuilt.name != wheel.name:
            raise RuntimeError(f"independent rebuild changed the wheel filename: {wheel.name!r} != {rebuilt.name!r}")
        original_digest = _sha256_file(wheel)
        rebuilt_digest = _sha256_file(rebuilt)
        if rebuilt_digest != original_digest:
            differences = _wheel_difference_report(wheel, rebuilt)
            raise RuntimeError(
                "wheel is not byte-for-byte reproducible from its sdist: "
                f"{original_digest} != {rebuilt_digest}; {differences}"
            )


def _wheel_difference_report(original: Path, rebuilt: Path, limit: int = 12) -> str:
    """Describe a failed exact comparison without weakening the comparison."""

    differences = []
    with zipfile.ZipFile(original) as original_archive, zipfile.ZipFile(rebuilt) as rebuilt_archive:
        original_infos = original_archive.infolist()
        rebuilt_infos = rebuilt_archive.infolist()
        original_names = [info.filename for info in original_infos]
        rebuilt_names = [info.filename for info in rebuilt_infos]
        if original_names != rebuilt_names:
            original_only = sorted(set(original_names) - set(rebuilt_names))
            rebuilt_only = sorted(set(rebuilt_names) - set(original_names))
            differences.append(
                "member sequence differs "
                f"(original-only={original_only[:limit]!r}, rebuilt-only={rebuilt_only[:limit]!r})"
            )

        original_by_name = {info.filename: info for info in original_infos}
        rebuilt_by_name = {info.filename: info for info in rebuilt_infos}
        shared_names = sorted(set(original_by_name) & set(rebuilt_by_name))
        for name in shared_names:
            original_info = original_by_name[name]
            rebuilt_info = rebuilt_by_name[name]
            metadata_fields = (
                "date_time",
                "compress_type",
                "comment",
                "extra",
                "create_system",
                "create_version",
                "extract_version",
                "flag_bits",
                "internal_attr",
                "external_attr",
            )
            changed_metadata = [
                field for field in metadata_fields if getattr(original_info, field) != getattr(rebuilt_info, field)
            ]
            if changed_metadata:
                differences.append(f"{name!r}: ZIP metadata differs ({', '.join(changed_metadata)})")
            original_member_digest = hashlib.sha256(original_archive.read(original_info)).hexdigest()
            rebuilt_member_digest = hashlib.sha256(rebuilt_archive.read(rebuilt_info)).hexdigest()
            if original_member_digest != rebuilt_member_digest:
                differences.append(f"{name!r}: payload differs ({original_member_digest} != {rebuilt_member_digest})")

        if original_archive.comment != rebuilt_archive.comment:
            differences.append("ZIP archive comments differ")
    if not differences:
        differences.append(
            "ZIP container encoding differs although member sequence, decoded payloads, and inspected metadata match"
        )
    omitted = max(0, len(differences) - limit)
    report = "; ".join(differences[:limit])
    if omitted:
        report += f"; {omitted} additional difference(s) omitted"
    return report


def _extract_regular_sdist(sdist: Path, destination: Path) -> Path:
    names = set()
    casefolded_names = set()
    roots = set()
    members = []
    with tarfile.open(sdist, mode="r:gz") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            parts = name.split("/")
            if (
                not name
                or name.startswith("/")
                or "\\" in name
                or any(part in {"", ".", ".."} or ":" in part or "\0" in part for part in parts)
            ):
                raise RuntimeError(f"sdist contains an unsafe member: {member.name!r}")
            if name in names:
                raise RuntimeError(f"sdist contains a duplicate member: {name}")
            casefolded_name = name.casefold()
            if casefolded_name in casefolded_names:
                raise RuntimeError(f"sdist contains a case-insensitive member collision: {name}")
            if not member.isfile() and not member.isdir():
                raise RuntimeError(f"sdist reproducibility check rejects non-regular member: {member.name}")
            names.add(name)
            casefolded_names.add(casefolded_name)
            roots.add(parts[0])
            members.append(member)
        if len(roots) != 1:
            raise RuntimeError(f"sdist must have exactly one root: {sorted(roots)}")
        destination.mkdir()
        _extract_validated_tar(archive, destination, members=members)
    source_root = destination.joinpath(next(iter(roots))).resolve(strict=True)
    if destination.resolve(strict=True) not in source_root.parents or not source_root.is_dir():
        raise RuntimeError("sdist extraction did not produce one contained source root")
    return source_root


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_wheel_license_files(wheel: Path) -> None:
    names = set()
    license_members = {}
    metadata_members = {}
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            name = info.filename.rstrip("/")
            path = PurePosixPath(name)
            if (
                not name
                or name.startswith("/")
                or "\\" in name
                or any(part in {"", ".", ".."} for part in path.parts)
                or path.as_posix() != name
            ):
                raise RuntimeError(f"wheel contains an unsafe member: {info.filename!r}")
            if name in names:
                raise RuntimeError(f"wheel contains a duplicate member: {name}")
            names.add(name)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise RuntimeError(f"wheel contains a symlink: {name}")
            lowered = name.lower()
            if ".dist-info/licenses/" in lowered and not info.is_dir():
                license_members[name] = archive.read(info)
            if lowered.endswith(".dist-info/metadata") and not info.is_dir():
                metadata_members[name] = archive.read(info)
        corrupt = archive.testzip()
        if corrupt is not None:
            raise RuntimeError(f"wheel has a corrupt member: {corrupt}")
    required_suffixes = {
        ".dist-info/licenses/LICENSE",
        ".dist-info/licenses/THIRD_PARTY_NOTICES.md",
        ".dist-info/licenses/hscommon/LICENSE",
    }
    missing = sorted(
        suffix for suffix in required_suffixes if not any(name.endswith(suffix) for name in license_members)
    )
    if missing:
        raise RuntimeError(f"wheel is missing required license files: {missing}")
    if len(metadata_members) != 1:
        raise RuntimeError(f"wheel must contain exactly one METADATA file, found {sorted(metadata_members)}")
    metadata_message = BytesParser(policy=policy.default).parsebytes(next(iter(metadata_members.values())))
    if metadata_message.get("License-Expression") != "GPL-3.0-only":
        raise RuntimeError("wheel metadata is missing the GPL-3.0-only License-Expression")
    if metadata_message.get("License") is not None:
        raise RuntimeError("wheel metadata contains the legacy License field")
    classifiers = set(metadata_message.get_all("Classifier", ()))
    if "Development Status :: 4 - Beta" not in classifiers:
        raise RuntimeError("wheel metadata does not identify this pre-release project as Beta")
    if "Development Status :: 5 - Production/Stable" in classifiers:
        raise RuntimeError("wheel metadata claims Production/Stable status before the first public release")
    notice_name = next(name for name in license_members if name.endswith(".dist-info/licenses/THIRD_PARTY_NOTICES.md"))
    try:
        notice = license_members[notice_name].decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("wheel third-party notice must be UTF-8") from error
    for distribution in (
        "distro",
        "mutagen",
        "Pillow",
        "PyQt6",
        "PyQt6-Qt6",
        "PyQt6_sip",
        "semantic-version",
        "xxhash",
        "pywin32",
    ):
        if distribution not in notice:
            raise RuntimeError(f"wheel third-party notice is missing dependency {distribution}")


def _validate_wheel_runtime_data(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        record_name = next(name for name in names if name.endswith(".dist-info/RECORD"))
        record_names = {
            row[0]
            for row in csv.reader(
                io.StringIO(archive.read(record_name).decode("utf-8")),
            )
            if row
        }
    required = {
        *(f"help/{language}/index.html" for language in EXPECTED_HELP_LANGUAGES),
        "locale/de/LC_MESSAGES/core.mo",
        "locale/fr/LC_MESSAGES/ui.mo",
    }
    missing = sorted(required - names)
    if missing:
        raise RuntimeError(f"wheel is missing required runtime data: {missing}")
    missing_from_record = sorted(required - record_names)
    if missing_from_record:
        raise RuntimeError(f"wheel RECORD is missing runtime data: {missing_from_record}")
    help_indexes = {name for name in names if name.startswith("help/") and name.endswith("/index.html")}
    if len(help_indexes) != len(EXPECTED_HELP_LANGUAGES):
        raise RuntimeError(f"wheel contains {len(help_indexes)} help indexes; expected {len(EXPECTED_HELP_LANGUAGES)}")
    gettext_catalogs = {name for name in names if name.startswith("locale/") and name.endswith(".mo")}
    if len(gettext_catalogs) != EXPECTED_GETTEXT_CATALOGS:
        raise RuntimeError(
            f"wheel contains {len(gettext_catalogs)} gettext catalogs; " f"expected {EXPECTED_GETTEXT_CATALOGS}"
        )
    generated_build_state = sorted(name for name in names if "/.doctrees/" in name or name.endswith("/.buildinfo"))
    if generated_build_state:
        raise RuntimeError(f"wheel contains Sphinx build state: {generated_build_state[:10]}")
    generated_bytecode = sorted(name for name in names if "/__pycache__/" in name or name.endswith((".pyc", ".pyo")))
    if generated_bytecode:
        raise RuntimeError(f"wheel contains build-root-specific Python bytecode: {generated_bytecode[:10]}")
    development_only = sorted(
        name
        for name in names
        if name.startswith(("core/tests/", "hscommon/tests/", "scripts/"))
        or name.startswith(("core/pe/modules/", "qt/pe/modules/"))
        or name.endswith((".c", ".h", ".m"))
    )
    if development_only:
        raise RuntimeError(f"wheel contains development-only sources: {development_only[:10]}")
    if not any(name.startswith("images/") and name.endswith(".png") for name in names):
        raise RuntimeError("wheel is missing packaged image resources")


def _validate_darwin_wheel_build_uuids(wheel: Path) -> None:
    if sys.platform != "darwin":
        return
    with zipfile.ZipFile(wheel) as archive:
        native_members = sorted(
            (info for info in archive.infolist() if not info.is_dir() and info.filename.endswith(".so")),
            key=lambda info: info.filename,
        )
        if not native_members:
            raise RuntimeError("macOS wheel has no native extensions to validate")
        with tempfile.TemporaryDirectory(prefix="dupeguru-macho-uuid-") as temporary:
            temporary_path = Path(temporary)
            uuid_owners = {}
            for index, info in enumerate(native_members):
                extracted = temporary_path.joinpath(f"extension-{index}.so")
                member_payload = archive.read(info)
                member_digest = hashlib.sha256(member_payload).hexdigest()
                extracted.write_bytes(member_payload)
                result = _run(["dwarfdump", "--uuid", extracted], capture_output=True)
                output_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                matches = [DARWIN_BUILD_UUID.fullmatch(line) for line in output_lines]
                if not matches or any(match is None for match in matches):
                    raise RuntimeError(f"macOS wheel member {info.filename!r} has no valid LC_UUID")
                uuids = [match.group("uuid").casefold() for match in matches]
                architectures = [match.group("architecture") for match in matches]
                if len(set(architectures)) != len(architectures):
                    raise RuntimeError(f"macOS wheel member {info.filename!r} repeats an LC_UUID architecture")
                for uuid, architecture in zip(uuids, architectures):
                    owner = f"{info.filename} ({architecture})"
                    previous = uuid_owners.get(uuid)
                    if previous is not None:
                        previous_owner, previous_architecture, previous_digest = previous
                        if previous_architecture == architecture and previous_digest == member_digest:
                            continue
                        conflict = (
                            "a different architecture"
                            if previous_architecture != architecture
                            else "a different member SHA-256"
                        )
                        raise RuntimeError(
                            f"macOS Mach-O image {owner!r} reuses LC_UUID {uuid} "
                            f"from {previous_owner!r} with {conflict}"
                        )
                    uuid_owners[uuid] = (owner, architecture, member_digest)


def _validate_sdist_build_inputs(sdist: Path) -> None:
    required_files = {
        "LICENSE",
        "MANIFEST.in",
        "README.md",
        "README.ja.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "build.py",
        "docs/SOURCE-COMPANION.md",
        "docs/PORTABLE-NOTICE.txt",
        "hscommon/LICENSE",
        "package.py",
        "pyproject.toml",
        "release-sources.json",
        "requirements-release.txt",
        "run.py",
        "scripts/ci_artifact_smoke.py",
        "scripts/dependency_license_inventory.py",
        "scripts/desktop_bundle.py",
        "scripts/frozen_runtime_license_inventory.py",
        "scripts/portable_bundle.py",
        "scripts/release_metadata.py",
        "scripts/source_companion.py",
        "setup.cfg",
        "setup.py",
    }
    required_prefixes = (
        "core/",
        "help/",
        "hscommon/",
        "images/",
        "locale/",
        "pkg/",
        "qt/",
    )
    files = set()
    roots = set()
    with tarfile.open(sdist, mode="r:gz") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            parts = name.split("/")
            if not name or name.startswith("/") or "\\" in name or any(part in {"", ".", ".."} for part in parts):
                raise RuntimeError(f"sdist contains an unsafe member: {member.name!r}")
            roots.add(parts[0])
            if member.isfile() or member.issym():
                if len(parts) < 2:
                    raise RuntimeError("sdist file is outside its package root")
                files.add("/".join(parts[1:]))
            elif not member.isdir():
                raise RuntimeError(f"sdist contains an unsupported member: {member.name}")
    if len(roots) != 1:
        raise RuntimeError(f"sdist must have exactly one root: {sorted(roots)}")
    missing = sorted(required_files - files)
    if missing:
        raise RuntimeError(f"sdist is missing required build inputs: {missing}")
    missing_prefixes = [prefix for prefix in required_prefixes if not any(name.startswith(prefix) for name in files)]
    if missing_prefixes:
        raise RuntimeError(f"sdist is missing required source trees: {missing_prefixes}")
    for required_source in (
        "help/en/index.rst",
        "help/conf.tmpl",
        "locale/de/LC_MESSAGES/core.po",
    ):
        if required_source not in files:
            raise RuntimeError(f"sdist is missing runtime-data source: {required_source}")
    forbidden_generated = [
        name
        for name in files
        if name.endswith(".mo")
        or (name.startswith("help/") and (name.endswith("/conf.py") or name.endswith("/changelog.rst")))
    ]
    if forbidden_generated:
        raise RuntimeError(f"sdist contains non-source generated runtime data: {forbidden_generated[:10]}")


def _parser() -> ArgumentParser:
    parser = ArgumentParser()
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--qt-requirement", default="PyQt6==6.11.0")
    parser.add_argument(
        "--constraints",
        type=Path,
        default=Path("requirements-release.txt"),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--installed", action="store_true")
    mode.add_argument("--reproducible-wheel", action="store_true")
    mode.add_argument("--twine-check", action="store_true")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.installed:
        if args.artifacts is not None:
            raise RuntimeError("--artifacts cannot be used with --installed")
        _installed_smoke()
        return 0
    if args.artifacts is None:
        raise RuntimeError("--artifacts is required")
    if args.twine_check:
        artifact_twine_check(args.artifacts)
        return 0
    if args.reproducible_wheel:
        reproducible_wheel_check(args.artifacts)
        return 0
    artifact_install_smoke(
        args.artifacts,
        args.qt_requirement,
        args.constraints,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
