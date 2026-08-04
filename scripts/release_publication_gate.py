# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Revalidate every mutable GitHub publication prerequisite.

The tagged-release workflow invokes this gate after protected-environment
approval and again in the same shell step that makes a draft public.  Keeping
the checks in one program prevents the two call sites from drifting apart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

_COMMIT = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_TAG = re.compile(r"v[0-9A-Za-z][0-9A-Za-z._+-]*")
_ENVIRONMENTS = frozenset({"stable-release", "prerelease"})
_REQUIRED_WORKFLOWS = ("default.yml", "codeql-analysis.yml")
_GITHUB_API_VERSION = "2026-03-10"
_MAX_API_RESPONSE_BYTES = 16 * 1024 * 1024
_ASSET_API_PAGE_SIZE = 100
_HASH_CHUNK_SIZE = 1024 * 1024


class PublicationGateError(RuntimeError):
    """A mutable publication prerequisite is absent or no longer valid."""


@dataclass(frozen=True)
class LocalReleaseAsset:
    """One locally verified regular file expected in the draft release."""

    name: str
    size: int
    digest: str


def _object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PublicationGateError(f"GitHub returned a duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_nonstandard_json_constant(value: str) -> None:
    raise PublicationGateError(f"GitHub returned a forbidden JSON constant: {value}")


def _decode_api_json(payload: bytes, label: str) -> Any:
    if len(payload) > _MAX_API_RESPONSE_BYTES:
        raise PublicationGateError(f"{label} response exceeded the safety limit")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise PublicationGateError(f"{label} response used a forbidden UTF-8 BOM")
    try:
        text = payload.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except PublicationGateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationGateError(f"{label} response was not strict UTF-8 JSON") from error


def decode_api_document(payload: bytes, label: str) -> dict[str, Any]:
    document = _decode_api_json(payload, label)
    if not isinstance(document, dict):
        raise PublicationGateError(f"{label} response must be a JSON object")
    return document


def decode_api_array(payload: bytes, label: str) -> list[Any]:
    document = _decode_api_json(payload, label)
    if not isinstance(document, list):
        raise PublicationGateError(f"{label} response must be a JSON array")
    return document


def _require_exact_bool(document: Mapping[str, Any], field: str, expected: bool, label: str) -> None:
    value = document.get(field)
    if type(value) is not bool or value is not expected:
        raise PublicationGateError(f"{label}.{field} must be {expected!r}")


def validate_environment(document: Mapping[str, Any], expected_name: str) -> None:
    if document.get("name") != expected_name:
        raise PublicationGateError("the protected publication environment changed identity")
    _require_exact_bool(document, "can_admins_bypass", False, "environment")
    rules = document.get("protection_rules")
    if not isinstance(rules, list):
        raise PublicationGateError("environment.protection_rules must be a list")
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") != "required_reviewers":
            continue
        if type(rule.get("prevent_self_review")) is not bool or rule["prevent_self_review"] is not True:
            continue
        reviewers = rule.get("reviewers")
        if isinstance(reviewers, list) and reviewers and all(isinstance(item, dict) for item in reviewers):
            return
    raise PublicationGateError("the environment requires no independent protected reviewer")


def validate_repository(document: Mapping[str, Any]) -> None:
    _require_exact_bool(document, "has_issues", True, "repository")


def validate_immutable_releases(document: Mapping[str, Any]) -> None:
    _require_exact_bool(document, "enabled", True, "immutable_releases")


def validate_private_vulnerability_reporting(document: Mapping[str, Any]) -> None:
    _require_exact_bool(document, "enabled", True, "private_vulnerability_reporting")


def validate_tag_target(document: Mapping[str, Any], expected_commit: str) -> None:
    if document.get("sha") != expected_commit:
        raise PublicationGateError("the release tag no longer resolves to the workflow commit")


def validate_main_ancestry(document: Mapping[str, Any], expected_commit: str) -> None:
    if document.get("status") not in {"ahead", "identical"}:
        raise PublicationGateError("main no longer descends from the release commit")
    merge_base = document.get("merge_base_commit")
    if not isinstance(merge_base, dict) or merge_base.get("sha") != expected_commit:
        raise PublicationGateError("the release commit is no longer the main comparison merge base")


def validate_workflow_runs(document: Mapping[str, Any], expected_commit: str, workflow: str) -> None:
    runs = document.get("workflow_runs")
    if not isinstance(runs, list):
        raise PublicationGateError(f"{workflow} workflow_runs must be a list")
    for run in runs:
        if not isinstance(run, dict):
            continue
        if (
            run.get("head_sha") == expected_commit
            and run.get("head_branch") == "main"
            and run.get("event") == "push"
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
        ):
            return
    raise PublicationGateError(f"{workflow} has no successful main push run for the release commit")


def validate_publication_state(
    *,
    environment_name: str,
    expected_commit: str,
    environment: Mapping[str, Any],
    repository: Mapping[str, Any],
    immutable_releases: Mapping[str, Any],
    vulnerability_reporting: Mapping[str, Any],
    tag_target: Mapping[str, Any],
    comparison: Mapping[str, Any],
    workflow_runs: Mapping[str, Mapping[str, Any]],
) -> None:
    validate_environment(environment, environment_name)
    validate_repository(repository)
    validate_immutable_releases(immutable_releases)
    validate_private_vulnerability_reporting(vulnerability_reporting)
    validate_tag_target(tag_target, expected_commit)
    validate_main_ancestry(comparison, expected_commit)
    if set(workflow_runs) != set(_REQUIRED_WORKFLOWS):
        raise PublicationGateError("the publication gate did not fetch every required workflow")
    for workflow in _REQUIRED_WORKFLOWS:
        validate_workflow_runs(workflow_runs[workflow], expected_commit, workflow)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_dev,
        value.st_ino,
    )


def _hash_regular_file(path: Path) -> LocalReleaseAsset:
    try:
        before = path.lstat()
    except OSError as error:
        raise PublicationGateError(f"could not inspect local release asset {path.name!r}") from error
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise PublicationGateError(f"local release asset {path.name!r} is not a regular non-symlink file")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _stat_identity(opened) != _stat_identity(before):
            raise PublicationGateError(f"local release asset {path.name!r} changed before hashing")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = None
            while chunk := stream.read(_HASH_CHUNK_SIZE):
                digest.update(chunk)
        after = path.lstat()
    except PublicationGateError:
        raise
    except OSError as error:
        raise PublicationGateError(f"could not hash local release asset {path.name!r}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)

    if path.is_symlink() or _stat_identity(after) != _stat_identity(before):
        raise PublicationGateError(f"local release asset {path.name!r} changed while hashing")
    return LocalReleaseAsset(path.name, before.st_size, f"sha256:{digest.hexdigest()}")


def inventory_local_release_assets(directory: Path) -> dict[str, LocalReleaseAsset]:
    try:
        directory_stat = directory.lstat()
    except OSError as error:
        raise PublicationGateError("the local release-assets directory could not be inspected") from error
    if not stat.S_ISDIR(directory_stat.st_mode) or directory.is_symlink():
        raise PublicationGateError("the local release-assets path must be a non-symlink directory")

    try:
        entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
    except OSError as error:
        raise PublicationGateError("the local release-assets directory could not be enumerated") from error
    if not entries:
        raise PublicationGateError("the local release-assets directory is empty")
    if len(entries) >= _ASSET_API_PAGE_SIZE:
        raise PublicationGateError("the local release-assets directory exceeds the single-page safety limit")

    assets: dict[str, LocalReleaseAsset] = {}
    casefolded_names: set[str] = set()
    for entry in entries:
        folded_name = entry.name.casefold()
        if folded_name in casefolded_names:
            raise PublicationGateError("local release asset names have a case-insensitive collision")
        casefolded_names.add(folded_name)
        asset = _hash_regular_file(entry)
        assets[asset.name] = asset

    try:
        names_after_hashing = sorted(entry.name for entry in directory.iterdir())
    except OSError as error:
        raise PublicationGateError("the local release-assets directory changed while hashing") from error
    if names_after_hashing != sorted(assets):
        raise PublicationGateError("the local release-assets directory changed while hashing")
    return assets


def validate_draft_release(
    document: Mapping[str, Any],
    *,
    expected_tag: str,
    expected_prerelease: bool,
) -> int:
    release_id = document.get("id")
    if type(release_id) is not int or release_id <= 0:
        raise PublicationGateError("draft release.id must be a positive integer")
    if document.get("tag_name") != expected_tag:
        raise PublicationGateError("the draft release changed tag identity")
    _require_exact_bool(document, "draft", True, "release")
    _require_exact_bool(document, "prerelease", expected_prerelease, "release")
    return release_id


def validate_draft_assets(
    document: list[Any],
    *,
    local_assets: Mapping[str, LocalReleaseAsset],
) -> None:
    if len(document) >= _ASSET_API_PAGE_SIZE:
        raise PublicationGateError("the draft asset response reached the single-page safety limit")
    if len(document) != len(local_assets):
        raise PublicationGateError("the draft asset count does not match the local release payload")

    remote_names: set[str] = set()
    remote_casefolded_names: set[str] = set()
    remote_ids: set[int] = set()
    for item in document:
        if not isinstance(item, dict):
            raise PublicationGateError("every draft asset must be a JSON object")
        asset_id = item.get("id")
        if type(asset_id) is not int or asset_id <= 0 or asset_id in remote_ids:
            raise PublicationGateError("draft asset identities must be unique positive integers")
        remote_ids.add(asset_id)
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise PublicationGateError("every draft asset must have a non-empty string name")
        folded_name = name.casefold()
        if name in remote_names or folded_name in remote_casefolded_names:
            raise PublicationGateError("draft asset names contain a duplicate or case-insensitive collision")
        remote_names.add(name)
        remote_casefolded_names.add(folded_name)
        if item.get("state") != "uploaded":
            raise PublicationGateError(f"draft asset {name!r} is not completely uploaded")

        expected = local_assets.get(name)
        if expected is None:
            raise PublicationGateError(f"draft asset {name!r} is not present in the local release payload")
        size = item.get("size")
        if type(size) is not int or size != expected.size:
            raise PublicationGateError(f"draft asset {name!r} has the wrong byte size")
        digest = item.get("digest")
        if not isinstance(digest, str) or digest != expected.digest:
            raise PublicationGateError(f"draft asset {name!r} has the wrong or missing SHA-256 digest")

    if remote_names != set(local_assets):
        raise PublicationGateError("the draft asset names do not exactly match the local release payload")


def _validate_arguments(repository: str, environment: str, tag: str, commit: str) -> None:
    if _REPOSITORY.fullmatch(repository) is None:
        raise PublicationGateError("repository must be an owner/name pair")
    if environment not in _ENVIRONMENTS:
        raise PublicationGateError("publication environment is not allowlisted")
    if _TAG.fullmatch(tag) is None:
        raise PublicationGateError("release tag contains unsupported characters")
    if _COMMIT.fullmatch(commit) is None:
        raise PublicationGateError("release commit must be a lowercase full SHA-1")


def _validate_final_gate_arguments(
    release_assets_directory: Path | None,
    require_draft: bool,
    expected_prerelease: bool | None,
) -> None:
    final_gate_requested = release_assets_directory is not None or require_draft or expected_prerelease is not None
    if not final_gate_requested:
        return
    if release_assets_directory is None or require_draft is not True:
        raise PublicationGateError(
            "final publication validation requires --release-assets-directory and --require-draft"
        )
    if type(expected_prerelease) is not bool:
        raise PublicationGateError("final publication validation requires an exact prerelease expectation")


def _gh_api_payload(path: str, *, fields: Mapping[str, str] | None = None) -> bytes:
    command = [
        "gh",
        "api",
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        f"X-GitHub-Api-Version: {_GITHUB_API_VERSION}",
        path,
    ]
    for key, value in sorted((fields or {}).items()):
        command.extend(("-f", f"{key}={value}"))
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PublicationGateError("the GitHub API command could not complete") from error
    if result.returncode != 0:
        raise PublicationGateError(f"the GitHub API rejected {path!r} with exit code {result.returncode}")
    return result.stdout


def _gh_api(path: str, *, fields: Mapping[str, str] | None = None) -> dict[str, Any]:
    return decode_api_document(_gh_api_payload(path, fields=fields), path)


def _gh_api_array(path: str, *, fields: Mapping[str, str] | None = None) -> list[Any]:
    return decode_api_array(_gh_api_payload(path, fields=fields), path)


def run_gate(
    repository: str,
    environment: str,
    tag: str,
    commit: str,
    *,
    api: Callable[..., dict[str, Any]] = _gh_api,
    array_api: Callable[..., list[Any]] = _gh_api_array,
    release_assets_directory: Path | None = None,
    require_draft: bool = False,
    expected_prerelease: bool | None = None,
) -> None:
    _validate_arguments(repository, environment, tag, commit)
    _validate_final_gate_arguments(release_assets_directory, require_draft, expected_prerelease)
    repository_path = f"repos/{repository}"
    local_assets = None
    if release_assets_directory is not None:
        local_assets = inventory_local_release_assets(release_assets_directory)

    environment_document = api(
        f"{repository_path}/environments/{quote(environment, safe='')}",
    )
    repository_document = api(repository_path)
    immutable_releases_document = api(f"{repository_path}/immutable-releases")
    vulnerability_document = api(f"{repository_path}/private-vulnerability-reporting")
    tag_document = api(f"{repository_path}/commits/{quote(tag, safe='')}")
    comparison_document = api(f"{repository_path}/compare/{commit}...main")
    workflow_documents = {
        workflow: api(
            f"{repository_path}/actions/workflows/{quote(workflow, safe='')}/runs",
            fields={
                "event": "push",
                "head_sha": commit,
                "per_page": "100",
                "status": "completed",
            },
        )
        for workflow in _REQUIRED_WORKFLOWS
    }
    validate_publication_state(
        environment_name=environment,
        expected_commit=commit,
        environment=environment_document,
        repository=repository_document,
        immutable_releases=immutable_releases_document,
        vulnerability_reporting=vulnerability_document,
        tag_target=tag_document,
        comparison=comparison_document,
        workflow_runs=workflow_documents,
    )

    if local_assets is not None:
        release_document = api(
            f"{repository_path}/releases/tags/{quote(tag, safe='')}",
        )
        release_id = validate_draft_release(
            release_document,
            expected_tag=tag,
            expected_prerelease=expected_prerelease,
        )
        assets_document = array_api(
            f"{repository_path}/releases/{release_id}/assets",
            fields={"per_page": str(_ASSET_API_PAGE_SIZE)},
        )
        validate_draft_assets(assets_document, local_assets=local_assets)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--release-assets-directory", type=Path)
    parser.add_argument("--require-draft", action="store_true")
    parser.add_argument("--expected-prerelease", choices=("true", "false"))
    return parser


def main(argv=None) -> int:
    args = create_parser().parse_args(argv)
    expected_prerelease = None
    if args.expected_prerelease is not None:
        expected_prerelease = args.expected_prerelease == "true"
    try:
        run_gate(
            args.repository,
            args.environment,
            args.tag,
            args.commit,
            release_assets_directory=args.release_assets_directory,
            require_draft=args.require_draft,
            expected_prerelease=expected_prerelease,
        )
    except PublicationGateError as error:
        raise SystemExit(f"release publication refused: {error}") from error
    print("All mutable GitHub publication prerequisites are currently valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
