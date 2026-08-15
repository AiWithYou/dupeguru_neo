import hashlib

import pytest

from scripts import desktop_release

REPOSITORY = "AiWithYou/dupeguru_neo"
COMMIT = "a" * 40
OLD_COMMIT = "b" * 40
RUN_ID = 1234
VERSION = "5.4.1"


def _project(tmp_path, version=VERSION):
    root = tmp_path / "project"
    (root / "core").mkdir(parents=True)
    (root / "core" / "__init__.py").write_text(
        f'__version__ = "{version}"\n',
        encoding="utf-8",
        newline="\n",
    )
    return root


def _expected_names(version=VERSION):
    return desktop_release.expected_asset_names(version)


def _remote_assets(version=VERSION, *, local_directory=None):
    result = []
    for index, name in enumerate(sorted(_expected_names(version)), start=1):
        if local_directory is None:
            size = index
            digest = f"sha256:{index:064x}"
        else:
            payload = (local_directory / name).read_bytes()
            size = len(payload)
            digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        result.append(
            {
                "id": index,
                "name": name,
                "size": size,
                "digest": digest,
                "state": "uploaded",
            }
        )
    return result


class FakeGitHub:
    def __init__(self, *, head=COMMIT, tag_target=None, codeql="success", release=None, assets=None):
        self.head = head
        self.tag_target = tag_target
        self.codeql = codeql
        self.release = release
        self.assets = assets

    def api(self, path, *, fields=None):
        if path == f"repos/{REPOSITORY}":
            return {"default_branch": "main"}
        if path == f"repos/{REPOSITORY}/git/ref/heads/main":
            return {"object": {"sha": self.head}}
        if path == f"repos/{REPOSITORY}/actions/runs/{RUN_ID}":
            return {
                "id": RUN_ID,
                "name": "CI",
                "path": ".github/workflows/default.yml",
                "event": "push",
                "head_branch": "main",
                "head_sha": COMMIT,
                "status": "completed",
                "conclusion": "success",
            }
        if path == f"repos/{REPOSITORY}/actions/workflows/codeql-analysis.yml/runs":
            assert fields == {
                "event": "push",
                "head_sha": COMMIT,
                "per_page": "100",
                "status": "completed",
            }
            if self.codeql == "pending":
                return {"workflow_runs": []}
            return {
                "workflow_runs": [
                    {
                        "head_sha": COMMIT,
                        "head_branch": "main",
                        "event": "push",
                        "status": "completed",
                        "conclusion": self.codeql,
                    }
                ]
            }
        if path == f"repos/{REPOSITORY}/releases/tags/desktop-{VERSION}":
            assert self.release is not None
            return self.release
        raise AssertionError((path, fields))

    def array_api(self, path, *, fields=None):
        if path == f"repos/{REPOSITORY}/releases":
            assert fields == {"per_page": "100"}
            return [] if self.release is None else [self.release]
        if path == f"repos/{REPOSITORY}/git/matching-refs/tags/desktop-{VERSION}":
            if self.tag_target is None:
                return []
            return [
                {
                    "ref": f"refs/tags/desktop-{VERSION}",
                    "object": {"type": "commit", "sha": self.tag_target},
                }
            ]
        if path == f"repos/{REPOSITORY}/releases/99/assets":
            assert fields == {"per_page": "100"}
            assert self.assets is not None
            return self.assets
        raise AssertionError((path, fields))


def _asset_directory(tmp_path, *, bad_windows_checksum=False, extra=False):
    directory = tmp_path / "desktop-dist"
    directory.mkdir()
    windows = f"dupeguru-neo-{VERSION}-windows-x86_64-unsigned.exe"
    macos = f"dupeguru-neo-{VERSION}-macos-arm64-adhoc.app.zip"
    (directory / windows).write_bytes(b"MZ exact Windows payload")
    (directory / macos).write_bytes(b"PK exact macOS payload")
    for name in (windows, macos):
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if name == windows and bad_windows_checksum:
            digest = "0" * 64
        (directory / f"{name}.sha256").write_text(
            f"{digest} *{name}\n",
            encoding="utf-8",
            newline="\n",
        )
    common = (
        f"dupeGuru Neo {VERSION}\n\n"
        "Exact source / 対応ソース:\n"
        f"https://github.com/{REPOSITORY}/tree/{COMMIT}\n\n"
        "An exact copy may be published permanently in a desktop-* GitHub pre-release.\n"
        "This is not an official signed stable release asset.\n"
    )
    (directory / "README-WINDOWS.txt").write_text(
        common + "EXEをダブルクリックしてください。\nThis development EXE is not Authenticode-signed.\n",
        encoding="utf-8",
        newline="\n",
    )
    (directory / "README-MACOS.txt").write_text(
        common
        + "dupeguru-neo.appをApplicationsへ移動してください。\n"
        + "This development APP is not Developer ID signed or notarized.\n",
        encoding="utf-8",
        newline="\n",
    )
    if extra:
        (directory / "debug.log").write_text("not public\n", encoding="utf-8")
    return directory


def test_plan_publishes_one_new_version_from_exact_successful_main(tmp_path):
    github = FakeGitHub()

    plan = desktop_release.plan_desktop_release(
        REPOSITORY,
        COMMIT,
        RUN_ID,
        _project(tmp_path),
        api=github.api,
        array_api=github.array_api,
        wait_seconds=0,
    )

    assert plan == desktop_release.DesktopReleasePlan(VERSION, f"desktop-{VERSION}", True, "new-version")


def test_plan_skips_a_main_commit_that_was_superseded_before_publication(tmp_path):
    github = FakeGitHub(head=OLD_COMMIT)

    plan = desktop_release.plan_desktop_release(
        REPOSITORY,
        COMMIT,
        RUN_ID,
        _project(tmp_path),
        api=github.api,
        array_api=github.array_api,
        wait_seconds=0,
    )

    assert not plan.publish
    assert plan.reason == "superseded-main"


def test_plan_refuses_a_failed_exact_codeql_run(tmp_path):
    github = FakeGitHub(codeql="failure")

    with pytest.raises(desktop_release.DesktopReleaseError, match="CodeQL did not succeed"):
        desktop_release.plan_desktop_release(
            REPOSITORY,
            COMMIT,
            RUN_ID,
            _project(tmp_path),
            api=github.api,
            array_api=github.array_api,
            wait_seconds=0,
        )


def test_plan_requires_a_complete_existing_public_release_before_skipping(tmp_path):
    source_url = f"https://github.com/{REPOSITORY}/tree/{OLD_COMMIT}"
    github = FakeGitHub(
        tag_target=OLD_COMMIT,
        release={
            "id": 99,
            "tag_name": f"desktop-{VERSION}",
            "draft": False,
            "prerelease": True,
            "published_at": "2026-08-15T00:00:00Z",
            "body": f"Built from {source_url}",
        },
        assets=_remote_assets(),
    )

    plan = desktop_release.plan_desktop_release(
        REPOSITORY,
        COMMIT,
        RUN_ID,
        _project(tmp_path),
        api=github.api,
        array_api=github.array_api,
        wait_seconds=0,
    )

    assert not plan.publish
    assert plan.reason == "already-published"


def test_plan_does_not_hide_an_incomplete_existing_release(tmp_path):
    github = FakeGitHub(
        tag_target=OLD_COMMIT,
        release={
            "id": 99,
            "tag_name": f"desktop-{VERSION}",
            "draft": True,
            "prerelease": True,
            "published_at": None,
            "body": f"https://github.com/{REPOSITORY}/tree/{OLD_COMMIT}",
        },
        assets=_remote_assets(),
    )

    with pytest.raises(desktop_release.DesktopReleaseError, match="release.draft"):
        desktop_release.plan_desktop_release(
            REPOSITORY,
            COMMIT,
            RUN_ID,
            _project(tmp_path),
            api=github.api,
            array_api=github.array_api,
            wait_seconds=0,
        )


def test_local_payload_binds_both_sidecars_guides_source_and_notes(tmp_path):
    directory = _asset_directory(tmp_path)
    notes = tmp_path / "release-notes.md"

    assets = desktop_release.verify_local_desktop_assets(
        directory,
        REPOSITORY,
        COMMIT,
        VERSION,
        notes_output=notes,
    )

    assert set(assets) == set(_expected_names())
    notes_text = notes.read_text(encoding="utf-8")
    assert "恒久公開デスクトップ版" in notes_text
    assert f"https://github.com/{REPOSITORY}/tree/{COMMIT}" in notes_text
    for name in _expected_names():
        if name.endswith((".exe", ".app.zip")):
            assert name in notes_text


@pytest.mark.parametrize(
    ("bad_checksum", "extra", "message"),
    [
        (True, False, "does not match"),
        (False, True, "asset set differs"),
    ],
)
def test_local_payload_fails_closed_on_changed_or_extra_files(tmp_path, bad_checksum, extra, message):
    directory = _asset_directory(tmp_path, bad_windows_checksum=bad_checksum, extra=extra)

    with pytest.raises(desktop_release.DesktopReleaseError, match=message):
        desktop_release.verify_local_desktop_assets(directory, REPOSITORY, COMMIT, VERSION)


@pytest.mark.parametrize("published", [False, True])
def test_remote_gate_binds_draft_and_public_release_assets_to_local_bytes(tmp_path, published):
    directory = _asset_directory(tmp_path)
    github = FakeGitHub(
        tag_target=COMMIT,
        release={
            "id": 99,
            "tag_name": f"desktop-{VERSION}",
            "draft": not published,
            "prerelease": True,
            "published_at": "2026-08-15T00:00:00Z" if published else None,
            "body": f"Built from https://github.com/{REPOSITORY}/tree/{COMMIT}",
        },
        assets=_remote_assets(local_directory=directory),
    )

    desktop_release.verify_remote_desktop_release(
        REPOSITORY,
        COMMIT,
        VERSION,
        directory,
        published=published,
        ci_run_id=RUN_ID,
        api=github.api,
        array_api=github.array_api,
    )


def test_draft_gate_rechecks_current_main_immediately_before_publication(tmp_path):
    directory = _asset_directory(tmp_path)
    github = FakeGitHub(
        head=OLD_COMMIT,
        tag_target=COMMIT,
        release={
            "id": 99,
            "tag_name": f"desktop-{VERSION}",
            "draft": True,
            "prerelease": True,
            "published_at": None,
            "body": f"Built from https://github.com/{REPOSITORY}/tree/{COMMIT}",
        },
        assets=_remote_assets(local_directory=directory),
    )

    with pytest.raises(desktop_release.DesktopReleaseError, match="no longer current main"):
        desktop_release.verify_remote_desktop_release(
            REPOSITORY,
            COMMIT,
            VERSION,
            directory,
            published=False,
            ci_run_id=RUN_ID,
            api=github.api,
            array_api=github.array_api,
        )


def test_draft_gate_requires_one_exact_release_listing_match(tmp_path):
    directory = _asset_directory(tmp_path)
    github = FakeGitHub(tag_target=COMMIT, release=None)

    with pytest.raises(desktop_release.DesktopReleaseError, match="exactly one draft"):
        desktop_release.verify_remote_desktop_release(
            REPOSITORY,
            COMMIT,
            VERSION,
            directory,
            published=False,
            ci_run_id=RUN_ID,
            api=github.api,
            array_api=github.array_api,
        )


@pytest.mark.parametrize("value", ["5.4", "5.4.1rc1", "5.4.1+local", "v5.4.1"])
def test_project_publication_version_must_be_stable_three_part(tmp_path, value):
    with pytest.raises(desktop_release.DesktopReleaseError, match="MAJOR.MINOR.PATCH"):
        desktop_release.read_project_version(_project(tmp_path, value))
