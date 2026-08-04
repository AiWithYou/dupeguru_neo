import copy
import hashlib
from types import SimpleNamespace

import pytest

from scripts import release_publication_gate as gate

COMMIT = "a" * 40


def valid_state():
    return {
        "environment_name": "stable-release",
        "expected_commit": COMMIT,
        "environment": {
            "name": "stable-release",
            "can_admins_bypass": False,
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "prevent_self_review": True,
                    "reviewers": [{"type": "User", "reviewer": {"login": "reviewer"}}],
                }
            ],
        },
        "repository": {
            "has_issues": True,
        },
        "immutable_releases": {"enabled": True},
        "vulnerability_reporting": {"enabled": True},
        "tag_target": {"sha": COMMIT},
        "comparison": {
            "status": "ahead",
            "merge_base_commit": {"sha": COMMIT},
        },
        "workflow_runs": {
            "default.yml": {
                "workflow_runs": [
                    {
                        "head_sha": COMMIT,
                        "head_branch": "main",
                        "event": "push",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
            "codeql-analysis.yml": {
                "workflow_runs": [
                    {
                        "head_sha": COMMIT,
                        "head_branch": "main",
                        "event": "push",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
        },
    }


def validate(state):
    gate.validate_publication_state(**state)


def test_complete_publication_state_is_accepted():
    validate(valid_state())


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("environment", "name"), "prerelease"),
        (("environment", "can_admins_bypass"), True),
        (("environment", "can_admins_bypass"), 0),
        (("environment", "protection_rules"), None),
        (("environment", "protection_rules", 0, "prevent_self_review"), False),
        (("environment", "protection_rules", 0, "reviewers"), []),
        (("environment", "protection_rules", 0, "reviewers"), ["not-an-object"]),
        (("immutable_releases", "enabled"), False),
        (("immutable_releases", "enabled"), 1),
        (("repository", "has_issues"), False),
        (("vulnerability_reporting", "enabled"), False),
        (("tag_target", "sha"), "b" * 40),
        (("comparison", "status"), "diverged"),
        (("comparison", "merge_base_commit", "sha"), "b" * 40),
        (("workflow_runs", "default.yml", "workflow_runs"), []),
        (("workflow_runs", "codeql-analysis.yml", "workflow_runs", 0, "event"), "pull_request"),
    ],
)
def test_mutated_publication_prerequisite_is_rejected(path, value):
    state = valid_state()
    target = state
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(gate.PublicationGateError):
        validate(state)


def test_workflow_set_must_be_exact():
    state = valid_state()
    del state["workflow_runs"]["codeql-analysis.yml"]
    with pytest.raises(gate.PublicationGateError, match="every required workflow"):
        validate(state)

    state = valid_state()
    state["workflow_runs"]["unexpected.yml"] = copy.deepcopy(state["workflow_runs"]["default.yml"])
    with pytest.raises(gate.PublicationGateError, match="every required workflow"):
        validate(state)


def test_successful_workflow_run_may_appear_after_an_unrelated_run():
    state = valid_state()
    state["workflow_runs"]["default.yml"]["workflow_runs"].insert(
        0,
        {
            "head_sha": "b" * 40,
            "head_branch": "feature",
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
        },
    )
    validate(state)


def test_api_json_is_strict_utf8_object_without_duplicate_keys_or_bom():
    assert gate.decode_api_document(b'{"enabled":true}', "test") == {"enabled": True}

    with pytest.raises(gate.PublicationGateError, match="duplicate JSON key"):
        gate.decode_api_document(b'{"enabled":true,"enabled":false}', "test")
    with pytest.raises(gate.PublicationGateError, match="BOM"):
        gate.decode_api_document(b'\xef\xbb\xbf{"enabled":true}', "test")
    with pytest.raises(gate.PublicationGateError, match="UTF-8 JSON"):
        gate.decode_api_document(b"\xff", "test")
    with pytest.raises(gate.PublicationGateError, match="JSON object"):
        gate.decode_api_document(b"[]", "test")


def test_api_json_size_is_bounded(monkeypatch):
    monkeypatch.setattr(gate, "_MAX_API_RESPONSE_BYTES", 4)
    with pytest.raises(gate.PublicationGateError, match="safety limit"):
        gate.decode_api_document(b'{"a":1}', "test")


def test_asset_api_json_is_a_strict_bounded_array_without_duplicate_keys(monkeypatch):
    assert gate.decode_api_array(b'[{"id":1}]', "assets") == [{"id": 1}]
    with pytest.raises(gate.PublicationGateError, match="JSON array"):
        gate.decode_api_array(b'{"id":1}', "assets")
    with pytest.raises(gate.PublicationGateError, match="duplicate JSON key"):
        gate.decode_api_array(b'[{"id":1,"id":2}]', "assets")
    with pytest.raises(gate.PublicationGateError, match="BOM"):
        gate.decode_api_array(b"\xef\xbb\xbf[]", "assets")

    monkeypatch.setattr(gate, "_MAX_API_RESPONSE_BYTES", 1)
    with pytest.raises(gate.PublicationGateError, match="safety limit"):
        gate.decode_api_array(b"[]", "assets")


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_api_json_rejects_nonstandard_nonfinite_constants(constant):
    with pytest.raises(gate.PublicationGateError, match="forbidden JSON constant"):
        gate.decode_api_document(b'{"value":' + constant + b"}", "object")
    with pytest.raises(gate.PublicationGateError, match="forbidden JSON constant"):
        gate.decode_api_array(b"[" + constant + b"]", "array")


@pytest.mark.parametrize(
    ("repository", "environment", "tag", "commit"),
    [
        ("owner", "stable-release", "v5.0.0", COMMIT),
        ("owner/repo/extra", "stable-release", "v5.0.0", COMMIT),
        ("owner/repo", "production", "v5.0.0", COMMIT),
        ("owner/repo", "stable-release", "5.0.0", COMMIT),
        ("owner/repo", "stable-release", "v5.0.0/other", COMMIT),
        ("owner/repo", "stable-release", "v5.0.0", COMMIT.upper()),
        ("owner/repo", "stable-release", "v5.0.0", "a" * 39),
    ],
)
def test_gate_arguments_are_allowlisted(repository, environment, tag, commit):
    with pytest.raises(gate.PublicationGateError):
        gate.run_gate(repository, environment, tag, commit, api=lambda *args, **kwargs: {})


def test_run_gate_fetches_all_state_with_bounded_queries():
    state = valid_state()
    calls = []

    def api(path, *, fields=None):
        calls.append((path, fields))
        if path.endswith("/environments/stable-release"):
            return state["environment"]
        if path.endswith("/private-vulnerability-reporting"):
            return state["vulnerability_reporting"]
        if path.endswith("/immutable-releases"):
            return state["immutable_releases"]
        if path.endswith("/commits/v5.0.0"):
            return state["tag_target"]
        if path.endswith(f"/compare/{COMMIT}...main"):
            return state["comparison"]
        for workflow in ("default.yml", "codeql-analysis.yml"):
            if path.endswith(f"/actions/workflows/{workflow}/runs"):
                return state["workflow_runs"][workflow]
        if path == "repos/owner/repo":
            return state["repository"]
        raise AssertionError(path)

    gate.run_gate("owner/repo", "stable-release", "v5.0.0", COMMIT, api=api)

    assert len(calls) == 8
    workflow_calls = [item for item in calls if item[0].endswith("/runs")]
    assert len(workflow_calls) == 2
    for _, fields in workflow_calls:
        assert fields == {
            "event": "push",
            "head_sha": COMMIT,
            "per_page": "100",
            "status": "completed",
        }


def _local_payload(tmp_path):
    directory = tmp_path.joinpath("dist")
    directory.mkdir()
    directory.joinpath("alpha.bin").write_bytes(b"alpha")
    directory.joinpath("beta.bin").write_bytes(b"beta payload")
    return directory


def _remote_assets(directory):
    result = []
    for asset_id, path in enumerate(sorted(directory.iterdir()), start=101):
        content = path.read_bytes()
        result.append(
            {
                "id": asset_id,
                "name": path.name,
                "state": "uploaded",
                "size": len(content),
                "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
            }
        )
    return result


def test_draft_asset_inventory_exactly_matches_local_regular_files(tmp_path):
    directory = _local_payload(tmp_path)
    local_assets = gate.inventory_local_release_assets(directory)
    gate.validate_draft_assets(_remote_assets(directory), local_assets=local_assets)


def test_local_asset_inventory_rejects_non_files_and_a_full_api_page(tmp_path):
    directory = tmp_path.joinpath("nested-payload")
    directory.mkdir()
    directory.joinpath("unexpected-directory").mkdir()
    with pytest.raises(gate.PublicationGateError, match="regular non-symlink"):
        gate.inventory_local_release_assets(directory)

    directory = tmp_path.joinpath("too-many-assets")
    directory.mkdir()
    for index in range(100):
        directory.joinpath(f"{index:03}.bin").write_bytes(b"")
    with pytest.raises(gate.PublicationGateError, match="single-page safety limit"):
        gate.inventory_local_release_assets(directory)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda assets: assets[0].update(state="open"), "not completely uploaded"),
        (lambda assets: assets[0].pop("digest"), "wrong or missing SHA-256"),
        (lambda assets: assets[0].update(size=999), "wrong byte size"),
        (lambda assets: assets[0].update(digest="sha256:" + ("0" * 64)), "wrong or missing SHA-256"),
        (lambda assets: assets[0].update(name="renamed.bin"), "not present"),
    ],
)
def test_draft_asset_mutation_is_rejected(tmp_path, mutation, message):
    directory = _local_payload(tmp_path)
    assets = _remote_assets(directory)
    mutation(assets)
    with pytest.raises(gate.PublicationGateError, match=message):
        gate.validate_draft_assets(
            assets,
            local_assets=gate.inventory_local_release_assets(directory),
        )


def test_draft_asset_extra_and_missing_files_are_rejected(tmp_path):
    directory = _local_payload(tmp_path)
    local_assets = gate.inventory_local_release_assets(directory)
    assets = _remote_assets(directory)

    with pytest.raises(gate.PublicationGateError, match="count"):
        gate.validate_draft_assets(assets[:-1], local_assets=local_assets)

    extra = copy.deepcopy(assets)
    extra.append(
        {
            "id": 999,
            "name": "extra.bin",
            "state": "uploaded",
            "size": 0,
            "digest": f"sha256:{hashlib.sha256(b'').hexdigest()}",
        }
    )
    with pytest.raises(gate.PublicationGateError, match="count"):
        gate.validate_draft_assets(extra, local_assets=local_assets)


@pytest.mark.parametrize("case_collision", [False, True])
def test_draft_asset_duplicate_or_case_colliding_names_are_rejected(tmp_path, case_collision):
    directory = _local_payload(tmp_path)
    local_assets = gate.inventory_local_release_assets(directory)
    assets = _remote_assets(directory)
    assets[1]["name"] = assets[0]["name"].upper() if case_collision else assets[0]["name"]

    with pytest.raises(gate.PublicationGateError, match="duplicate or case-insensitive collision"):
        gate.validate_draft_assets(assets, local_assets=local_assets)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("tag_name", "v5.0.1", "tag identity"),
        ("draft", False, "release.draft"),
        ("draft", 1, "release.draft"),
        ("prerelease", True, "release.prerelease"),
        ("id", 0, "positive integer"),
        ("id", True, "positive integer"),
    ],
)
def test_draft_release_identity_and_state_are_exact(field, value, message):
    document = {
        "id": 42,
        "tag_name": "v5.0.0",
        "draft": True,
        "prerelease": False,
    }
    document[field] = value
    with pytest.raises(gate.PublicationGateError, match=message):
        gate.validate_draft_release(
            document,
            expected_tag="v5.0.0",
            expected_prerelease=False,
        )


def test_draft_release_accepts_the_expected_prerelease_state():
    assert (
        gate.validate_draft_release(
            {
                "id": 42,
                "tag_name": "v5.0.0rc1",
                "draft": True,
                "prerelease": True,
            },
            expected_tag="v5.0.0rc1",
            expected_prerelease=True,
        )
        == 42
    )


@pytest.mark.parametrize(
    ("directory", "require_draft", "expected_prerelease"),
    [
        (None, True, False),
        ("placeholder", False, False),
        ("placeholder", True, None),
        ("placeholder", True, 0),
    ],
)
def test_final_gate_options_must_be_supplied_as_one_exact_contract(
    directory,
    require_draft,
    expected_prerelease,
):
    with pytest.raises(gate.PublicationGateError, match="final publication|prerelease expectation"):
        gate.run_gate(
            "owner/repo",
            "stable-release",
            "v5.0.0",
            COMMIT,
            release_assets_directory=directory,
            require_draft=require_draft,
            expected_prerelease=expected_prerelease,
            api=lambda *args, **kwargs: {},
        )


def test_final_gate_refetches_mutable_state_then_checks_draft_assets_last(tmp_path):
    directory = _local_payload(tmp_path)
    state = valid_state()
    calls = []
    release_id = 42

    def api(path, *, fields=None):
        calls.append(("object", path, fields))
        if path.endswith("/releases/tags/v5.0.0"):
            return {
                "id": release_id,
                "tag_name": "v5.0.0",
                "draft": True,
                "prerelease": False,
            }
        if path.endswith("/environments/stable-release"):
            return state["environment"]
        if path.endswith("/private-vulnerability-reporting"):
            return state["vulnerability_reporting"]
        if path.endswith("/immutable-releases"):
            return state["immutable_releases"]
        if path.endswith("/commits/v5.0.0"):
            return state["tag_target"]
        if path.endswith(f"/compare/{COMMIT}...main"):
            return state["comparison"]
        for workflow in ("default.yml", "codeql-analysis.yml"):
            if path.endswith(f"/actions/workflows/{workflow}/runs"):
                return state["workflow_runs"][workflow]
        if path == "repos/owner/repo":
            return state["repository"]
        raise AssertionError(path)

    def array_api(path, *, fields=None):
        calls.append(("array", path, fields))
        return _remote_assets(directory)

    gate.run_gate(
        "owner/repo",
        "stable-release",
        "v5.0.0",
        COMMIT,
        api=api,
        array_api=array_api,
        release_assets_directory=directory,
        require_draft=True,
        expected_prerelease=False,
    )

    assert len(calls) == 10
    assert calls[0][1].endswith("/environments/stable-release")
    assert calls[-2:] == [
        ("object", "repos/owner/repo/releases/tags/v5.0.0", None),
        ("array", f"repos/owner/repo/releases/{release_id}/assets", {"per_page": "100"}),
    ]


def test_gh_api_rejects_command_failure_without_copying_stderr(monkeypatch):
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=b"secret must not be copied",
        ),
    )
    with pytest.raises(gate.PublicationGateError) as raised:
        gate._gh_api("repos/owner/repo")
    assert "secret" not in str(raised.value)


def test_gh_api_passes_fixed_get_method_and_sorted_query_fields(monkeypatch):
    observed = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(gate.subprocess, "run", run)
    assert gate._gh_api("repos/owner/repo", fields={"z": "2", "a": "1"}) == {}
    assert observed["command"] == [
        "gh",
        "api",
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2026-03-10",
        "repos/owner/repo",
        "-f",
        "a=1",
        "-f",
        "z=2",
    ]
    assert observed["kwargs"] == {
        "check": False,
        "capture_output": True,
        "timeout": 60,
    }


def test_gh_array_api_uses_one_bounded_page_and_strict_array_decoder(monkeypatch):
    observed = {}

    def run(command, **kwargs):
        observed["command"] = command
        return SimpleNamespace(returncode=0, stdout=b'[{"id":1}]', stderr=b"")

    monkeypatch.setattr(gate.subprocess, "run", run)
    assert gate._gh_api_array("repos/owner/repo/releases/42/assets", fields={"per_page": "100"}) == [{"id": 1}]
    assert observed["command"][-2:] == ["-f", "per_page=100"]
