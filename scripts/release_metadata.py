#!/usr/bin/env python3

"""Generate deterministic checksums and a dependency SBOM for release assets."""

from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime, timezone
import gzip
import hashlib
import io
from importlib import metadata
import json
import os
import platform
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import sys
import tarfile
import tempfile
import uuid

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

_CHECKSUM_LINE = re.compile(r"^(?P<digest>[0-9a-f]{64}) \*(?P<name>.+)$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@-]*$")
_STABLE_TAG = re.compile(r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_SIGSTORE_BUNDLE_SUFFIX = ".sigstore.json"
_FORBIDDEN_RELEASE_SUFFIXES = (
    ".bundle",
    ".crt",
    ".der",
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
    ".sig",
)
_MAX_SIGSTORE_BUNDLE_SIZE = 16 * 1024 * 1024
_DEPENDENCY_SNAPSHOT_SCHEMA = "dupeguru.release-dependency-snapshot"
_RUNTIME_TARGETS = frozenset(
    {
        "linux-x86_64",
        "macos-arm64",
        "windows-x86_64",
    }
)
_MACOS_ARM64_WHEEL_PLATFORM = re.compile(r"^macosx_[0-9]+_[0-9]+_arm64$")
_SOURCE_REQUIRED_FILES = {
    ".github/workflows/release.yml",
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "README.en.md",
    "THIRD_PARTY_NOTICES.md",
    "build.py",
    "docs/SOURCE-COMPANION.md",
    "hscommon/LICENSE",
    "package.py",
    "pyproject.toml",
    "release-sources.json",
    "requirements-release.txt",
    "run.py",
    "setup.cfg",
    "setup.py",
    "scripts/ci_artifact_smoke.py",
    "scripts/dependency_license_inventory.py",
    "scripts/desktop_bundle.py",
    "scripts/frozen_runtime_license_inventory.py",
    "scripts/portable_bundle.py",
    "scripts/release_metadata.py",
    "scripts/source_companion.py",
}
_SOURCE_REQUIRED_PREFIXES = (
    "core/",
    "docs/",
    "hscommon/",
    "images/",
    "pkg/",
    "qt/",
    "scripts/",
)


def _source_date_epoch() -> int:
    raw_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if raw_epoch is None:
        raise RuntimeError("SOURCE_DATE_EPOCH is required for reproducible release metadata")
    try:
        epoch = int(raw_epoch)
    except ValueError as error:
        raise RuntimeError("SOURCE_DATE_EPOCH must be an integer") from error
    if epoch < 0:
        raise RuntimeError("SOURCE_DATE_EPOCH must not be negative")
    return epoch


def _timestamp() -> str:
    epoch = _source_date_epoch()
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dependency_lock_metadata(lock_path: Path) -> dict[str, str]:
    if lock_path.is_symlink() or not lock_path.is_file():
        raise RuntimeError("dependency lock must be a regular non-symlink file")
    lock_path = lock_path.resolve(strict=True)
    if lock_path.name != "requirements-release.txt":
        raise RuntimeError("release dependency lock must be requirements-release.txt")
    return {
        "path": lock_path.name,
        "sha256": _sha256(lock_path),
    }


def _locked_runtime_versions(lock_path: Path) -> dict[str, Version]:
    _dependency_lock_metadata(lock_path)
    locked = {}
    for line_number, raw_line in enumerate(
        lock_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as error:
            raise RuntimeError(f"invalid dependency lock line {line_number}") from error
        specifiers = list(requirement.specifier)
        if (
            requirement.url is not None
            or requirement.extras
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise RuntimeError(f"dependency lock line {line_number} is not an exact version pin")
        name = canonicalize_name(requirement.name)
        if name in locked:
            raise RuntimeError(f"duplicate dependency lock entry: {name}")
        try:
            locked[name] = Version(specifiers[0].version)
        except InvalidVersion as error:
            raise RuntimeError(f"invalid dependency lock version for {name}") from error
    if not locked:
        raise RuntimeError("dependency lock contains no runtime packages")
    return locked


def _stream_sha256(stream) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _validate_artifact_name(name: str) -> None:
    if _ARTIFACT_NAME.fullmatch(name) is None or Path(name).name != name:
        raise RuntimeError(f"unsafe artifact filename: {name!r}")


def _release_payload_files(artifact_directory: Path) -> tuple[list[Path], list[Path]]:
    artifact_directory = artifact_directory.resolve(strict=True)
    if not artifact_directory.is_dir():
        raise RuntimeError(f"artifact directory is not a directory: {artifact_directory}")
    subjects = []
    bundles = []
    casefolded_names = set()
    for path in artifact_directory.iterdir():
        _validate_artifact_name(path.name)
        casefolded_name = path.name.casefold()
        if casefolded_name in casefolded_names:
            raise RuntimeError(f"case-insensitive duplicate release artifact: {path.name}")
        casefolded_names.add(casefolded_name)
        if path.is_symlink():
            raise RuntimeError(f"release artifact must not be a symlink: {path.name}")
        if not path.is_file():
            raise RuntimeError(f"release payload must contain flat files only: {path.name}")
        lowered_name = path.name.lower()
        if lowered_name.endswith(_FORBIDDEN_RELEASE_SUFFIXES):
            raise RuntimeError(f"forbidden release credential/signature sidecar: {path.name}")
        if lowered_name.endswith(_SIGSTORE_BUNDLE_SUFFIX):
            if not 0 < path.stat().st_size <= _MAX_SIGSTORE_BUNDLE_SIZE:
                raise RuntimeError(f"Sigstore bundle has an invalid size: {path.name}")
            bundles.append(path)
        else:
            subjects.append(path)
    return (
        sorted(subjects, key=lambda item: item.name),
        sorted(bundles, key=lambda item: item.name),
    )


def _artifact_candidates(
    artifact_directory: Path,
    excluded_path: Path | None = None,
    *,
    allow_signature_bundles: bool = False,
) -> list[Path]:
    candidates, bundles = _release_payload_files(artifact_directory)
    if bundles and not allow_signature_bundles:
        raise RuntimeError("Sigstore bundles are forbidden before release signing")
    excluded = excluded_path.resolve() if excluded_path is not None else None
    return [path for path in candidates if excluded is None or path.resolve() != excluded]


def _load_sigstore_bundle(path: Path) -> dict:
    def reject_duplicate_keys(pairs):
        document = {}
        for key, value in pairs:
            if key in document:
                raise RuntimeError(f"duplicate JSON key in Sigstore bundle: {path.name}: {key}")
            document[key] = value
        return document

    try:
        bundle = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except RuntimeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid Sigstore bundle for {path.name}") from error
    media_type = bundle.get("mediaType") if isinstance(bundle, dict) else None
    if not isinstance(media_type, str) or not media_type.startswith("application/vnd.dev.sigstore.bundle"):
        raise RuntimeError(f"unexpected Sigstore bundle type for {path.name}")
    return bundle


def verify_release_payload_layout(
    artifact_directory: Path,
    *,
    signature_mode: str,
) -> None:
    if signature_mode not in {"forbidden", "optional", "required"}:
        raise RuntimeError(f"unsupported signature mode: {signature_mode!r}")
    subjects, bundles = _release_payload_files(artifact_directory)
    if not subjects:
        raise RuntimeError("release payload contains no subject artifacts")
    if signature_mode == "forbidden":
        if bundles:
            raise RuntimeError("Sigstore bundles are forbidden before release signing")
        return
    if signature_mode == "optional" and not bundles:
        return
    expected_bundle_names = {f"{subject.name}{_SIGSTORE_BUNDLE_SUFFIX}" for subject in subjects}
    actual_bundle_names = {bundle.name for bundle in bundles}
    if actual_bundle_names != expected_bundle_names:
        missing = sorted(expected_bundle_names - actual_bundle_names)
        orphaned = sorted(actual_bundle_names - expected_bundle_names)
        raise RuntimeError(
            "Sigstore sidecar bijection mismatch; missing={} orphaned={}".format(
                missing,
                orphaned,
            )
        )
    for bundle in bundles:
        _load_sigstore_bundle(bundle)


def verify_release_payload_contract(
    artifact_directory: Path,
    *,
    version: str,
    payload_kind: str,
    signature_mode: str,
) -> None:
    parsed_version = validate_release_version(version)
    verify_release_payload_layout(
        artifact_directory,
        signature_mode=signature_mode,
    )
    subjects, _ = _release_payload_files(artifact_directory)
    subject_names = {path.name for path in subjects}

    if payload_kind == "source-companion":
        expected_names = {
            "SOURCE-COMPANION-SHA256SUMS",
            f"dupeguru-neo-{version}-source-companion.tar",
        }
    elif payload_kind == "release":
        forbidden_assets = sorted(
            name
            for name in subject_names
            if "source-companion" in name.casefold()
            or ("portable" in name.casefold() and name.casefold().endswith((".zip", ".tar.gz")))
        )
        if forbidden_assets:
            asset_list = ", ".join(forbidden_assets)
            raise RuntimeError(
                f"official release payload must not contain portable or source-companion assets: {asset_list}"
            )

        canonical_sdist_name = f"dupeguru_neo-{version}.tar.gz"
        wheels = [path for path in subjects if path.name.endswith(".whl")]
        sdists = [path for path in subjects if path.name == canonical_sdist_name]
        if len(wheels) != len(_RUNTIME_TARGETS) or len(sdists) != 1:
            raise RuntimeError(
                f"release payload must contain the canonical sdist "
                f"{canonical_sdist_name!r} and exactly one CPython 3.13 wheel "
                "for every release runtime target"
            )
        try:
            sdist_name, sdist_version = parse_sdist_filename(sdists[0].name)
        except InvalidSdistFilename as error:
            raise RuntimeError("release payload has an invalid Python artifact name") from error
        if canonicalize_name(sdist_name) != "dupeguru-neo" or sdist_version != parsed_version:
            raise RuntimeError("release Python artifact identity mismatch")

        wheel_names = set()
        wheel_targets = set()
        for wheel in wheels:
            try:
                wheel_name, wheel_version, build, tags = parse_wheel_filename(wheel.name)
            except InvalidWheelFilename as error:
                raise RuntimeError("release payload has an invalid Python artifact name") from error
            if (
                canonicalize_name(wheel_name) != "dupeguru-neo"
                or wheel_version != parsed_version
                or build != ()
                or len(tags) != 1
            ):
                raise RuntimeError(f"release wheel identity mismatch: {wheel.name}")
            tag = next(iter(tags))
            if tag.interpreter != "cp313" or tag.abi != "cp313":
                raise RuntimeError(f"release wheel must target the frozen CPython 3.13 ABI: {wheel.name}")
            if tag.platform == "linux_x86_64":
                target = "linux-x86_64"
            elif tag.platform == "win_amd64":
                target = "windows-x86_64"
            elif _MACOS_ARM64_WHEEL_PLATFORM.fullmatch(tag.platform):
                target = "macos-arm64"
            else:
                raise RuntimeError(f"release wheel has an unsupported platform tag: {wheel.name}")
            if target in wheel_targets:
                raise RuntimeError(f"release payload contains duplicate wheels for {target}")
            wheel_targets.add(target)
            wheel_names.add(wheel.name)
        if wheel_targets != _RUNTIME_TARGETS:
            raise RuntimeError("release payload must contain one CPython 3.13 wheel for every runtime target")

        expected_names = {
            "BUILD-METADATA.json",
            "HSCOMMON-BSD-3-CLAUSE.txt",
            "LICENSE",
            "SHA256SUMS",
            "THIRD_PARTY_NOTICES.md",
            "release-sources.json",
            "requirements-release.txt",
            f"dupeguru-neo-{version}-source.tar.gz",
            f"dupeguru-neo-{version}.cdx.json",
            canonical_sdist_name,
            *wheel_names,
        }
    else:
        raise RuntimeError(f"unsupported release payload kind: {payload_kind!r}")

    if subject_names != expected_names:
        missing = sorted(expected_names - subject_names)
        unexpected = sorted(subject_names - expected_names)
        raise RuntimeError(
            "release subject allowlist mismatch; missing={} unexpected={}".format(
                missing,
                unexpected,
            )
        )


def _distribution_index() -> dict[str, metadata.Distribution]:
    return {
        canonicalize_name(distribution.metadata["Name"]): distribution
        for distribution in metadata.distributions()
        if distribution.metadata.get("Name")
    }


def _required_names(distribution: metadata.Distribution) -> set[str]:
    required = set()
    for raw_requirement in distribution.requires or ():
        requirement = Requirement(raw_requirement)
        if requirement.marker is None or requirement.marker.evaluate():
            required.add(canonicalize_name(requirement.name))
    return required


def dependency_closure(root_name: str) -> tuple[dict[str, metadata.Distribution], dict[str, set[str]]]:
    index = _distribution_index()
    root = canonicalize_name(root_name)
    if root not in index:
        raise metadata.PackageNotFoundError(root_name)
    selected: dict[str, metadata.Distribution] = {}
    dependency_edges: dict[str, set[str]] = {}
    pending = [root]
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        distribution = index.get(name)
        if distribution is None:
            raise RuntimeError(f"installed dependency metadata is missing for {name}")
        selected[name] = distribution
        requirements = _required_names(distribution)
        missing = requirements - index.keys()
        if missing:
            raise RuntimeError(f"installed dependencies are missing: {', '.join(sorted(missing))}")
        dependency_edges[name] = requirements
        pending.extend(sorted(requirements, reverse=True))
    return selected, dependency_edges


def _runtime_target() -> str:
    system = platform.system().lower()
    if system == "darwin":
        system = "macos"
    machine = platform.machine().lower()
    if machine in {"amd64", "x64"}:
        machine = "x86_64"
    elif machine == "aarch64":
        machine = "arm64"
    target = f"{system}-{machine}"
    if target not in _RUNTIME_TARGETS:
        raise RuntimeError(f"unsupported release dependency-snapshot target: {target}")
    return target


def generate_dependency_snapshot(
    root_name: str,
    target: str,
    output_path: Path,
) -> Path:
    if target not in _RUNTIME_TARGETS:
        raise RuntimeError(f"unsupported release dependency-snapshot target: {target}")
    actual_target = _runtime_target()
    if target != actual_target:
        raise RuntimeError(f"dependency-snapshot target {target!r} does not match the runner {actual_target!r}")
    if output_path.is_symlink():
        raise RuntimeError("dependency-snapshot output must not be a symlink")
    selected, edges = dependency_closure(root_name)
    root = canonicalize_name(root_name)
    components = []
    for name in sorted(selected):
        distribution = selected[name]
        component = {
            "name": name,
            "display_name": distribution.metadata["Name"],
            "version": distribution.version,
            "purl": f"pkg:pypi/{name}@{distribution.version}",
        }
        license_expression = distribution.metadata.get("License-Expression")
        if license_expression:
            component["license_expression"] = license_expression
        components.append(component)
    document = {
        "schema": _DEPENDENCY_SNAPSHOT_SCHEMA,
        "version": 1,
        "target": target,
        "root": root,
        "components": components,
        "dependencies": [
            {
                "ref": name,
                "depends_on": sorted(edges[name]),
            }
            for name in sorted(selected)
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return output_path


def _load_json_document(path: Path, purpose: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{purpose} must be a regular non-symlink file: {path.name}")
    if not 0 < path.stat().st_size <= 2 * 1024 * 1024:
        raise RuntimeError(f"{purpose} has an invalid size: {path.name}")

    def reject_duplicate_keys(pairs):
        document = {}
        for key, value in pairs:
            if key in document:
                raise RuntimeError(f"duplicate JSON key in {purpose}: {path.name}: {key}")
            document[key] = value
        return document

    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except RuntimeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid {purpose}: {path.name}") from error
    if not isinstance(document, dict):
        raise RuntimeError(f"{purpose} must contain a JSON object: {path.name}")
    return document


def _load_dependency_snapshots(
    snapshot_directory: Path,
    root_name: str,
) -> tuple[dict[str, dict[str, str]], dict[str, set[str]], dict[str, set[str]], dict[str, str]]:
    if snapshot_directory.is_symlink():
        raise RuntimeError("dependency-snapshot directory must not be a symlink")
    snapshot_directory = snapshot_directory.resolve(strict=True)
    if not snapshot_directory.is_dir():
        raise RuntimeError("dependency-snapshot directory must be a directory")
    paths = sorted(snapshot_directory.iterdir(), key=lambda item: item.name)
    if any(path.is_dir() for path in paths):
        raise RuntimeError("dependency-snapshot directory must contain flat files only")
    if len(paths) != len(_RUNTIME_TARGETS):
        raise RuntimeError("one dependency snapshot is required for every runtime target")

    root = canonicalize_name(root_name)
    records: dict[str, dict[str, str]] = {}
    merged_edges: dict[str, set[str]] = {}
    component_targets: dict[str, set[str]] = {}
    snapshot_digests = {}
    seen_targets = set()
    for path in paths:
        _validate_artifact_name(path.name)
        document = _load_json_document(path, "dependency snapshot")
        if (
            set(document)
            != {
                "components",
                "dependencies",
                "root",
                "schema",
                "target",
                "version",
            }
            or document.get("schema") != _DEPENDENCY_SNAPSHOT_SCHEMA
            or document.get("version") != 1
            or document.get("root") != root
        ):
            raise RuntimeError(f"dependency snapshot identity mismatch: {path.name}")
        target = document.get("target")
        if not isinstance(target, str) or target not in _RUNTIME_TARGETS or target in seen_targets:
            raise RuntimeError(f"dependency snapshot target mismatch: {path.name}")
        if path.name != f"dependency-snapshot-{target}.json":
            raise RuntimeError(f"dependency snapshot filename mismatch: {path.name}")
        seen_targets.add(target)
        snapshot_digests[target] = _sha256(path)

        raw_components = document.get("components")
        raw_dependencies = document.get("dependencies")
        if not isinstance(raw_components, list) or not isinstance(raw_dependencies, list):
            raise RuntimeError(f"dependency snapshot structure mismatch: {path.name}")
        snapshot_records = {}
        for component in raw_components:
            if not isinstance(component, dict):
                raise RuntimeError(f"dependency snapshot component is not an object: {path.name}")
            allowed_keys = {
                "display_name",
                "license_expression",
                "name",
                "purl",
                "version",
            }
            if not {"display_name", "name", "purl", "version"} <= component.keys() or (component.keys() - allowed_keys):
                raise RuntimeError(f"dependency snapshot component structure mismatch: {path.name}")
            name = component["name"]
            display_name = component["display_name"]
            version = component["version"]
            purl = component["purl"]
            if (
                not isinstance(name, str)
                or canonicalize_name(name) != name
                or not isinstance(display_name, str)
                or not display_name
                or not isinstance(version, str)
                or not isinstance(purl, str)
                or purl != f"pkg:pypi/{name}@{version}"
            ):
                raise RuntimeError(f"dependency snapshot component identity mismatch: {path.name}")
            try:
                Version(version)
            except InvalidVersion as error:
                raise RuntimeError(f"dependency snapshot has an invalid version: {path.name}") from error
            license_expression = component.get("license_expression")
            if license_expression is not None and (not isinstance(license_expression, str) or not license_expression):
                raise RuntimeError(f"dependency snapshot has an invalid license: {path.name}")
            record = {
                "display_name": display_name,
                "version": version,
                "purl": purl,
            }
            if license_expression is not None:
                record["license_expression"] = license_expression
            if name in snapshot_records:
                raise RuntimeError(f"duplicate dependency snapshot component: {path.name}: {name}")
            snapshot_records[name] = record
            existing = records.get(name)
            if existing is not None and existing != record:
                raise RuntimeError(f"dependency metadata differs across runtime targets: {name}")
            records[name] = record
            component_targets.setdefault(name, set()).add(target)
        if root not in snapshot_records:
            raise RuntimeError(f"dependency snapshot is missing its root component: {path.name}")

        snapshot_edges = {}
        for dependency in raw_dependencies:
            if (
                not isinstance(dependency, dict)
                or set(dependency) != {"depends_on", "ref"}
                or not isinstance(dependency["ref"], str)
                or not isinstance(dependency["depends_on"], list)
                or any(not isinstance(name, str) for name in dependency["depends_on"])
            ):
                raise RuntimeError(f"dependency snapshot edge structure mismatch: {path.name}")
            ref = dependency["ref"]
            depends_on = dependency["depends_on"]
            if (
                ref in snapshot_edges
                or ref not in snapshot_records
                or len(depends_on) != len(set(depends_on))
                or depends_on != sorted(depends_on)
                or any(name not in snapshot_records for name in depends_on)
            ):
                raise RuntimeError(f"dependency snapshot edge identity mismatch: {path.name}")
            snapshot_edges[ref] = set(depends_on)
        if set(snapshot_edges) != set(snapshot_records):
            raise RuntimeError(f"dependency snapshot edge inventory mismatch: {path.name}")

        reachable = set()
        pending = [root]
        while pending:
            name = pending.pop()
            if name in reachable:
                continue
            reachable.add(name)
            pending.extend(snapshot_edges[name] - reachable)
        if reachable != set(snapshot_records):
            raise RuntimeError(f"dependency snapshot contains unreachable components: {path.name}")
        for name, dependencies in snapshot_edges.items():
            merged_edges.setdefault(name, set()).update(dependencies)

    if seen_targets != _RUNTIME_TARGETS:
        raise RuntimeError("dependency snapshots do not cover every release runtime target")
    for name in records:
        merged_edges.setdefault(name, set())
    return records, merged_edges, component_targets, snapshot_digests


def generate_sbom(
    root_name: str,
    output_path: Path,
    artifact_directory: Path | None = None,
    lock_path: Path | None = None,
    dependency_snapshots_directory: Path | None = None,
) -> Path:
    if output_path.is_symlink():
        raise RuntimeError("SBOM output must not be a symlink")
    root = canonicalize_name(root_name)
    component_targets = {}
    snapshot_digests = {}
    if dependency_snapshots_directory is None:
        selected, edges = dependency_closure(root_name)
        records = {}
        for name, distribution in selected.items():
            record = {
                "display_name": distribution.metadata["Name"],
                "version": distribution.version,
                "purl": f"pkg:pypi/{name}@{distribution.version}",
            }
            license_expression = distribution.metadata.get("License-Expression")
            if license_expression:
                record["license_expression"] = license_expression
            records[name] = record
    else:
        records, edges, component_targets, snapshot_digests = _load_dependency_snapshots(
            dependency_snapshots_directory,
            root_name,
        )
        if lock_path is not None:
            locked_versions = _locked_runtime_versions(lock_path)
            runtime_names = set(records) - {root}
            if runtime_names != set(locked_versions):
                missing = sorted(set(locked_versions) - runtime_names)
                unexpected = sorted(runtime_names - set(locked_versions))
                raise RuntimeError(
                    "cross-platform runtime closure does not match the dependency "
                    f"lock; missing={missing} unexpected={unexpected}"
                )
            for name in sorted(runtime_names):
                if Version(records[name]["version"]) != locked_versions[name]:
                    raise RuntimeError(f"installed runtime version does not match the dependency lock: {name}")

    def bom_ref(name: str) -> str:
        return records[name]["purl"]

    components = []
    for name in sorted(records):
        record = records[name]
        component = {
            "type": "application" if name == root else "library",
            "bom-ref": bom_ref(name),
            "name": record["display_name"],
            "version": record["version"],
            "purl": bom_ref(name),
        }
        license_expression = record.get("license_expression")
        if license_expression:
            component["licenses"] = [{"expression": license_expression}]
        if name in component_targets:
            component["properties"] = [
                {
                    "name": "dupeguru:runtime-targets",
                    "value": ",".join(sorted(component_targets[name])),
                }
            ]
        components.append(component)

    if artifact_directory is not None:
        artifact_directory = artifact_directory.resolve(strict=True)
        resolved_output = output_path.resolve()
        for path in _artifact_candidates(artifact_directory, resolved_output):
            digest = _sha256(path)
            components.append(
                {
                    "type": "file",
                    "bom-ref": f"urn:dupeguru:release-artifact:{digest}:{path.name}",
                    "name": path.name,
                    "hashes": [{"alg": "SHA-256", "content": digest}],
                    "properties": [
                        {
                            "name": "dupeguru:release-artifact:size",
                            "value": str(path.stat().st_size),
                        }
                    ],
                }
            )

    dependencies = [
        {
            "ref": bom_ref(name),
            "dependsOn": [bom_ref(dependency) for dependency in sorted(edges[name])],
        }
        for name in sorted(records)
    ]
    root_component = next(component for component in components if component["bom-ref"] == bom_ref(root))
    lock = _dependency_lock_metadata(lock_path) if lock_path is not None else None
    serial_seed = (
        f"{root_component['name']}:{root_component['version']}:{_timestamp()}:"
        f"{lock['sha256'] if lock is not None else 'unlocked'}"
    )
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, serial_seed)}",
        "version": 1,
        "metadata": {
            "timestamp": _timestamp(),
            "component": root_component,
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "dupeGuru Neo release_metadata",
                        "version": "1",
                    }
                ]
            },
        },
        "components": components,
        "dependencies": dependencies,
    }
    properties = []
    if lock is not None:
        properties.extend(
            [
                {
                    "name": "dupeguru:dependency-lock:path",
                    "value": lock["path"],
                },
                {
                    "name": "dupeguru:dependency-lock:sha256",
                    "value": lock["sha256"],
                },
            ]
        )
    if dependency_snapshots_directory is not None:
        properties.extend(
            [
                {
                    "name": "dupeguru:sbom:inventory-scope",
                    "value": (
                        "union of installed runtime dependency closures captured "
                        "on every release target, plus release payload files"
                    ),
                },
                {
                    "name": "dupeguru:sbom:runtime-targets",
                    "value": ",".join(sorted(_RUNTIME_TARGETS)),
                },
            ]
        )
        properties.extend(
            {
                "name": f"dupeguru:sbom:dependency-snapshot:{target}:sha256",
                "value": snapshot_digests[target],
            }
            for target in sorted(snapshot_digests)
        )
    if properties:
        document["metadata"]["properties"] = properties
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return output_path


def generate_checksums(artifact_directory: Path, output_path: Path) -> Path:
    artifact_directory = artifact_directory.resolve(strict=True)
    if output_path.is_symlink():
        raise RuntimeError("checksum output must not be a symlink")
    output_path = output_path.resolve()
    if output_path.parent != artifact_directory:
        raise RuntimeError("checksum output must be directly inside the artifact directory")
    candidates = _artifact_candidates(artifact_directory, output_path)
    if not candidates:
        raise RuntimeError("no release artifacts found")
    lines = []
    for path in candidates:
        lines.append(f"{_sha256(path)} *{path.name}")
    output_path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return output_path


def read_checksum_entries(checksum_path: Path) -> list[tuple[str, str]]:
    entries = []
    names = set()
    for line_number, line in enumerate(checksum_path.read_text(encoding="ascii").splitlines(), start=1):
        match = _CHECKSUM_LINE.fullmatch(line)
        if match is None:
            raise RuntimeError(f"invalid checksum line {line_number}")
        name = match.group("name")
        _validate_artifact_name(name)
        if name in names:
            raise RuntimeError(f"duplicate checksum entry: {name}")
        names.add(name)
        entries.append((match.group("digest"), name))
    if not entries:
        raise RuntimeError("checksum file has no entries")
    return entries


def verify_checksums(artifact_directory: Path, checksum_path: Path) -> None:
    artifact_directory = artifact_directory.resolve(strict=True)
    checksum_path = checksum_path.resolve(strict=True)
    if checksum_path.parent != artifact_directory:
        raise RuntimeError("checksum file must be directly inside the artifact directory")
    verify_release_payload_layout(
        artifact_directory,
        signature_mode="optional",
    )
    entries = read_checksum_entries(checksum_path)
    expected_names = {
        path.name
        for path in _artifact_candidates(
            artifact_directory,
            checksum_path,
            allow_signature_bundles=True,
        )
    }
    listed_names = {name for _, name in entries}
    if listed_names != expected_names:
        missing = sorted(expected_names - listed_names)
        unexpected = sorted(listed_names - expected_names)
        raise RuntimeError("checksum inventory mismatch; missing={} unexpected={}".format(missing, unexpected))
    for expected_digest, name in entries:
        actual_digest = _sha256(artifact_directory.joinpath(name))
        if actual_digest != expected_digest:
            raise RuntimeError(f"checksum mismatch for {name}")


def generate_build_manifest(
    artifact_directory: Path,
    output_path: Path,
    *,
    repository: str,
    commit: str,
    ref: str,
    version: str,
    lock_path: Path | None = None,
) -> Path:
    if _REPOSITORY.fullmatch(repository) is None:
        raise RuntimeError("repository must use the owner/name form")
    if _COMMIT.fullmatch(commit) is None:
        raise RuntimeError("commit must be a lowercase full-length Git SHA")
    if not ref.startswith("refs/tags/") or any(character in ref for character in ("\r", "\n", "\0")):
        raise RuntimeError("release ref must be a safe tag ref")
    validate_release_tag(ref.removeprefix("refs/tags/"), version)
    artifact_directory = artifact_directory.resolve(strict=True)
    if output_path.is_symlink():
        raise RuntimeError("manifest output must not be a symlink")
    output_path = output_path.resolve()
    if output_path.parent != artifact_directory:
        raise RuntimeError("manifest output must be directly inside the artifact directory")
    artifacts = [
        {
            "name": path.name,
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        }
        for path in _artifact_candidates(artifact_directory, output_path)
    ]
    if not artifacts:
        raise RuntimeError("no release artifacts found")
    document = {
        "schema": "dupeguru.release-build-metadata",
        "schema_version": 1,
        "repository": repository,
        "commit": commit,
        "ref": ref,
        "version": version,
        "source_date_epoch": _source_date_epoch(),
        "timestamp": _timestamp(),
        "builder_runtime": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "artifacts": artifacts,
    }
    if lock_path is not None:
        document["dependency_lock"] = _dependency_lock_metadata(lock_path)
    output_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return output_path


def verify_signature_bundles(artifact_directory: Path) -> None:
    artifact_directory = artifact_directory.resolve(strict=True)
    verify_release_payload_layout(
        artifact_directory,
        signature_mode="required",
    )
    candidates = _artifact_candidates(
        artifact_directory,
        allow_signature_bundles=True,
    )
    for artifact in candidates:
        _load_sigstore_bundle(artifact.with_name(artifact.name + _SIGSTORE_BUNDLE_SUFFIX))


def verify_sigstore_bundles(
    artifact_directory: Path,
    *,
    certificate_identity: str,
    certificate_oidc_issuer: str,
) -> None:
    for label, value in (
        ("certificate identity", certificate_identity),
        ("certificate OIDC issuer", certificate_oidc_issuer),
    ):
        if not value or any(character in value for character in ("\r", "\n", "\0")):
            raise RuntimeError(f"unsafe {label}")
    verify_signature_bundles(artifact_directory)
    artifact_directory = artifact_directory.resolve(strict=True)
    for artifact in _artifact_candidates(
        artifact_directory,
        allow_signature_bundles=True,
    ):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "sigstore",
                "verify",
                "identity",
                "--bundle",
                artifact.with_name(artifact.name + _SIGSTORE_BUNDLE_SUFFIX),
                "--cert-identity",
                certificate_identity,
                "--cert-oidc-issuer",
                certificate_oidc_issuer,
                artifact,
            ],
            check=True,
        )


def _git_tree_entries(commit: str) -> dict[str, tuple[str, str]]:
    if _COMMIT.fullmatch(commit) is None:
        raise RuntimeError("source commit must be a lowercase full-length Git SHA")
    resolved_commit = subprocess.run(
        ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if resolved_commit != commit:
        raise RuntimeError("source commit does not resolve exactly")
    raw_tree = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-tree", "-r", "-z", commit],
        check=True,
        capture_output=True,
    ).stdout
    entries = {}
    for raw_entry in raw_tree.split(b"\0"):
        if not raw_entry:
            continue
        try:
            raw_metadata, raw_path = raw_entry.split(b"\t", 1)
            mode, object_type, object_id = raw_metadata.decode("ascii").split()
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise RuntimeError("Git tree contains an unsupported entry") from error
        candidate = PurePosixPath(path)
        if (
            not candidate.parts
            or candidate.is_absolute()
            or candidate.as_posix() != path
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise RuntimeError(f"Git tree contains an unsafe path: {path!r}")
        if object_type != "blob" or mode not in {"100644", "100755", "120000"}:
            raise RuntimeError(f"unsupported Git tree entry: {mode} {object_type} {path}")
        if path in entries:
            raise RuntimeError(f"duplicate Git tree path: {path}")
        entries[path] = (mode, object_id)
    if not entries:
        raise RuntimeError("source commit contains no tracked files")
    missing = sorted(_SOURCE_REQUIRED_FILES - entries.keys())
    if missing:
        raise RuntimeError(f"source commit is missing required build files: {missing}")
    missing_prefixes = [
        prefix for prefix in _SOURCE_REQUIRED_PREFIXES if not any(path.startswith(prefix) for path in entries)
    ]
    if missing_prefixes:
        raise RuntimeError(f"source commit is missing required source trees: {missing_prefixes}")
    return entries


def _git_blob_contents(entries: dict[str, tuple[str, str]]) -> dict[str, bytes]:
    process = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise RuntimeError("cannot open Git object verification pipes")
    contents = {}
    try:
        for path, (_, object_id) in entries.items():
            process.stdin.write(object_id.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().decode("ascii").strip().split()
            if len(header) != 3 or header[0] != object_id or header[1] != "blob":
                raise RuntimeError(f"cannot read Git blob for {path}")
            size = int(header[2])
            content = process.stdout.read(size)
            terminator = process.stdout.read(1)
            if len(content) != size or terminator != b"\n":
                raise RuntimeError(f"truncated Git blob for {path}")
            contents[path] = content
        process.stdin.close()
        return_code = process.wait(timeout=30)
        if return_code != 0:
            stderr = process.stderr.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"git cat-file failed: {stderr[-2000:]}")
    except BaseException:
        process.kill()
        process.wait()
        raise
    return contents


def _safe_source_member(name: str, root_name: str) -> str | None:
    if not name or "\0" in name or "\\" in name or name.startswith("/"):
        raise RuntimeError(f"unsafe corresponding-source member: {name!r}")
    candidate = PurePosixPath(name.rstrip("/"))
    if (
        not candidate.parts
        or candidate.as_posix() != name.rstrip("/")
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or candidate.parts[0] != root_name
    ):
        raise RuntimeError(f"unsafe corresponding-source member: {name!r}")
    if len(candidate.parts) == 1:
        return None
    return PurePosixPath(*candidate.parts[1:]).as_posix()


def verify_corresponding_source(
    archive_path: Path,
    *,
    commit: str,
    version: str,
) -> None:
    validate_release_version(version)
    expected_name = f"dupeguru-neo-{version}-source.tar.gz"
    if archive_path.is_symlink():
        raise RuntimeError("corresponding-source archive must not be a symlink")
    archive_path = archive_path.resolve(strict=True)
    if archive_path.name != expected_name:
        raise RuntimeError("corresponding-source archive has an unsafe name or type")
    epoch = _source_date_epoch()
    with archive_path.open("rb") as raw_stream:
        gzip_header = raw_stream.read(10)
    if len(gzip_header) != 10 or gzip_header[:2] != b"\x1f\x8b" or int.from_bytes(gzip_header[4:8], "little") != epoch:
        raise RuntimeError("corresponding-source gzip timestamp is not deterministic")
    entries = _git_tree_entries(commit)
    expected_contents = _git_blob_contents(entries)
    root_name = f"dupeguru-neo-{version}-source"
    found = {}
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive:
            relative_name = _safe_source_member(member.name, root_name)
            if member.mtime != epoch or member.uid != 0 or member.gid != 0 or member.uname or member.gname:
                raise RuntimeError(f"non-deterministic corresponding-source metadata: {member.name}")
            if relative_name is None:
                if not member.isdir():
                    raise RuntimeError("corresponding-source root must be a directory")
                continue
            if relative_name in found:
                raise RuntimeError(f"duplicate corresponding-source member: {relative_name}")
            if member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"cannot read corresponding-source member: {relative_name}")
                digest = _stream_sha256(stream)
                member_kind = "100755" if member.mode & 0o111 else "100644"
            elif member.issym():
                if not member.linkname or member.linkname.startswith("/") or "\0" in member.linkname:
                    raise RuntimeError(f"unsafe corresponding-source symlink: {relative_name}")
                normalized_target = posixpath.normpath(
                    posixpath.join(
                        posixpath.dirname(member.name),
                        member.linkname,
                    )
                )
                target = PurePosixPath(normalized_target)
                if (
                    not target.parts
                    or target.parts[0] != root_name
                    or any(part in {"", ".", ".."} for part in target.parts)
                ):
                    raise RuntimeError(f"escaping corresponding-source symlink: {relative_name}")
                digest = hashlib.sha256(member.linkname.encode("utf-8")).hexdigest()
                member_kind = "120000"
            elif member.isdir():
                continue
            else:
                raise RuntimeError(f"unsupported corresponding-source member: {relative_name}")
            found[relative_name] = (member_kind, digest)
    expected = {
        path: (mode, hashlib.sha256(expected_contents[path]).hexdigest()) for path, (mode, _) in entries.items()
    }
    if found != expected:
        missing = sorted(expected.keys() - found.keys())
        unexpected = sorted(found.keys() - expected.keys())
        mismatched = sorted(path for path in expected.keys() & found.keys() if expected[path] != found[path])
        raise RuntimeError(
            "corresponding-source mismatch; missing={} unexpected={} mismatched={}".format(
                missing,
                unexpected,
                mismatched,
            )
        )


def generate_corresponding_source(
    output_path: Path,
    *,
    commit: str,
    version: str,
) -> Path:
    validate_release_version(version)
    expected_name = f"dupeguru-neo-{version}-source.tar.gz"
    if output_path.is_symlink():
        raise RuntimeError("source archive output must not be a symlink")
    output_path = output_path.resolve()
    if output_path.name != expected_name:
        raise RuntimeError(f"source archive must be named {expected_name}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() or output_path.is_symlink():
        raise FileExistsError(f"refusing to overwrite source archive: {output_path}")
    epoch = _source_date_epoch()
    root_name = f"dupeguru-neo-{version}-source"
    entries = _git_tree_entries(commit)
    blob_contents = _git_blob_contents(entries)
    compressed_archive = tempfile.NamedTemporaryFile(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        delete=False,
    )
    compressed_archive.close()
    compressed_path = Path(compressed_archive.name)
    try:
        with compressed_path.open("wb") as raw_output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw_output,
                mtime=epoch,
            ) as compressed_output:
                with tarfile.open(
                    mode="w",
                    fileobj=compressed_output,
                    format=tarfile.PAX_FORMAT,
                ) as destination_archive:
                    directories = {root_name}
                    for path in entries:
                        parts = PurePosixPath(path).parts
                        for length in range(1, len(parts)):
                            directories.add(f"{root_name}/" + PurePosixPath(*parts[:length]).as_posix())
                    for directory in sorted(
                        directories,
                        key=lambda name: (name.count("/"), name),
                    ):
                        member = tarfile.TarInfo(f"{directory}/")
                        member.type = tarfile.DIRTYPE
                        member.mode = 0o755
                        member.uid = 0
                        member.gid = 0
                        member.uname = ""
                        member.gname = ""
                        member.mtime = epoch
                        destination_archive.addfile(member)
                    for path in sorted(entries):
                        mode, _ = entries[path]
                        content = blob_contents[path]
                        member = tarfile.TarInfo(f"{root_name}/{path}")
                        member.uid = 0
                        member.gid = 0
                        member.uname = ""
                        member.gname = ""
                        member.mtime = epoch
                        if mode == "120000":
                            try:
                                link_target = content.decode("utf-8")
                            except UnicodeDecodeError as error:
                                raise RuntimeError(f"source symlink target is not UTF-8: {path}") from error
                            normalized_target = posixpath.normpath(
                                posixpath.join(
                                    posixpath.dirname(member.name),
                                    link_target,
                                )
                            )
                            target = PurePosixPath(normalized_target)
                            if (
                                not link_target
                                or link_target.startswith("/")
                                or not target.parts
                                or target.parts[0] != root_name
                            ):
                                raise RuntimeError(f"source symlink escapes archive root: {path}")
                            member.type = tarfile.SYMTYPE
                            member.linkname = link_target
                            member.mode = 0o777
                            destination_archive.addfile(member)
                        else:
                            member.type = tarfile.REGTYPE
                            member.mode = 0o755 if mode == "100755" else 0o644
                            member.size = len(content)
                            destination_archive.addfile(member, io.BytesIO(content))
        os.replace(compressed_path, output_path)
    except BaseException:
        compressed_path.unlink(missing_ok=True)
        raise
    verify_corresponding_source(
        output_path,
        commit=commit,
        version=version,
    )
    return output_path


def validate_release_version(version: str) -> Version:
    try:
        parsed_version = Version(version)
    except InvalidVersion as error:
        raise RuntimeError(f"invalid package version: {version}") from error
    if parsed_version.local is not None or parsed_version.epoch != 0:
        raise RuntimeError("local and epoch versions are not allowed for releases")
    if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*", version) is None:
        raise RuntimeError("release version is unsafe for an artifact filename")
    return parsed_version


def validate_release_tag(tag: str, version: str) -> bool:
    if any(character in tag for character in ("\r", "\n", "\0")):
        raise RuntimeError("release tag contains a control character")
    parsed_version = validate_release_version(version)
    if tag != f"v{version}":
        raise RuntimeError(f"tag {tag!r} does not match package version {version!r}")
    stable = _STABLE_TAG.fullmatch(tag) is not None
    if stable and (parsed_version.is_prerelease or parsed_version.is_devrelease):
        raise RuntimeError("a stable tag cannot contain a pre-release version")
    return stable


def git_release_context(tag: str, commit: str) -> int:
    if _COMMIT.fullmatch(commit) is None:
        raise RuntimeError("GITHUB_SHA must be a lowercase full-length Git SHA")
    tagged_commit = subprocess.run(
        ["git", "rev-list", "-n", "1", f"refs/tags/{tag}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if tagged_commit != commit:
        raise RuntimeError("the release tag does not resolve to GITHUB_SHA")
    ancestry = subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            commit,
            "refs/remotes/origin/main",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if ancestry.returncode == 1:
        raise RuntimeError("the release commit is not reachable from origin/main")
    if ancestry.returncode != 0:
        reason = ancestry.stderr.strip() or "unknown error"
        raise RuntimeError(f"Git could not prove release commit ancestry through origin/main: {reason}")
    raw_epoch = subprocess.run(
        ["git", "show", "-s", "--format=%ct", commit],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    try:
        epoch = int(raw_epoch)
    except ValueError as error:
        raise RuntimeError("Git returned an invalid commit timestamp") from error
    if epoch < 0:
        raise RuntimeError("Git returned a negative commit timestamp")
    return epoch


def _append_github_values(path: Path, values: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            rendered = str(value)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key) is None:
                raise RuntimeError(f"unsafe GitHub output key: {key}")
            if any(character in rendered for character in ("\r", "\n", "\0")):
                raise RuntimeError(f"unsafe GitHub output value for {key}")
            stream.write(f"{key}={rendered}\n")


def _parser() -> ArgumentParser:
    parser = ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    sbom = subparsers.add_parser("sbom")
    sbom.add_argument("--distribution", required=True)
    sbom.add_argument("--output", type=Path, required=True)
    sbom.add_argument("--artifacts-directory", type=Path)
    sbom.add_argument("--lock", type=Path, required=True)
    sbom.add_argument("--dependency-snapshots-directory", type=Path)
    dependency_snapshot = subparsers.add_parser("dependency-snapshot")
    dependency_snapshot.add_argument("--distribution", required=True)
    dependency_snapshot.add_argument("--target", choices=sorted(_RUNTIME_TARGETS), required=True)
    dependency_snapshot.add_argument("--output", type=Path, required=True)
    checksums = subparsers.add_parser("checksums")
    checksums.add_argument("--directory", type=Path, required=True)
    checksums.add_argument("--output", type=Path, required=True)
    verify_checksums_parser = subparsers.add_parser("verify-checksums")
    verify_checksums_parser.add_argument("--directory", type=Path, required=True)
    verify_checksums_parser.add_argument("--checksums", type=Path, required=True)
    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--directory", type=Path, required=True)
    manifest.add_argument("--output", type=Path, required=True)
    manifest.add_argument("--repository", required=True)
    manifest.add_argument("--commit", required=True)
    manifest.add_argument("--ref", required=True)
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--lock", type=Path, required=True)
    signatures = subparsers.add_parser("verify-signatures")
    signatures.add_argument("--directory", type=Path, required=True)
    payload = subparsers.add_parser("verify-payload")
    payload.add_argument("--directory", type=Path, required=True)
    payload.add_argument(
        "--signature-mode",
        choices=("forbidden", "optional", "required"),
        required=True,
    )
    payload.add_argument(
        "--payload-kind",
        choices=("release", "source-companion"),
        required=True,
    )
    payload.add_argument("--version", required=True)
    sigstore = subparsers.add_parser("verify-sigstore")
    sigstore.add_argument("--directory", type=Path, required=True)
    sigstore.add_argument("--cert-identity", required=True)
    sigstore.add_argument("--cert-oidc-issuer", required=True)
    source = subparsers.add_parser("source-archive")
    source.add_argument("--output", type=Path, required=True)
    source.add_argument("--commit", required=True)
    source.add_argument("--version", required=True)
    verify_source = subparsers.add_parser("verify-source-archive")
    verify_source.add_argument("--archive", type=Path, required=True)
    verify_source.add_argument("--commit", required=True)
    verify_source.add_argument("--version", required=True)
    gate = subparsers.add_parser("gate")
    gate.add_argument("--tag", required=True)
    gate.add_argument("--version", required=True)
    gate.add_argument("--commit", required=True)
    gate.add_argument("--github-output", type=Path, required=True)
    gate.add_argument("--github-env", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "sbom":
        generate_sbom(
            args.distribution,
            args.output,
            artifact_directory=args.artifacts_directory,
            lock_path=args.lock,
            dependency_snapshots_directory=args.dependency_snapshots_directory,
        )
    elif args.command == "dependency-snapshot":
        generate_dependency_snapshot(
            args.distribution,
            args.target,
            args.output,
        )
    elif args.command == "checksums":
        generate_checksums(args.directory, args.output)
    elif args.command == "verify-checksums":
        verify_checksums(args.directory, args.checksums)
    elif args.command == "manifest":
        generate_build_manifest(
            args.directory,
            args.output,
            repository=args.repository,
            commit=args.commit,
            ref=args.ref,
            version=args.version,
            lock_path=args.lock,
        )
    elif args.command == "verify-signatures":
        verify_signature_bundles(args.directory)
    elif args.command == "verify-payload":
        verify_release_payload_contract(
            args.directory,
            version=args.version,
            payload_kind=args.payload_kind,
            signature_mode=args.signature_mode,
        )
    elif args.command == "verify-sigstore":
        verify_sigstore_bundles(
            args.directory,
            certificate_identity=args.cert_identity,
            certificate_oidc_issuer=args.cert_oidc_issuer,
        )
    elif args.command == "source-archive":
        generate_corresponding_source(
            args.output,
            commit=args.commit,
            version=args.version,
        )
    elif args.command == "verify-source-archive":
        verify_corresponding_source(
            args.archive,
            commit=args.commit,
            version=args.version,
        )
    elif args.command == "gate":
        stable = validate_release_tag(args.tag, args.version)
        epoch = git_release_context(args.tag, args.commit)
        _append_github_values(
            args.github_output,
            {
                "stable": str(stable).lower(),
                "version": args.version,
                "source_date_epoch": epoch,
            },
        )
        _append_github_values(args.github_env, {"SOURCE_DATE_EPOCH": epoch})
    return 0


if __name__ == "__main__":
    sys.exit(main())
