# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Plan and verify the permanent desktop pre-release publication."""

from __future__ import annotations

import argparse
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

if __package__:
    from .release_publication_gate import (
        LocalReleaseAsset,
        PublicationGateError,
        _gh_api,
        _gh_api_array,
        inventory_local_release_assets,
        validate_draft_assets,
    )
else:
    from release_publication_gate import (
        LocalReleaseAsset,
        PublicationGateError,
        _gh_api,
        _gh_api_array,
        inventory_local_release_assets,
        validate_draft_assets,
    )


_COMMIT = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_CHECKSUM = re.compile(r"(?P<digest>[0-9a-f]{64}) \*(?P<name>[^\r\n]+)\n")
_REMOTE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_ASSET_PAGE_SIZE = 100
_MAX_GUIDE_BYTES = 16 * 1024


class DesktopReleaseError(RuntimeError):
    """The desktop release is incomplete, stale, or internally inconsistent."""


@dataclass(frozen=True)
class DesktopReleasePlan:
    """One idempotent publication decision for the exact current main commit."""

    version: str
    tag: str
    publish: bool
    reason: str


def _validate_identity(repository: str, commit: str) -> None:
    if _REPOSITORY.fullmatch(repository) is None:
        raise DesktopReleaseError("repository must be an owner/name pair")
    if _COMMIT.fullmatch(commit) is None:
        raise DesktopReleaseError("commit must be a lowercase full SHA-1")


def read_project_version(project_root: Path) -> str:
    """Read one stable three-part version without importing the source tree."""

    version_file = project_root.resolve(strict=True).joinpath("core", "__init__.py")
    try:
        payload = version_file.read_bytes()
    except OSError as error:
        raise DesktopReleaseError("core/__init__.py could not be read") from error
    if payload.startswith(b"\xef\xbb\xbf"):
        raise DesktopReleaseError("core/__init__.py must not contain a UTF-8 BOM")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise DesktopReleaseError("core/__init__.py must be strict UTF-8") from error
    matches = re.findall(r'^__version__ = "([^"]+)"$', text, flags=re.MULTILINE)
    if len(matches) != 1 or _VERSION.fullmatch(matches[0]) is None:
        raise DesktopReleaseError("core.__version__ must be one stable MAJOR.MINOR.PATCH value")
    return matches[0]


def expected_asset_names(version: str) -> frozenset[str]:
    if _VERSION.fullmatch(version) is None:
        raise DesktopReleaseError("desktop release version must be MAJOR.MINOR.PATCH")
    windows = f"dupeguru-neo-{version}-windows-x86_64-unsigned.exe"
    macos = f"dupeguru-neo-{version}-macos-arm64-adhoc.app.zip"
    return frozenset(
        {
            windows,
            f"{windows}.sha256",
            "README-WINDOWS.txt",
            macos,
            f"{macos}.sha256",
            "README-MACOS.txt",
        }
    )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise DesktopReleaseError(f"GitHub returned an invalid {label}")
    return value


def _require_exact_bool(document: Mapping[str, Any], field: str, expected: bool, label: str) -> None:
    value = document.get(field)
    if type(value) is not bool or value is not expected:
        raise DesktopReleaseError(f"{label}.{field} must be {expected!r}")


def _default_branch_head(api: Callable[..., Mapping[str, Any]], repository: str) -> str:
    repository_path = f"repos/{repository}"
    metadata = _require_mapping(api(repository_path), "repository response")
    if metadata.get("default_branch") != "main":
        raise DesktopReleaseError("the repository default branch must remain main")
    reference = _require_mapping(api(f"{repository_path}/git/ref/heads/main"), "main reference")
    target = _require_mapping(reference.get("object"), "main reference target")
    commit = target.get("sha")
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise DesktopReleaseError("GitHub returned an invalid main commit")
    return commit


def _exact_tag_target(
    array_api: Callable[..., list[Any]],
    repository: str,
    tag: str,
) -> str | None:
    documents = array_api(
        f"repos/{repository}/git/matching-refs/tags/{quote(tag, safe='')}",
    )
    if len(documents) >= _ASSET_PAGE_SIZE:
        raise DesktopReleaseError("matching tag response reached the safety limit")
    expected_ref = f"refs/tags/{tag}"
    matches = []
    for document in documents:
        document = _require_mapping(document, "tag reference")
        if document.get("ref") != expected_ref:
            continue
        target = _require_mapping(document.get("object"), "tag target")
        if target.get("type") != "commit":
            raise DesktopReleaseError("desktop release tags must be lightweight commit tags")
        commit = target.get("sha")
        if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
            raise DesktopReleaseError("desktop release tag has an invalid target")
        matches.append(commit)
    if len(matches) > 1:
        raise DesktopReleaseError("GitHub returned duplicate exact desktop tag references")
    return matches[0] if matches else None


def _validate_existing_asset_records(documents: list[Any], version: str) -> None:
    expected_names = expected_asset_names(version)
    if len(documents) >= _ASSET_PAGE_SIZE:
        raise DesktopReleaseError("existing release assets reached the single-page safety limit")
    if len(documents) != len(expected_names):
        raise DesktopReleaseError("the existing desktop release has the wrong asset count")
    names = set()
    folded_names = set()
    identities = set()
    for value in documents:
        asset = _require_mapping(value, "release asset")
        asset_id = asset.get("id")
        if type(asset_id) is not int or asset_id <= 0 or asset_id in identities:
            raise DesktopReleaseError("release asset ids must be unique positive integers")
        identities.add(asset_id)
        name = asset.get("name")
        if not isinstance(name, str) or name not in expected_names:
            raise DesktopReleaseError("the existing desktop release has an unexpected asset")
        folded_name = name.casefold()
        if name in names or folded_name in folded_names:
            raise DesktopReleaseError("existing desktop release asset names collide")
        names.add(name)
        folded_names.add(folded_name)
        if asset.get("state") != "uploaded":
            raise DesktopReleaseError(f"existing release asset {name!r} is not uploaded")
        size = asset.get("size")
        if type(size) is not int or size <= 0:
            raise DesktopReleaseError(f"existing release asset {name!r} has an invalid size")
        digest = asset.get("digest")
        if not isinstance(digest, str) or _REMOTE_DIGEST.fullmatch(digest) is None:
            raise DesktopReleaseError(f"existing release asset {name!r} has no valid SHA-256")
    if names != set(expected_names):
        raise DesktopReleaseError("the existing desktop release asset set is incomplete")


def _validate_existing_release(
    api: Callable[..., Mapping[str, Any]],
    array_api: Callable[..., list[Any]],
    repository: str,
    version: str,
    tag: str,
    tag_target: str,
) -> None:
    release = _require_mapping(
        api(f"repos/{repository}/releases/tags/{quote(tag, safe='')}"),
        "desktop release",
    )
    release_id = release.get("id")
    if type(release_id) is not int or release_id <= 0:
        raise DesktopReleaseError("existing desktop release has an invalid id")
    if release.get("tag_name") != tag:
        raise DesktopReleaseError("existing desktop release changed tag identity")
    _require_exact_bool(release, "draft", False, "release")
    _require_exact_bool(release, "prerelease", True, "release")
    published_at = release.get("published_at")
    if not isinstance(published_at, str) or not published_at:
        raise DesktopReleaseError("existing desktop release has no publication time")
    body = release.get("body")
    source_url = f"https://github.com/{repository}/tree/{tag_target}"
    if not isinstance(body, str) or source_url not in body:
        raise DesktopReleaseError("existing desktop release does not name its exact source")
    assets = array_api(
        f"repos/{repository}/releases/{release_id}/assets",
        fields={"per_page": str(_ASSET_PAGE_SIZE)},
    )
    _validate_existing_asset_records(assets, version)


def _validate_ci_run(
    api: Callable[..., Mapping[str, Any]],
    repository: str,
    commit: str,
    ci_run_id: int,
) -> None:
    run = _require_mapping(
        api(f"repos/{repository}/actions/runs/{ci_run_id}"),
        "CI workflow run",
    )
    if run.get("id") != ci_run_id:
        raise DesktopReleaseError("CI workflow run changed identity")
    expected = {
        "name": "CI",
        "path": ".github/workflows/default.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": commit,
        "status": "completed",
        "conclusion": "success",
    }
    for field, value in expected.items():
        if run.get(field) != value:
            raise DesktopReleaseError(f"CI workflow run has unexpected {field}")


def _codeql_state(document: Mapping[str, Any], commit: str) -> str:
    runs = document.get("workflow_runs")
    if not isinstance(runs, list):
        raise DesktopReleaseError("CodeQL workflow_runs must be a list")
    matching_conclusions = []
    for value in runs:
        if not isinstance(value, dict):
            continue
        if (
            value.get("head_sha") == commit
            and value.get("head_branch") == "main"
            and value.get("event") == "push"
            and value.get("status") == "completed"
        ):
            matching_conclusions.append(value.get("conclusion"))
    if "success" in matching_conclusions:
        return "success"
    if matching_conclusions:
        return "failed"
    return "pending"


def _wait_for_codeql(
    api: Callable[..., Mapping[str, Any]],
    repository: str,
    commit: str,
    *,
    wait_seconds: int,
    poll_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    deadline = monotonic() + wait_seconds
    fields = {
        "event": "push",
        "head_sha": commit,
        "per_page": "100",
        "status": "completed",
    }
    while True:
        document = _require_mapping(
            api(
                f"repos/{repository}/actions/workflows/codeql-analysis.yml/runs",
                fields=fields,
            ),
            "CodeQL workflow runs",
        )
        state = _codeql_state(document, commit)
        if state == "success":
            return
        if state == "failed":
            raise DesktopReleaseError("CodeQL did not succeed for the exact main commit")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise DesktopReleaseError("CodeQL did not complete successfully before the publication deadline")
        sleep(min(poll_seconds, remaining))


def plan_desktop_release(
    repository: str,
    commit: str,
    ci_run_id: int,
    project_root: Path,
    *,
    api: Callable[..., Mapping[str, Any]] = _gh_api,
    array_api: Callable[..., list[Any]] = _gh_api_array,
    wait_seconds: int = 1800,
    poll_seconds: float = 15.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> DesktopReleasePlan:
    """Publish one new version only from successful, exact-current main checks."""

    _validate_identity(repository, commit)
    if type(ci_run_id) is not int or ci_run_id <= 0:
        raise DesktopReleaseError("CI run id must be a positive integer")
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 3600:
        raise DesktopReleaseError("CodeQL wait must be an integer from 0 through 3600 seconds")
    if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) or not 1 <= poll_seconds <= 60:
        raise DesktopReleaseError("CodeQL poll interval must be from 1 through 60 seconds")

    version = read_project_version(project_root)
    tag = f"desktop-{version}"
    tag_target = _exact_tag_target(array_api, repository, tag)
    if tag_target is not None:
        _validate_existing_release(api, array_api, repository, version, tag, tag_target)
        return DesktopReleasePlan(version, tag, False, "already-published")

    if _default_branch_head(api, repository) != commit:
        return DesktopReleasePlan(version, tag, False, "superseded-main")
    _validate_ci_run(api, repository, commit, ci_run_id)
    _wait_for_codeql(
        api,
        repository,
        commit,
        wait_seconds=wait_seconds,
        poll_seconds=float(poll_seconds),
        monotonic=monotonic,
        sleep=sleep,
    )
    if _default_branch_head(api, repository) != commit:
        return DesktopReleasePlan(version, tag, False, "superseded-main")
    if _exact_tag_target(array_api, repository, tag) is not None:
        raise DesktopReleaseError("the desktop tag appeared while publication was being planned")
    return DesktopReleasePlan(version, tag, True, "new-version")


def _read_small_utf8(path: Path, label: str, maximum_bytes: int) -> str:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise DesktopReleaseError(f"{label} could not be read") from error
    if not 0 < len(payload) <= maximum_bytes:
        raise DesktopReleaseError(f"{label} size is outside the safety limit")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise DesktopReleaseError(f"{label} must not contain a UTF-8 BOM")
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise DesktopReleaseError(f"{label} must be strict UTF-8") from error


def _verify_checksum_sidecar(
    directory: Path,
    assets: Mapping[str, LocalReleaseAsset],
    artifact_name: str,
) -> str:
    sidecar_name = f"{artifact_name}.sha256"
    text = _read_small_utf8(directory.joinpath(sidecar_name), sidecar_name, 512)
    match = _CHECKSUM.fullmatch(text)
    if match is None or match.group("name") != artifact_name:
        raise DesktopReleaseError(f"{sidecar_name} has an invalid checksum record")
    digest = match.group("digest")
    if assets[artifact_name].digest != f"sha256:{digest}":
        raise DesktopReleaseError(f"{sidecar_name} does not match the artifact bytes")
    return digest


def _verify_guide(directory: Path, filename: str, version: str, repository: str, commit: str) -> None:
    guide = _read_small_utf8(directory.joinpath(filename), filename, _MAX_GUIDE_BYTES)
    required = [
        f"dupeGuru Neo {version}\n",
        f"https://github.com/{repository}/tree/{commit}",
        "An exact copy may be published permanently in a desktop-* GitHub pre-release.",
        "This is not an official signed stable release asset.",
    ]
    if filename == "README-WINDOWS.txt":
        required.extend(("EXEをダブルクリック", "not Authenticode-signed"))
    else:
        required.extend(("dupeguru-neo.app", "not Developer ID signed or notarized"))
    missing = [text for text in required if text not in guide]
    if missing:
        raise DesktopReleaseError(f"{filename} is missing required publication guidance")
    if guide.count(f"https://github.com/{repository}/tree/{commit}") != 1:
        raise DesktopReleaseError(f"{filename} must name the exact source exactly once")


def _release_notes(
    repository: str,
    commit: str,
    version: str,
    windows_digest: str,
    macos_digest: str,
) -> str:
    tag = f"desktop-{version}"
    windows = f"dupeguru-neo-{version}-windows-x86_64-unsigned.exe"
    macos = f"dupeguru-neo-{version}-macos-arm64-adhoc.app.zip"
    source_url = f"https://github.com/{repository}/tree/{commit}"
    return f"""# dupeGuru Neo {version} 恒久公開デスクトップ版

Windows と Apple Silicon macOS 向けの、ログイン不要・有効期限なしのデスクトップ版です。
高速画像レビュー「シンギュラリティ」と、最新の安全性・安定性改善を含みます。

## ダウンロード

- Windows 10 / 11、64ビット: `{windows}`
- macOS Apple Silicon / arm64: `{macos}`

各本体と同名の `.sha256` ファイル、および OS 別 README も同梱しています。

## 安全性

これは恒久公開するデスクトップ・プレリリースです。正式な署名済み安定版では
ありません。Windows EXE は Authenticode 未署名、macOS APP は ad-hoc 署名のみで
Apple の公証を受けていないため、SmartScreen または Gatekeeper が警告を表示する
場合があります。ファイル操作は、バイト単位の完全一致を実行直前に再検証した
場合だけ、復元可能な隔離へ進めます。

## SHA-256

```text
{windows_digest}  {windows}
{macos_digest}  {macos}
```

Built from [`{commit}`]({source_url}) and published as the `{tag}` desktop
pre-release after the exact main CI and CodeQL runs succeeded.

---

English summary: This is the permanent, no-login desktop download for Windows
10/11 x86_64 and Apple Silicon macOS. It includes the fast “Singularity” image
review flow. The Windows executable is not Authenticode-signed; the macOS app
is ad-hoc signed and not notarized. This remains a desktop pre-release rather
than an officially signed stable release.
"""


def _inventory_desktop_assets(directory: Path) -> dict[str, LocalReleaseAsset]:
    try:
        return inventory_local_release_assets(directory)
    except PublicationGateError as error:
        raise DesktopReleaseError(str(error)) from error


def verify_local_desktop_assets(
    directory: Path,
    repository: str,
    commit: str,
    version: str,
    *,
    notes_output: Path | None = None,
) -> dict[str, LocalReleaseAsset]:
    """Verify the exact six-file desktop payload and optionally write its notes."""

    _validate_identity(repository, commit)
    if _VERSION.fullmatch(version) is None:
        raise DesktopReleaseError("desktop release version must be MAJOR.MINOR.PATCH")
    directory = directory.resolve(strict=True)
    assets = _inventory_desktop_assets(directory)
    expected_names = expected_asset_names(version)
    if set(assets) != set(expected_names):
        missing = sorted(expected_names - set(assets))
        extra = sorted(set(assets) - expected_names)
        raise DesktopReleaseError(f"desktop release asset set differs: missing={missing}, extra={extra}")

    windows = f"dupeguru-neo-{version}-windows-x86_64-unsigned.exe"
    macos = f"dupeguru-neo-{version}-macos-arm64-adhoc.app.zip"
    windows_digest = _verify_checksum_sidecar(directory, assets, windows)
    macos_digest = _verify_checksum_sidecar(directory, assets, macos)
    _verify_guide(directory, "README-WINDOWS.txt", version, repository, commit)
    _verify_guide(directory, "README-MACOS.txt", version, repository, commit)

    assets_after_reading = _inventory_desktop_assets(directory)
    if assets_after_reading != assets:
        raise DesktopReleaseError("desktop release assets changed during verification")
    if notes_output is not None:
        notes_path = notes_output.resolve(strict=False)
        if notes_path.parent == directory or directory in notes_path.parents:
            raise DesktopReleaseError("release notes must be outside the asset directory")
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        notes_path.write_text(
            _release_notes(repository, commit, version, windows_digest, macos_digest),
            encoding="utf-8",
            newline="\n",
        )
    return assets


def verify_remote_desktop_release(
    repository: str,
    commit: str,
    version: str,
    directory: Path,
    *,
    published: bool,
    ci_run_id: int | None = None,
    api: Callable[..., Mapping[str, Any]] = _gh_api,
    array_api: Callable[..., list[Any]] = _gh_api_array,
) -> None:
    """Bind a draft or public GitHub pre-release to the verified local bytes."""

    local_assets = verify_local_desktop_assets(directory, repository, commit, version)
    if not published:
        if type(ci_run_id) is not int or ci_run_id <= 0:
            raise DesktopReleaseError("draft publication requires the exact CI run id")
        if _default_branch_head(api, repository) != commit:
            raise DesktopReleaseError("the draft desktop release commit is no longer current main")
        _validate_ci_run(api, repository, commit, ci_run_id)
        _wait_for_codeql(
            api,
            repository,
            commit,
            wait_seconds=0,
            poll_seconds=1.0,
            monotonic=time.monotonic,
            sleep=time.sleep,
        )
    tag = f"desktop-{version}"
    if _exact_tag_target(array_api, repository, tag) != commit:
        raise DesktopReleaseError("desktop release tag does not resolve to the expected commit")
    release = _require_mapping(
        api(f"repos/{repository}/releases/tags/{quote(tag, safe='')}"),
        "desktop release",
    )
    release_id = release.get("id")
    if type(release_id) is not int or release_id <= 0:
        raise DesktopReleaseError("desktop release has an invalid id")
    if release.get("tag_name") != tag:
        raise DesktopReleaseError("desktop release changed tag identity")
    _require_exact_bool(release, "draft", not published, "release")
    _require_exact_bool(release, "prerelease", True, "release")
    body = release.get("body")
    source_url = f"https://github.com/{repository}/tree/{commit}"
    if not isinstance(body, str) or source_url not in body:
        raise DesktopReleaseError("desktop release notes do not name the exact source")
    if published:
        published_at = release.get("published_at")
        if not isinstance(published_at, str) or not published_at:
            raise DesktopReleaseError("published desktop release has no publication time")
    documents = array_api(
        f"repos/{repository}/releases/{release_id}/assets",
        fields={"per_page": str(_ASSET_PAGE_SIZE)},
    )
    try:
        validate_draft_assets(documents, local_assets=local_assets)
    except PublicationGateError as error:
        raise DesktopReleaseError(str(error)) from error


def _write_plan_outputs(path: Path, plan: DesktopReleasePlan) -> None:
    values = {
        "publish": str(plan.publish).lower(),
        "reason": plan.reason,
        "tag": plan.tag,
        "version": plan.version,
    }
    if any("\n" in value or "\r" in value for value in values.values()):
        raise DesktopReleaseError("GitHub output values must be one line")
    try:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            for key, value in sorted(values.items()):
                stream.write(f"{key}={value}\n")
    except OSError as error:
        raise DesktopReleaseError("GITHUB_OUTPUT could not be written") from error


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="decide whether this exact main build starts a new release")
    plan.add_argument("--repository", required=True)
    plan.add_argument("--commit", required=True)
    plan.add_argument("--ci-run-id", required=True, type=int)
    plan.add_argument("--project-root", required=True, type=Path)
    plan.add_argument("--github-output", required=True, type=Path)
    plan.add_argument("--wait-seconds", type=int, default=1800)
    plan.add_argument("--poll-seconds", type=float, default=15.0)

    local = subparsers.add_parser("verify-local", help="verify downloaded desktop assets")
    local.add_argument("--repository", required=True)
    local.add_argument("--commit", required=True)
    local.add_argument("--version", required=True)
    local.add_argument("--directory", required=True, type=Path)
    local.add_argument("--notes-output", required=True, type=Path)

    remote = subparsers.add_parser("verify-remote", help="verify a draft or published desktop release")
    remote.add_argument("--repository", required=True)
    remote.add_argument("--commit", required=True)
    remote.add_argument("--version", required=True)
    remote.add_argument("--directory", required=True, type=Path)
    remote.add_argument("--ci-run-id", type=int)
    state = remote.add_mutually_exclusive_group(required=True)
    state.add_argument("--draft", action="store_true")
    state.add_argument("--published", action="store_true")
    return parser


def main(argv=None) -> int:
    args = create_parser().parse_args(argv)
    try:
        if args.command == "plan":
            plan = plan_desktop_release(
                args.repository,
                args.commit,
                args.ci_run_id,
                args.project_root,
                wait_seconds=args.wait_seconds,
                poll_seconds=args.poll_seconds,
            )
            _write_plan_outputs(args.github_output, plan)
            print(f"Desktop release plan: publish={str(plan.publish).lower()} reason={plan.reason} tag={plan.tag}")
        elif args.command == "verify-local":
            assets = verify_local_desktop_assets(
                args.directory,
                args.repository,
                args.commit,
                args.version,
                notes_output=args.notes_output,
            )
            print(f"Verified {len(assets)} exact local desktop release assets.")
        else:
            verify_remote_desktop_release(
                args.repository,
                args.commit,
                args.version,
                args.directory,
                published=args.published,
                ci_run_id=args.ci_run_id,
            )
            state = "published" if args.published else "draft"
            print(f"Verified the exact {state} desktop release and all six assets.")
    except (DesktopReleaseError, PublicationGateError) as error:
        raise SystemExit(f"desktop release refused: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
