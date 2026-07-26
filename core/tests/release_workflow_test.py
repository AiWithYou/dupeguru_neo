import re
from pathlib import Path

from core import __version__

ROOT = Path(__file__).parents[2]
WORKFLOW_DIRECTORY = ROOT.joinpath(".github", "workflows")
FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
USES = re.compile(r"^\s*uses:\s*(?P<action>[^@\s]+)@(?P<revision>[^\s#]+)", re.MULTILINE)


def _workflow(name):
    return WORKFLOW_DIRECTORY.joinpath(name).read_text(encoding="utf-8")


def test_debian_changelog_starts_with_the_application_version():
    changelog = ROOT.joinpath("pkg", "debian", "changelog").read_text(encoding="utf-8")
    assert changelog.startswith(f"dupeguru ({__version__}-1) ")


def test_every_remote_action_is_pinned_to_a_full_commit():
    workflow_paths = sorted(WORKFLOW_DIRECTORY.glob("*.yml")) + sorted(WORKFLOW_DIRECTORY.glob("*.yaml"))
    assert workflow_paths
    for path in workflow_paths:
        text = path.read_text(encoding="utf-8")
        matches = list(USES.finditer(text))
        for match in matches:
            action = match.group("action")
            revision = match.group("revision")
            if action.startswith("./"):
                continue
            assert FULL_COMMIT.fullmatch(revision), (
                f"{path.relative_to(ROOT)} uses mutable action revision " f"{action}@{revision}"
            )


def test_every_workflow_declares_permissions_and_concurrency():
    for path in WORKFLOW_DIRECTORY.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert re.search(r"(?m)^permissions:", text), path
        assert re.search(r"(?m)^concurrency:", text), path
        assert "pull_request_target:" not in text


def test_untrusted_pull_request_ci_is_read_only_and_has_no_secret_reference():
    ci = _workflow("default.yml")
    assert "pull_request:" in ci
    assert re.search(r"(?m)^permissions:\n  contents: read$", ci)
    assert "secrets." not in ci
    assert "id-token: write" not in ci
    assert "contents: write" not in ci


def test_transifex_sync_is_an_optional_push_only_integration():
    workflow = _workflow("tx-push.yml")
    assert re.search(
        r"(?m)^on:\n" r"  push:\n" r"    branches:\n" r"      - master\n" r"    paths:\n" r"      - locale/\*\.pot$",
        workflow,
    )
    assert "pull_request:" not in workflow
    assert "pull_request_target:" not in workflow

    gate = workflow.split(
        "- name: Check optional Transifex integration",
        1,
    )[1].split(
        "- name: Check out the exact revision",
        1,
    )[0]
    assert "TRANSIFEX_CONFIGURED: ${{ secrets.TX_TOKEN != '' }}" in gate
    assert "TX_TOKEN: ${{ secrets.TX_TOKEN }}" not in gate
    assert "enabled=false" in gate
    assert "enabled=true" in gate
    assert "::notice title=Transifex sync skipped::" in gate
    assert "GITHUB_STEP_SUMMARY" in gate
    assert r"optional repository secret \`TX_TOKEN\` is not configured" in gate
    assert "No Transifex client was downloaded" in gate
    assert workflow.count("if: steps.transifex.outputs.enabled == 'true'") == 3


def test_transifex_secret_is_only_given_to_a_digest_verified_fixed_executable():
    workflow = _workflow("tx-push.yml")
    assert "install-transifex.sh" not in workflow
    assert "curl | tar" not in workflow
    assert 'TX_ARCHIVE_SHA256: "dcc747ae863dd5a232b6a322f78b8621f43cd6032189ee89e979418cc24927f2"' in workflow
    assert 'TX_BINARY_SHA256: "32645dc27b82e25d39de6027bbb0ccef5f92a81badac9bd24e736b9eefb63f22"' in workflow
    assert "scripts/verified_transifex.py" in workflow
    assert "--proto-redir '=https'" in workflow
    assert "sha256sum --check --strict" in workflow
    assert 'test ! -L "${tx_executable}"' in workflow
    assert "readlink --canonicalize-existing" in workflow
    assert workflow.count("TX Client, version=${TX_VERSION}") == 2
    assert workflow.count("TX_TOKEN: ${{ secrets.TX_TOKEN }}") == 1
    checkout_and_verification = workflow.split(
        "- name: Check out the exact revision",
        1,
    )[1].split(
        "- name: Update & Push Translation Sources",
        1,
    )[0]
    assert "TX_TOKEN" not in checkout_and_verification
    secret_step = workflow.split(
        "- name: Update & Push Translation Sources",
        1,
    )[1]
    assert "TX_TOKEN: ${{ secrets.TX_TOKEN }}" in secret_step
    assert '"${tx_executable}" push -s --use-git-timestamps' in secret_step
    assert "./tx push" not in secret_step


def test_ci_covers_every_supported_python_on_every_platform_with_pytest_9():
    ci = _workflow("default.yml")
    for version in ("3.10", "3.11", "3.12", "3.13", "3.14"):
        assert f'"{version}"' in ci
    for runner in ("ubuntu-24.04", "windows-2022", "macos-15"):
        assert runner in ci
    assert "macos-14" not in ci
    assert '"pytest==9.1.1"' in ci
    assert "python -m pytest core hscommon qt/tests" in ci
    assert "python -m pytest core hscommon qt/tests" in _workflow("release.yml")
    assert ci.count("--constraint requirements-release.txt") >= 4


def test_package_ci_has_build_validation_and_clean_install_smokes():
    ci = _workflow("default.yml")
    for required in (
        "pyproject-build --outdir dist",
        "scripts/ci_artifact_smoke.py --artifacts dist --twine-check",
        "scripts/ci_artifact_smoke.py --artifacts dist --reproducible-wheel",
        "scripts/ci_artifact_smoke.py",
        "PyQt6==6.11.0",
        "SOURCE_DATE_EPOCH",
        "--constraints requirements-release.txt",
    ):
        assert required in ci
    assert 'PYTHONHASHSEED: "0"' in ci


def test_ci_builds_and_smokes_local_portable_gui_without_uploading_it():
    ci = _workflow("default.yml")
    for required in (
        "Portable GUI smoke",
        "ubuntu-24.04",
        "windows-2022",
        "macos-15",
        '"pyinstaller==6.21.0"',
        '"sphinx==8.1.3"',
        "scripts/portable_bundle.py build",
        "Build and smoke the local frozen GUI without publishing it",
        "requirements-release.txt",
    ):
        assert required in ci
    assert "SOURCE_DATE_EPOCH" in ci
    assert "portable-${{ matrix.platform }}" not in ci
    assert "Upload checked unsigned portable bundle" not in ci


def test_tag_release_is_fail_closed_behind_signing_and_attestation():
    release = _workflow("release.yml")
    assert re.search(r"(?m)^  push:\n    tags:", release)
    assert "pull_request:" not in release
    assert "continue-on-error" not in release
    assert "actions/attest@" in release
    assert release.count("actions/attest@") == 2
    assert "sigstore/gh-action-sigstore-python@" in release
    assert "verify: true" in release
    assert "verify-signatures" in release
    assert release.count("verify-payload") == 2
    assert "--signature-mode forbidden" in release
    assert release.count("--signature-mode required") == 1
    assert release.count("--payload-kind release") == 2
    assert "--payload-kind source-companion" not in release
    assert '"sigstore==4.4.0"' in release
    assert "verify-sigstore" in release
    assert "gh attestation verify" in release
    assert "needs: [validate, attest-and-sign]" in release
    assert "'stable-release'" in release
    assert "actions: read" in release
    assert "contents: write" in release
    assert "id-token: write" in release
    assert "attestations: write" in release
    assert release.count("scripts/release_publication_gate.py") == 3
    assert release.count("repos/${GITHUB_REPOSITORY}/commits/${GITHUB_REF_NAME}") == 2
    assert release.count('test "${resolved_sha}" = "${GITHUB_SHA}"') == 2


def test_tag_release_builds_every_published_wheel_from_one_canonical_sdist():
    release = _workflow("release.yml")
    for required in (
        "Build the one canonical source distribution",
        "pyproject-build --sdist --outdir dist",
        "name: release-sdist",
        "Python package / ${{ matrix.target }} / CPython 3.12",
        "target: linux-x86_64",
        "target: windows-x86_64",
        "target: macos-arm64",
        'python-version: "3.12.13"',
        "Download the exact same canonical source distribution",
        "Build this target's CPython 3.12 wheel from the canonical sdist",
        '"dist/dupeguru_neo-${{ needs.validate.outputs.version }}.tar.gz"',
        "scripts/ci_artifact_smoke.py --artifacts dist --twine-check",
        "scripts/ci_artifact_smoke.py --artifacts dist --reproducible-wheel",
        "Install the wheel and canonical sdist in separate clean environments",
        "name: release-wheel-${{ matrix.target }}",
        "pattern: release-wheel-*",
    ):
        assert required in release
    assert release.count("name: release-sdist") == 3
    assert release.count("name: release-wheel-${{ matrix.target }}") == 1
    assert "release-python" not in release


def test_release_aggregates_real_dependency_closures_from_every_runtime_target():
    release = _workflow("release.yml")
    for required in (
        "Capture this target's installed runtime dependency closure",
        "scripts/release_metadata.py dependency-snapshot",
        '--target "${{ matrix.target }}"',
        "name: release-dependency-snapshot-${{ matrix.target }}",
        "pattern: release-dependency-snapshot-*",
        "Download every target's runtime dependency closure",
        "Generate the aggregate cross-platform CycloneDX SBOM",
        "--dependency-snapshots-directory dependency-snapshots",
    ):
        assert required in release
    assert "Install the built distribution for dependency inventory" not in release


def test_release_publication_requires_master_ancestry_and_successful_mainline_ci():
    release = _workflow("release.yml")
    publication_gate = ROOT.joinpath("scripts", "release_publication_gate.py").read_text(encoding="utf-8")
    for required in (
        "immutable_releases_enabled",
        "has_issues",
        "private-vulnerability-reporting",
        "can_admins_bypass",
        "required_reviewers",
        "prevent_self_review",
        "compare/{commit}...master",
        "default.yml",
        "codeql-analysis.yml",
        '"head_branch") == "master"',
        '"conclusion") == "success"',
    ):
        assert required in publication_gate
    assert release.count("scripts/release_publication_gate.py") == 3
    stable_publish = release.split("- name: Publish the complete stable release", 1)[1].split(
        "- name: Publish the complete pre-release",
        1,
    )[0]
    prerelease_publish = release.split("- name: Publish the complete pre-release", 1)[1]
    for publish_step, expected_prerelease in (
        (stable_publish, "false"),
        (prerelease_publish, "true"),
    ):
        assert publish_step.index("scripts/release_publication_gate.py") < publish_step.index("gh release edit")
        assert "--release-assets-directory dist" in publish_step
        assert "--require-draft" in publish_step
        assert f"--expected-prerelease {expected_prerelease}" in publish_step

    initial_gate = release.split(
        "- name: Require protected publication settings and the original tag target",
        1,
    )[
        1
    ].split("- name: Set up the exact frozen CPython runtime", 1,)[0]
    assert "--release-assets-directory" not in initial_gate
    assert "--require-draft" not in initial_gate
    assert "--expected-prerelease" not in initial_gate


def test_final_publication_gate_binds_the_exact_remote_draft_asset_set():
    publication_gate = ROOT.joinpath("scripts", "release_publication_gate.py").read_text(encoding="utf-8")
    for required in (
        "releases/tags/{quote(tag, safe='')}",
        "releases/{release_id}/assets",
        'fields={"per_page": str(_ASSET_API_PAGE_SIZE)}',
        'item.get("state") != "uploaded"',
        "case-insensitive collision",
        'item.get("digest")',
        "inventory_local_release_assets",
        "regular non-symlink file",
        "len(entries) >= _ASSET_API_PAGE_SIZE",
    ):
        assert required in publication_gate


def test_release_artifacts_survive_protected_environment_approval_waits():
    release = _workflow("release.yml")
    retention_values = re.findall(r"retention-days:\s*([0-9]+)", release)
    assert retention_values
    assert set(retention_values) == {"30"}


def test_release_docs_match_the_multi_target_artifact_and_provenance_contract():
    release_doc = ROOT.joinpath("docs", "RELEASE.md").read_text(encoding="utf-8")
    readme = ROOT.joinpath("README.md").read_text(encoding="utf-8")
    for text in (release_doc, readme):
        for required in (
            "CPython 3.12.13",
            "Linux x86_64",
            "Windows x86_64",
            "macOS arm64",
            "SHA256SUMS",
            "pywin32",
            "Sigstore",
            "OIDC",
            "post-install",
            "not uploaded",
            "complete native",
        ):
            assert required in text
    assert "exactly one wheel" not in release_doc
    assert "not a pip `--require-hashes` lock" in release_doc
    assert "retained for 30 days" in release_doc
    assert "hash-before-install authentication" in readme
    assert "SOURCE-COMPANION-SHA256SUMS" not in release_doc
    assert "unsigned-portable" not in release_doc


def test_release_smokes_portables_but_forbids_them_from_public_assets():
    release = _workflow("release.yml")
    for required in (
        "Non-published portable smoke / ${{ matrix.platform }}",
        "scripts/portable_bundle.py build",
        "enforce-release-policy",
        "--artifacts-directory dist",
        "cp LICENSE dist/LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "HSCOMMON-BSD-3-CLAUSE.txt",
        "requirements-release.txt",
        "release-sources.json",
        "source-archive",
        "verify-source-archive",
        "dupeguru-neo-${RELEASE_VERSION}-source.tar.gz",
        "needs: [validate, assemble]",
        "Portable GUI archives are intentionally not release assets",
    ):
        assert required in release
    assert release.count("platform: linux") == 1
    assert release.count("platform: windows") == 1
    assert release.count("platform: macos") == 1
    assert "pattern: portable-*" not in release
    assert "name: portable-${{ matrix.platform }}" not in release
    assert "source-release/*" not in release
    assert "SOURCE-COMPANION-PROOF.json" not in release
    assert "PORTABLE-NOTICE.txt" not in release
    assert "WINDOWS_SIGNING" not in release
    assert "APPLE_SIGNING" not in release


def test_release_uses_tagged_source_and_never_publishes_a_source_companion():
    release = _workflow("release.yml")
    for required in (
        "needs: [validate, build, package, portable]",
        "dupeguru-neo-${RELEASE_VERSION}-source.tar.gz",
        "verify-source-archive",
        "Upload every stable asset to a non-public draft",
        "--draft=false",
    ):
        assert required in release
    for forbidden in (
        "corresponding-source:",
        "scripts/source_companion.py",
        "source-companion-proof",
        "SOURCE-COMPANION-SHA256SUMS",
        "source-companion.tar",
        "source-release/",
    ):
        assert forbidden not in release


def test_sdist_and_corresponding_source_keep_rebuild_inputs():
    manifest = ROOT.joinpath("MANIFEST.in").read_text(encoding="utf-8")
    for required in (
        "include build.py",
        "include SECURITY.md",
        "include THIRD_PARTY_NOTICES.md",
        "include hscommon/LICENSE",
        "include package.py",
        "include pyproject.toml",
        "include release-sources.json",
        "include requirements-release.txt",
        "include setup.cfg",
        "recursive-include docs",
        "recursive-include images",
        "recursive-include pkg",
        "recursive-include scripts",
    ):
        assert required in manifest
    release = _workflow("release.yml")
    assert "source-archive" in release
    assert '--commit "${GITHUB_SHA}"' in release
    setup = ROOT.joinpath("setup.cfg").read_text(encoding="utf-8")
    for license_file in ("LICENSE", "THIRD_PARTY_NOTICES.md", "hscommon/LICENSE"):
        assert license_file in setup
    notice = ROOT.joinpath("THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    incorporated_license = ROOT.joinpath("hscommon", "LICENSE").read_text(encoding="utf-8")
    fenced_license = notice.split("```text\n", 1)[1].split("\n```", 1)[0] + "\n"
    assert fenced_license == incorporated_license


def test_nsis_and_frozen_windows_payload_keep_required_distribution_notices():
    package = ROOT.joinpath("package.py").read_text(encoding="utf-8")
    installer = ROOT.joinpath("setup.nsi").read_text(encoding="utf-8")
    for required in (
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "hscommon",
        "release-sources.json",
        "requirements-release.txt",
        "PORTABLE-NOTICE.txt",
    ):
        assert required in package
        assert required in installer
    for installed_name in (
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "HSCOMMON-BSD-3-CLAUSE.txt",
        "release-sources.json",
        "requirements-release.txt",
        "PORTABLE-NOTICE.txt",
    ):
        assert f"File /oname={installed_name}" in installer


def test_release_metadata_and_reproducible_build_inputs_are_pinned():
    pyproject = ROOT.joinpath("pyproject.toml").read_text(encoding="utf-8")
    tox = ROOT.joinpath("tox.ini").read_text(encoding="utf-8")
    for build_requirement in (
        '"polib==1.2.0"',
        '"setuptools==83.0.0"',
        '"Sphinx==8.1.3"',
        '"wheel==0.47.0"',
    ):
        assert build_requirement in pyproject
    assert 'minversion = "9.0"' in pyproject
    assert "envlist = py310,py311,py312,py313,py314" in tox
    assert "pytest==9.1.1" in tox
    assert "-c{toxinidir}/requirements-release.txt" in tox
    lock = ROOT.joinpath("requirements-release.txt").read_text(encoding="utf-8")
    for expected in (
        "distro==1.9.0",
        "mutagen==1.48.1",
        "Pillow==12.3.0",
        "PyQt6==6.11.0",
        "PyQt6-Qt6==6.11.1",
        "PyQt6_sip==13.11.1",
        "semantic-version==2.10.0",
        "xxhash==3.8.1",
        'pywin32==312; sys_platform == "win32"',
    ):
        assert expected in lock
    release = _workflow("release.yml")
    assert release.count("--constraint requirements-release.txt") >= 7
    assert release.count("--lock requirements-release.txt") >= 2
    setup_py = ROOT.joinpath("setup.py").read_text(encoding="utf-8")
    for required in (
        'os.environ.get("SOURCE_DATE_EPOCH") is not None',
        'compiler_type == "msvc"',
        'compiler_type == "unix"',
        '"/Brepro"',
        '"/experimental:deterministic"',
        'f"/pathmap:{source_root}=."',
        'f"-ffile-prefix-map={source_root}=."',
        'f"-fdebug-prefix-map={source_root}=."',
        "self.compiler.compile(",
        "def byte_compile(self, files):",
        'build_root.rglob("__pycache__")',
    ):
        assert required in setup_py
    assert 'PYTHONHASHSEED: "0"' in release
    assert "scripts/ci_artifact_smoke.py --artifacts dist --reproducible-wheel" in release
