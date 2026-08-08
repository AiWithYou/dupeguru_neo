"""Keep GitHub Actions artifacts only for the current default-branch commit."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PAGE_SIZE = 100
MAX_PAGES = 1_000


@dataclass(frozen=True)
class PruneReport:
    kept_count: int
    kept_bytes: int
    deleted_count: int
    deleted_bytes: int
    skipped: bool = False


def _require_mapping(value, description: str) -> dict:
    if not isinstance(value, dict):
        raise RuntimeError(f"GitHub returned an invalid {description}")
    return value


def _github_request(api_url: str, token: str, method: str, path: str):
    url = f"{api_url.rstrip('/')}{path}"
    request = urllib.request.Request(
        url,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "dupeguru-neo-artifact-pruner",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
        if method == "DELETE":
            if payload:
                raise RuntimeError("GitHub returned unexpected data after deleting an artifact")
            return None
        if not payload:
            raise RuntimeError("GitHub returned an empty response")
        try:
            return json.loads(payload)
        except (TypeError, ValueError) as error:
            raise RuntimeError("GitHub returned invalid JSON") from error


def _repository_path(repository: str) -> str:
    if not REPOSITORY.fullmatch(repository):
        raise ValueError("GITHUB_REPOSITORY must be an owner/name pair")
    owner, name = repository.split("/", 1)
    return f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"


def _default_branch_head(call_api, repository_path: str) -> str:
    repository = _require_mapping(call_api("GET", repository_path), "repository response")
    default_branch = repository.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise RuntimeError("GitHub did not report a default branch")
    encoded_branch = urllib.parse.quote(default_branch, safe="")
    reference = _require_mapping(
        call_api("GET", f"{repository_path}/git/ref/heads/{encoded_branch}"),
        "default-branch reference",
    )
    object_metadata = _require_mapping(reference.get("object"), "default-branch object")
    head_sha = object_metadata.get("sha")
    if not isinstance(head_sha, str) or not FULL_COMMIT.fullmatch(head_sha):
        raise RuntimeError("GitHub returned an invalid default-branch commit")
    return head_sha


def _list_artifacts(call_api, repository_path: str) -> list[dict]:
    artifacts_by_id: dict[int, dict] = {}
    for page in range(1, MAX_PAGES + 1):
        payload = _require_mapping(
            call_api(
                "GET",
                f"{repository_path}/actions/artifacts?per_page={PAGE_SIZE}&page={page}",
            ),
            "artifact listing",
        )
        page_artifacts = payload.get("artifacts")
        if not isinstance(page_artifacts, list):
            raise RuntimeError("GitHub returned an invalid artifact list")
        for artifact in page_artifacts:
            artifact = _require_mapping(artifact, "artifact record")
            artifact_id = artifact.get("id")
            if not isinstance(artifact_id, int) or isinstance(artifact_id, bool) or artifact_id <= 0:
                raise RuntimeError("GitHub returned an invalid artifact id")
            previous = artifacts_by_id.setdefault(artifact_id, artifact)
            if previous != artifact:
                raise RuntimeError("GitHub returned conflicting records for one artifact")
        if len(page_artifacts) < PAGE_SIZE:
            return list(artifacts_by_id.values())
    raise RuntimeError("GitHub artifact pagination exceeded its explicit limit")


def _artifact_size(artifact: dict) -> int:
    size = artifact.get("size_in_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise RuntimeError("GitHub returned an invalid artifact size")
    return size


def _artifact_generation(artifact: dict) -> tuple[str | None, int | None]:
    workflow_run = artifact.get("workflow_run")
    if not isinstance(workflow_run, dict):
        return None, None
    head_sha = workflow_run.get("head_sha")
    run_id = workflow_run.get("id")
    if not isinstance(head_sha, str):
        head_sha = None
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        run_id = None
    return head_sha, run_id


def _artifact_name(artifact: dict) -> str:
    name = artifact.get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("GitHub returned an invalid artifact name")
    return name


def prune_actions_artifacts(
    call_api,
    repository: str,
    keep_sha: str,
    keep_run_id: int,
    required_names: frozenset[str],
) -> PruneReport:
    """Delete every artifact not produced from the current default-branch head."""

    if not FULL_COMMIT.fullmatch(keep_sha):
        raise ValueError("GITHUB_SHA must be a lowercase 40-character commit")
    if not isinstance(keep_run_id, int) or isinstance(keep_run_id, bool) or keep_run_id <= 0:
        raise ValueError("GITHUB_RUN_ID must be a positive integer")
    if not required_names or any(not isinstance(name, str) or not name for name in required_names):
        raise ValueError("required artifact names must be non-empty strings")
    repository_path = _repository_path(repository)
    artifacts = _list_artifacts(call_api, repository_path)
    if _default_branch_head(call_api, repository_path) != keep_sha:
        return PruneReport(0, 0, 0, 0, skipped=True)

    keep_generation = (keep_sha, keep_run_id)
    kept = [artifact for artifact in artifacts if _artifact_generation(artifact) == keep_generation]
    kept_names = {_artifact_name(artifact) for artifact in kept}
    missing_names = sorted(required_names - kept_names)
    if missing_names:
        raise RuntimeError("latest artifact generation is incomplete: " + ", ".join(missing_names))
    obsolete = [artifact for artifact in artifacts if _artifact_generation(artifact) != keep_generation]
    kept_bytes = sum(_artifact_size(artifact) for artifact in kept)
    deleted_bytes = sum(_artifact_size(artifact) for artifact in obsolete)
    for artifact in sorted(obsolete, key=lambda item: item["id"]):
        call_api("DELETE", f"{repository_path}/actions/artifacts/{artifact['id']}")
    return PruneReport(
        kept_count=len(kept),
        kept_bytes=kept_bytes,
        deleted_count=len(obsolete),
        deleted_bytes=deleted_bytes,
    )


def main() -> int:
    api_url = os.environ.get("GITHUB_API_URL", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    keep_sha = os.environ.get("GITHUB_SHA", "")
    run_id_text = os.environ.get("GITHUB_RUN_ID", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not api_url.startswith("https://"):
        raise SystemExit("GITHUB_API_URL must be an HTTPS URL")
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")
    if not run_id_text.isdecimal():
        raise SystemExit("GITHUB_RUN_ID must be a positive integer")
    run_id = int(run_id_text)

    def call_api(method: str, path: str):
        return _github_request(api_url, token, method, path)

    report = prune_actions_artifacts(
        call_api,
        repository,
        keep_sha,
        run_id,
        frozenset(
            {
                f"dupeguru-neo-windows-exe-{keep_sha}",
                f"dupeguru-neo-macos-app-{keep_sha}",
                "packages-Windows",
                "packages-Linux",
                "packages-macOS",
            }
        ),
    )
    if report.skipped:
        print(f"Skipped artifact pruning because {keep_sha} is no longer the default-branch head")
        return 0
    print(
        "Kept {kept} latest artifacts ({kept_bytes} bytes); "
        "deleted {deleted} obsolete artifacts ({deleted_bytes} bytes)".format(
            kept=report.kept_count,
            kept_bytes=report.kept_bytes,
            deleted=report.deleted_count,
            deleted_bytes=report.deleted_bytes,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
