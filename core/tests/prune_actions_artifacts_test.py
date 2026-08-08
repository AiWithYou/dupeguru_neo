import pytest

from scripts.prune_actions_artifacts import PAGE_SIZE, prune_actions_artifacts

CURRENT_SHA = "a" * 40
OLD_SHA = "b" * 40
CURRENT_RUN = 1000
OLD_RUN = 900
REPOSITORY_PATH = "/repos/AiWithYou/dupeguru_neo"


def _artifact(artifact_id, head_sha, run_id, name=None, size=10):
    workflow_run = None if head_sha is None else {"head_sha": head_sha, "id": run_id}
    return {
        "id": artifact_id,
        "name": name or f"artifact-{artifact_id}",
        "size_in_bytes": size,
        "workflow_run": workflow_run,
    }


class FakeGitHub:
    def __init__(self, pages, head_sha=CURRENT_SHA):
        self.pages = pages
        self.head_sha = head_sha
        self.deleted = []
        self.list_requests = 0

    def __call__(self, method, path):
        if method == "GET" and path == REPOSITORY_PATH:
            return {"default_branch": "main"}
        if method == "GET" and path == f"{REPOSITORY_PATH}/git/ref/heads/main":
            return {"object": {"sha": self.head_sha}}
        if method == "GET" and path.startswith(f"{REPOSITORY_PATH}/actions/artifacts?"):
            self.list_requests += 1
            page = int(path.rsplit("page=", 1)[1])
            return {"artifacts": self.pages.get(page, [])}
        if method == "DELETE" and path.startswith(f"{REPOSITORY_PATH}/actions/artifacts/"):
            self.deleted.append(int(path.rsplit("/", 1)[1]))
            return None
        raise AssertionError((method, path))


def test_pruner_keeps_only_current_default_branch_artifacts_across_pages():
    first_page = [_artifact(index, OLD_SHA, OLD_RUN, size=index) for index in range(1, PAGE_SIZE + 1)]
    current = _artifact(PAGE_SIZE + 1, CURRENT_SHA, CURRENT_RUN, name="current", size=500)
    unknown = _artifact(PAGE_SIZE + 2, None, None, size=20)
    github = FakeGitHub({1: first_page, 2: [current, unknown]})

    report = prune_actions_artifacts(
        github,
        "AiWithYou/dupeguru_neo",
        CURRENT_SHA,
        CURRENT_RUN,
        frozenset({"current"}),
    )

    assert report.kept_count == 1
    assert report.kept_bytes == 500
    assert report.deleted_count == PAGE_SIZE + 1
    assert report.deleted_bytes == sum(range(1, PAGE_SIZE + 1)) + 20
    assert github.deleted == list(range(1, PAGE_SIZE + 1)) + [PAGE_SIZE + 2]
    assert github.list_requests == 2


def test_pruner_does_not_delete_when_run_is_no_longer_latest():
    github = FakeGitHub({1: [_artifact(1, OLD_SHA, OLD_RUN)]}, head_sha=OLD_SHA)

    report = prune_actions_artifacts(
        github,
        "AiWithYou/dupeguru_neo",
        CURRENT_SHA,
        CURRENT_RUN,
        frozenset({"current"}),
    )

    assert report.skipped
    assert report.deleted_count == 0
    assert github.deleted == []


@pytest.mark.parametrize(
    ("repository", "commit"),
    [
        ("missing-slash", CURRENT_SHA),
        ("owner/repo/extra", CURRENT_SHA),
        ("owner/repo", "A" * 40),
        ("owner/repo", "a" * 39),
    ],
)
def test_pruner_rejects_ambiguous_scope(repository, commit):
    github = FakeGitHub({1: []})

    with pytest.raises(ValueError):
        prune_actions_artifacts(github, repository, commit, CURRENT_RUN, frozenset({"current"}))


def test_pruner_does_not_delete_until_every_required_current_artifact_exists():
    github = FakeGitHub(
        {
            1: [
                _artifact(1, OLD_SHA, OLD_RUN, name="old"),
                _artifact(2, CURRENT_SHA, CURRENT_RUN, name="current"),
            ]
        }
    )

    with pytest.raises(RuntimeError, match="missing-current"):
        prune_actions_artifacts(
            github,
            "AiWithYou/dupeguru_neo",
            CURRENT_SHA,
            CURRENT_RUN,
            frozenset({"current", "missing-current"}),
        )

    assert github.deleted == []


@pytest.mark.parametrize(
    "artifact",
    [
        {
            "id": True,
            "name": "old",
            "size_in_bytes": 1,
            "workflow_run": {"head_sha": OLD_SHA, "id": OLD_RUN},
        },
        {
            "id": 1,
            "name": "old",
            "size_in_bytes": -1,
            "workflow_run": {"head_sha": OLD_SHA, "id": OLD_RUN},
        },
    ],
)
def test_pruner_fails_closed_on_invalid_artifact_metadata(artifact):
    current = _artifact(2, CURRENT_SHA, CURRENT_RUN, name="current")
    github = FakeGitHub({1: [artifact, current]})

    with pytest.raises(RuntimeError):
        prune_actions_artifacts(
            github,
            "AiWithYou/dupeguru_neo",
            CURRENT_SHA,
            CURRENT_RUN,
            frozenset({"current"}),
        )

    assert github.deleted == []
