import hashlib
import json
import os
import subprocess
import tarfile

import pytest

from scripts import release_metadata


class FakeDistribution:
    def __init__(self, name, version, requires=()):
        self.metadata = {"Name": name}
        self.version = version
        self.requires = list(requires)


def test_generate_checksums_is_sorted_and_rejects_preexisting_signature_files(tmp_path):
    (tmp_path / "z.whl").write_bytes(b"z")
    (tmp_path / "a.tar.gz").write_bytes(b"a")
    output = tmp_path / "SHA256SUMS"

    release_metadata.generate_checksums(tmp_path, output)

    expected_a = hashlib.sha256(b"a").hexdigest()
    expected_z = hashlib.sha256(b"z").hexdigest()
    assert output.read_text(encoding="ascii").splitlines() == [
        f"{expected_a} *a.tar.gz",
        f"{expected_z} *z.whl",
    ]

    (tmp_path / "z.whl.sigstore.json").write_text(
        json.dumps({"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="forbidden before release signing"):
        release_metadata.generate_checksums(tmp_path, tmp_path / "SECOND-SHA256SUMS")


@pytest.mark.parametrize(
    "name",
    (
        "release.sig",
        "release.bundle",
        "release.crt",
        "release.pem",
        "release.key",
        "release.p12",
        "release.pfx",
        "release.jks",
        "release.keystore",
    ),
)
def test_release_payload_rejects_reserved_signature_and_secret_extensions(
    tmp_path,
    name,
):
    (tmp_path / "package.whl").write_bytes(b"wheel")
    (tmp_path / name).write_bytes(b"must not be published")

    with pytest.raises(RuntimeError, match="forbidden release credential"):
        release_metadata.verify_release_payload_layout(
            tmp_path,
            signature_mode="forbidden",
        )


def test_signed_checksum_payload_requires_one_bundle_per_subject(tmp_path):
    artifact = tmp_path / "package.whl"
    artifact.write_bytes(b"wheel")
    checksums = tmp_path / "SHA256SUMS"
    release_metadata.generate_checksums(tmp_path, checksums)
    bundle_payload = json.dumps({"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"})
    artifact.with_name(artifact.name + ".sigstore.json").write_text(
        bundle_payload,
        encoding="utf-8",
    )
    checksums.with_name(checksums.name + ".sigstore.json").write_text(
        bundle_payload,
        encoding="utf-8",
    )

    release_metadata.verify_checksums(tmp_path, checksums)
    release_metadata.verify_release_payload_layout(
        tmp_path,
        signature_mode="required",
    )

    (tmp_path / "orphan.sigstore.json").write_text(
        bundle_payload,
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="orphaned"):
        release_metadata.verify_checksums(tmp_path, checksums)


def test_sigstore_bundle_rejects_duplicate_json_keys(tmp_path):
    (tmp_path / "package.whl").write_bytes(b"wheel")
    (tmp_path / "package.whl.sigstore.json").write_text(
        '{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json",'
        '"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="duplicate JSON key"):
        release_metadata.verify_release_payload_layout(
            tmp_path,
            signature_mode="required",
        )


def _write_complete_release_subjects(directory, version="5.0.0"):
    names = {
        "BUILD-METADATA.json",
        "HSCOMMON-BSD-3-CLAUSE.txt",
        "LICENSE",
        "SHA256SUMS",
        "THIRD_PARTY_NOTICES.md",
        "release-sources.json",
        "requirements-release.txt",
        f"dupeguru-neo-{version}-source.tar.gz",
        f"dupeguru-neo-{version}.cdx.json",
        f"dupeguru_neo-{version}-cp312-cp312-linux_x86_64.whl",
        f"dupeguru_neo-{version}-cp312-cp312-macosx_11_0_arm64.whl",
        f"dupeguru_neo-{version}-cp312-cp312-win_amd64.whl",
        f"dupeguru_neo-{version}.tar.gz",
    }
    for name in names:
        directory.joinpath(name).write_bytes(b"release subject")
    return names


def test_release_payload_contract_rejects_unknown_flat_files(tmp_path):
    _write_complete_release_subjects(tmp_path)

    release_metadata.verify_release_payload_contract(
        tmp_path,
        version="5.0.0",
        payload_kind="release",
        signature_mode="forbidden",
    )

    (tmp_path / "debug.log").write_text("private build paths", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"unexpected=.*debug\.log"):
        release_metadata.verify_release_payload_contract(
            tmp_path,
            version="5.0.0",
            payload_kind="release",
            signature_mode="forbidden",
        )


def test_signed_release_contract_requires_bundles_for_the_exact_allowlist(tmp_path):
    names = _write_complete_release_subjects(tmp_path)
    bundle_payload = json.dumps({"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"})
    for name in names:
        tmp_path.joinpath(name + ".sigstore.json").write_text(
            bundle_payload,
            encoding="utf-8",
        )

    release_metadata.verify_release_payload_contract(
        tmp_path,
        version="5.0.0",
        payload_kind="release",
        signature_mode="required",
    )


def test_release_contract_requires_one_frozen_abi_wheel_per_runtime_target(tmp_path):
    names = _write_complete_release_subjects(tmp_path)
    missing_wheel = next(name for name in names if name.endswith("-win_amd64.whl"))
    tmp_path.joinpath(missing_wheel).unlink()

    with pytest.raises(RuntimeError, match="one CPython 3.12 wheel"):
        release_metadata.verify_release_payload_contract(
            tmp_path,
            version="5.0.0",
            payload_kind="release",
            signature_mode="forbidden",
        )

    tmp_path.joinpath(missing_wheel).write_bytes(b"release subject")
    linux_wheel = next(name for name in names if name.endswith("-linux_x86_64.whl"))
    tmp_path.joinpath(linux_wheel).rename(tmp_path / linux_wheel.replace("-cp312-cp312-", "-cp313-cp313-"))
    with pytest.raises(RuntimeError, match="frozen CPython 3.12 ABI"):
        release_metadata.verify_release_payload_contract(
            tmp_path,
            version="5.0.0",
            payload_kind="release",
            signature_mode="forbidden",
        )


def test_release_contract_requires_the_exact_canonical_sdist_filename(tmp_path):
    _write_complete_release_subjects(tmp_path)
    canonical = tmp_path / "dupeguru_neo-5.0.0.tar.gz"
    canonical.rename(tmp_path / "dupeguru_neo-5.0.tar.gz")

    with pytest.raises(RuntimeError, match="canonical sdist"):
        release_metadata.verify_release_payload_contract(
            tmp_path,
            version="5.0.0",
            payload_kind="release",
            signature_mode="forbidden",
        )


@pytest.mark.parametrize(
    "name",
    (
        "dupeguru-neo-5.0.0-linux-x86_64-unsigned-portable.tar.gz",
        "dupeguru-neo-5.0.0-macos-arm64-unsigned-portable.tar.gz",
        "dupeguru-neo-5.0.0-windows-x86_64-unsigned-portable.zip",
        "SOURCE-COMPANION-PROOF.json",
        "SOURCE-COMPANION-SHA256SUMS",
        "dupeguru-neo-5.0.0-source-companion.tar",
    ),
)
def test_official_release_contract_forbids_portable_and_source_companion_assets(
    tmp_path,
    name,
):
    _write_complete_release_subjects(tmp_path)
    tmp_path.joinpath(name).write_bytes(b"must not be published")

    with pytest.raises(RuntimeError, match="must not contain portable or source-companion"):
        release_metadata.verify_release_payload_contract(
            tmp_path,
            version="5.0.0",
            payload_kind="release",
            signature_mode="forbidden",
        )


def test_source_companion_payload_has_a_separate_two_subject_allowlist(tmp_path):
    subjects = {
        "SOURCE-COMPANION-SHA256SUMS",
        "dupeguru-neo-5.0.0-source-companion.tar",
    }
    bundle_payload = json.dumps({"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"})
    for name in subjects:
        tmp_path.joinpath(name).write_bytes(b"source companion subject")
        tmp_path.joinpath(name + ".sigstore.json").write_text(
            bundle_payload,
            encoding="utf-8",
        )

    release_metadata.verify_release_payload_contract(
        tmp_path,
        version="5.0.0",
        payload_kind="source-companion",
        signature_mode="required",
    )

    (tmp_path / "build.log").write_text("must not be published", encoding="utf-8")
    (tmp_path / "build.log.sigstore.json").write_text(
        bundle_payload,
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match=r"unexpected=.*build\.log"):
        release_metadata.verify_release_payload_contract(
            tmp_path,
            version="5.0.0",
            payload_kind="source-companion",
            signature_mode="required",
        )


def test_generate_checksums_requires_artifacts(tmp_path):
    with pytest.raises(RuntimeError, match="no release artifacts"):
        release_metadata.generate_checksums(tmp_path, tmp_path / "SHA256SUMS")


def test_verify_checksums_rejects_tampering_and_unlisted_artifacts(tmp_path):
    artifact = tmp_path / "package.whl"
    artifact.write_bytes(b"original")
    checksums = tmp_path / "SHA256SUMS"
    release_metadata.generate_checksums(tmp_path, checksums)
    release_metadata.verify_checksums(tmp_path, checksums)

    artifact.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        release_metadata.verify_checksums(tmp_path, checksums)

    artifact.write_bytes(b"original")
    (tmp_path / "unlisted.tar.gz").write_bytes(b"unexpected")
    with pytest.raises(RuntimeError, match="inventory mismatch"):
        release_metadata.verify_checksums(tmp_path, checksums)


def test_checksum_inventory_rejects_unsafe_or_duplicate_names(tmp_path):
    checksum = "0" * 64
    checksums = tmp_path / "SHA256SUMS"
    checksums.write_text(
        f"{checksum} *artifact.whl\n{checksum} *artifact.whl\n",
        encoding="ascii",
    )

    with pytest.raises(RuntimeError, match="duplicate"):
        release_metadata.read_checksum_entries(checksums)

    checksums.write_text(f"{checksum} *../escape.whl\n", encoding="ascii")
    with pytest.raises(RuntimeError, match="unsafe"):
        release_metadata.read_checksum_entries(checksums)


def test_generate_sbom_contains_only_dependency_closure(tmp_path, monkeypatch):
    distributions = {
        "dupeguru-neo": FakeDistribution("dupeguru-neo", "5.0.0", ["dependency>=1"]),
        "dependency": FakeDistribution("dependency", "1.2.3"),
        "build-tool": FakeDistribution("build-tool", "9.0"),
    }
    monkeypatch.setattr(release_metadata, "_distribution_index", lambda: distributions)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")

    output = tmp_path / "sbom.cdx.json"
    release_metadata.generate_sbom("dupeguru-neo", output)
    document = json.loads(output.read_text(encoding="utf-8"))

    assert document["bomFormat"] == "CycloneDX"
    assert document["specVersion"] == "1.6"
    assert document["metadata"]["timestamp"] == "1970-01-01T00:00:00Z"
    assert {item["name"] for item in document["components"]} == {
        "dupeguru-neo",
        "dependency",
    }


def test_generate_sbom_inventories_release_artifacts(tmp_path, monkeypatch):
    distributions = {
        "dupeguru-neo": FakeDistribution("dupeguru-neo", "5.0.0"),
    }
    monkeypatch.setattr(release_metadata, "_distribution_index", lambda: distributions)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    artifact = tmp_path / "dupeguru-neo-5.0.0-linux-x86_64-unsigned-portable.tar.gz"
    artifact.write_bytes(b"portable")
    output = tmp_path / "sbom.cdx.json"

    release_metadata.generate_sbom(
        "dupeguru-neo",
        output,
        artifact_directory=tmp_path,
    )

    document = json.loads(output.read_text(encoding="utf-8"))
    artifact_component = next(item for item in document["components"] if item["name"] == artifact.name)
    assert artifact_component["type"] == "file"
    assert artifact_component["hashes"] == [
        {
            "alg": "SHA-256",
            "content": hashlib.sha256(b"portable").hexdigest(),
        }
    ]


def _write_dependency_snapshot(directory, target, components, edges):
    payload = {
        "schema": "dupeguru.release-dependency-snapshot",
        "version": 1,
        "target": target,
        "root": "dupeguru-neo",
        "components": [
            {
                "name": name,
                "display_name": display_name,
                "version": version,
                "purl": f"pkg:pypi/{name}@{version}",
            }
            for name, display_name, version in components
        ],
        "dependencies": [
            {
                "ref": name,
                "depends_on": sorted(dependencies),
            }
            for name, dependencies in sorted(edges.items())
        ],
    }
    path = directory / f"dependency-snapshot-{target}.json"
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def test_aggregate_sbom_unions_exact_runtime_closures_from_every_target(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    root = ("dupeguru-neo", "dupeguru-neo", "5.0.0")
    dependency = ("dependency", "dependency", "1.2.3")
    pywin32 = ("pywin32", "pywin32", "312")
    for target in ("linux-x86_64", "macos-arm64"):
        _write_dependency_snapshot(
            snapshots,
            target,
            [root, dependency],
            {
                "dupeguru-neo": {"dependency"},
                "dependency": set(),
            },
        )
    _write_dependency_snapshot(
        snapshots,
        "windows-x86_64",
        [root, dependency, pywin32],
        {
            "dupeguru-neo": {"dependency", "pywin32"},
            "dependency": set(),
            "pywin32": set(),
        },
    )
    lock = tmp_path / "requirements-release.txt"
    lock.write_text(
        'dependency==1.2.3\npywin32==312; sys_platform == "win32"\n',
        encoding="utf-8",
        newline="\n",
    )

    output = tmp_path / "aggregate.cdx.json"
    release_metadata.generate_sbom(
        "dupeguru-neo",
        output,
        lock_path=lock,
        dependency_snapshots_directory=snapshots,
    )
    document = json.loads(output.read_text(encoding="utf-8"))

    components = {item["name"]: item for item in document["components"]}
    assert set(components) == {"dependency", "dupeguru-neo", "pywin32"}
    assert components["pywin32"]["properties"] == [
        {
            "name": "dupeguru:runtime-targets",
            "value": "windows-x86_64",
        }
    ]
    root_ref = components["dupeguru-neo"]["bom-ref"]
    dependency_by_ref = {item["ref"]: item["dependsOn"] for item in document["dependencies"]}
    assert set(dependency_by_ref[root_ref]) == {
        components["dependency"]["bom-ref"],
        components["pywin32"]["bom-ref"],
    }
    properties = {item["name"]: item["value"] for item in document["metadata"]["properties"]}
    assert properties["dupeguru:sbom:inventory-scope"] == (
        "union of installed runtime dependency closures captured on every " "release target, plus release payload files"
    )
    assert properties["dupeguru:sbom:runtime-targets"] == ("linux-x86_64,macos-arm64,windows-x86_64")
    for target in ("linux-x86_64", "macos-arm64", "windows-x86_64"):
        assert (
            properties[f"dupeguru:sbom:dependency-snapshot:{target}:sha256"]
            == hashlib.sha256(snapshots.joinpath(f"dependency-snapshot-{target}.json").read_bytes()).hexdigest()
        )

    lock.write_text(
        'dependency==9.9.9\npywin32==312; sys_platform == "win32"\n',
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(RuntimeError, match="version does not match"):
        release_metadata.generate_sbom(
            "dupeguru-neo",
            tmp_path / "mismatched.cdx.json",
            lock_path=lock,
            dependency_snapshots_directory=snapshots,
        )


def test_dependency_snapshot_rejects_a_target_label_that_does_not_match_runner(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(release_metadata, "_runtime_target", lambda: "linux-x86_64")
    with pytest.raises(RuntimeError, match="does not match the runner"):
        release_metadata.generate_dependency_snapshot(
            "dupeguru-neo",
            "windows-x86_64",
            tmp_path / "snapshot.json",
        )


def test_dependency_snapshot_records_the_installed_runtime_closure(tmp_path, monkeypatch):
    distributions = {
        "dupeguru-neo": FakeDistribution("dupeguru-neo", "5.0.0", ["dependency>=1"]),
        "dependency": FakeDistribution("dependency", "1.2.3"),
    }
    monkeypatch.setattr(release_metadata, "_distribution_index", lambda: distributions)
    monkeypatch.setattr(release_metadata, "_runtime_target", lambda: "linux-x86_64")
    output = tmp_path / "dependency-snapshot-linux-x86_64.json"

    release_metadata.generate_dependency_snapshot(
        "dupeguru-neo",
        "linux-x86_64",
        output,
    )

    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["target"] == "linux-x86_64"
    assert {item["name"] for item in document["components"]} == {
        "dependency",
        "dupeguru-neo",
    }
    dependencies = {item["ref"]: item["depends_on"] for item in document["dependencies"]}
    assert dependencies == {
        "dependency": [],
        "dupeguru-neo": ["dependency"],
    }


def test_dependency_snapshot_runtime_target_normalizes_release_runner_names(monkeypatch):
    monkeypatch.setattr(release_metadata.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(release_metadata.platform, "machine", lambda: "arm64")
    assert release_metadata._runtime_target() == "macos-arm64"

    monkeypatch.setattr(release_metadata.platform, "system", lambda: "Windows")
    monkeypatch.setattr(release_metadata.platform, "machine", lambda: "AMD64")
    assert release_metadata._runtime_target() == "windows-x86_64"


def test_sbom_requires_source_date_epoch(tmp_path, monkeypatch):
    distributions = {
        "dupeguru-neo": FakeDistribution("dupeguru-neo", "5.0.0"),
    }
    monkeypatch.setattr(release_metadata, "_distribution_index", lambda: distributions)
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    with pytest.raises(RuntimeError, match="SOURCE_DATE_EPOCH"):
        release_metadata.generate_sbom("dupeguru-neo", tmp_path / "sbom.json")


def test_build_manifest_uses_release_identity_and_reproducible_timestamp(tmp_path, monkeypatch):
    artifact = tmp_path / "dupeguru_neo-5.0.0.tar.gz"
    artifact.write_bytes(b"source")
    output = tmp_path / "BUILD-METADATA.json"
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1")

    release_metadata.generate_build_manifest(
        tmp_path,
        output,
        repository="AiWithYou/dupeguru_neo",
        commit="a" * 40,
        ref="refs/tags/v5.0.0",
        version="5.0.0",
    )

    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["source_date_epoch"] == 1
    assert manifest["timestamp"] == "1970-01-01T00:00:01Z"
    assert manifest["commit"] == "a" * 40
    assert manifest["artifacts"] == [
        {
            "name": artifact.name,
            "sha256": hashlib.sha256(b"source").hexdigest(),
            "size": 6,
        }
    ]


def test_sbom_and_manifest_record_exact_dependency_lock(tmp_path, monkeypatch):
    distributions = {
        "dupeguru-neo": FakeDistribution("dupeguru-neo", "5.0.0"),
    }
    monkeypatch.setattr(release_metadata, "_distribution_index", lambda: distributions)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1")
    lock = tmp_path / "requirements-release.txt"
    lock.write_text("Pillow==12.3.0\n", encoding="utf-8", newline="\n")
    expected_digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    artifacts = tmp_path / "dist"
    artifacts.mkdir()
    (artifacts / "dupeguru_neo-5.0.0.tar.gz").write_bytes(b"source")

    sbom_path = artifacts / "dupeguru-neo-5.0.0.cdx.json"
    release_metadata.generate_sbom(
        "dupeguru-neo",
        sbom_path,
        artifact_directory=artifacts,
        lock_path=lock,
    )
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    assert {item["name"]: item["value"] for item in sbom["metadata"]["properties"]} == {
        "dupeguru:dependency-lock:path": "requirements-release.txt",
        "dupeguru:dependency-lock:sha256": expected_digest,
    }

    manifest_path = artifacts / "BUILD-METADATA.json"
    release_metadata.generate_build_manifest(
        artifacts,
        manifest_path,
        repository="AiWithYou/dupeguru_neo",
        commit="a" * 40,
        ref="refs/tags/v5.0.0",
        version="5.0.0",
        lock_path=lock,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["dependency_lock"] == {
        "path": "requirements-release.txt",
        "sha256": expected_digest,
    }


@pytest.mark.parametrize(
    ("tag", "version", "stable"),
    [
        ("v5.0.0", "5.0.0", True),
        ("v5.0.0rc1", "5.0.0rc1", False),
        ("v5.0.0.dev1", "5.0.0.dev1", False),
    ],
)
def test_release_tag_classification(tag, version, stable):
    assert release_metadata.validate_release_tag(tag, version) is stable


def test_release_tag_must_exactly_match_package_version():
    with pytest.raises(RuntimeError, match="does not match"):
        release_metadata.validate_release_tag("v5.0.1", "5.0.0")
    with pytest.raises(RuntimeError, match="local"):
        release_metadata.validate_release_tag("v5.0.0+local", "5.0.0+local")


def test_verify_signature_bundles_requires_well_formed_bundle_for_every_artifact(tmp_path):
    artifact = tmp_path / "package.whl"
    artifact.touch()
    bundle = tmp_path / "package.whl.sigstore.json"
    bundle.write_text(
        json.dumps({"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"}),
        encoding="utf-8",
    )

    release_metadata.verify_signature_bundles(tmp_path)

    bundle.unlink()
    with pytest.raises(RuntimeError, match="bijection mismatch"):
        release_metadata.verify_signature_bundles(tmp_path)


def test_verify_sigstore_bundles_checks_every_artifact_identity(tmp_path, monkeypatch):
    artifact = tmp_path / "package.whl"
    artifact.touch()
    bundle = tmp_path / "package.whl.sigstore.json"
    bundle.write_text(
        json.dumps({"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"}),
        encoding="utf-8",
    )
    commands = []

    def record(command, check):
        commands.append(command)

    monkeypatch.setattr(release_metadata.subprocess, "run", record)
    release_metadata.verify_sigstore_bundles(
        tmp_path,
        certificate_identity="https://github.com/AiWithYou/dupeguru_neo/workflow@refs/tags/v5.0.0",
        certificate_oidc_issuer="https://token.actions.githubusercontent.com",
    )

    assert len(commands) == 1
    assert commands[0][-1] == artifact
    assert "--bundle" in commands[0]
    assert str(bundle) in [str(item) for item in commands[0]]


def test_gate_writes_safe_single_line_github_values(tmp_path, monkeypatch):
    output = tmp_path / "github-output"
    environment = tmp_path / "github-env"
    monkeypatch.setattr(release_metadata, "git_release_context", lambda tag, commit: 123)

    exit_code = release_metadata.main(
        [
            "gate",
            "--tag",
            "v5.0.0",
            "--version",
            "5.0.0",
            "--commit",
            "a" * 40,
            "--github-output",
            str(output),
            "--github-env",
            str(environment),
        ]
    )

    assert exit_code == 0
    assert output.read_text(encoding="utf-8").splitlines() == [
        "stable=true",
        "version=5.0.0",
        "source_date_epoch=123",
    ]
    assert environment.read_text(encoding="utf-8") == "SOURCE_DATE_EPOCH=123\n"


def test_git_release_context_requires_origin_master_ancestry(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "release-test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Release Test"],
        cwd=repository,
        check=True,
    )
    tracked = repository / "tracked.txt"
    tracked.write_text("tagged\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    environment = os.environ.copy()
    environment["GIT_AUTHOR_DATE"] = "2000-01-01T00:00:00Z"
    environment["GIT_COMMITTER_DATE"] = "2000-01-01T00:00:00Z"
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "tagged"],
        cwd=repository,
        env=environment,
        check=True,
    )
    tagged_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "tag", "v5.0.0", tagged_commit], cwd=repository, check=True)
    tracked.write_text("master descendant\n", encoding="utf-8")
    subprocess.run(["git", "commit", "--quiet", "-am", "descendant"], cwd=repository, check=True)
    master_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/master", master_commit],
        cwd=repository,
        check=True,
    )
    monkeypatch.chdir(repository)

    assert release_metadata.git_release_context("v5.0.0", tagged_commit) == 946684800

    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    unrelated_commit = subprocess.run(
        ["git", "commit-tree", tree],
        cwd=repository,
        env=environment,
        input="unrelated\n",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/master", unrelated_commit],
        cwd=repository,
        check=True,
    )
    with pytest.raises(RuntimeError, match="not reachable from origin/master"):
        release_metadata.git_release_context("v5.0.0", tagged_commit)


def test_artifact_symlink_is_rejected(tmp_path):
    target = tmp_path / "target.whl"
    target.touch()
    link = tmp_path / "linked.whl"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises(RuntimeError, match="symlink"):
        release_metadata.generate_checksums(tmp_path, tmp_path / "SHA256SUMS")


def test_artifact_payload_must_be_flat(tmp_path):
    (tmp_path / "nested").mkdir()

    with pytest.raises(RuntimeError, match="flat files"):
        release_metadata.generate_checksums(tmp_path, tmp_path / "SHA256SUMS")


def test_corresponding_source_is_deterministic_and_matches_tagged_git_tree(
    tmp_path,
    monkeypatch,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    tracked_files = {
        ".github/workflows/release.yml": "name: release\n",
        "LICENSE": "GPLv3\n",
        "MANIFEST.in": "include LICENSE\n",
        "README.md": "dupeGuru Neo\n",
        "THIRD_PARTY_NOTICES.md": "BSD notices\n",
        "build.py": "print('build')\n",
        "core/__init__.py": "__version__ = '5.0.0'\n",
        "docs/PORTABLE-NOTICE.txt": "unsigned portable\n",
        "docs/SOURCE-COMPANION.md": "source companion\n",
        "hscommon/LICENSE": "BSD-3-Clause\n",
        "images/logo.png": "image\n",
        "package.py": "print('package')\n",
        "pkg/example.txt": "platform package\n",
        "pyproject.toml": "[build-system]\n",
        "qt/__init__.py": "",
        "release-sources.json": "{}\n",
        "requirements-release.txt": "Pillow==12.3.0\n",
        "run.py": "print('run')\n",
        "scripts/ci_artifact_smoke.py": "print('smoke')\n",
        "scripts/dependency_license_inventory.py": "print('licenses')\n",
        "scripts/frozen_runtime_license_inventory.py": "print('frozen licenses')\n",
        "scripts/portable_bundle.py": "print('portable')\n",
        "scripts/release_metadata.py": "print('metadata')\n",
        "scripts/source_companion.py": "print('source companion')\n",
        "setup.cfg": "[metadata]\nname = dupeguru-neo\n",
        "setup.py": "from setuptools import setup\nsetup()\n",
    }
    for name, content in tracked_files.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "release-test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Release Test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    git_environment = os.environ.copy()
    git_environment["GIT_AUTHOR_DATE"] = "2000-01-01T00:00:00Z"
    git_environment["GIT_COMMITTER_DATE"] = "2000-01-01T00:00:00Z"
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "release source"],
        cwd=repository,
        env=git_environment,
        check=True,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    epoch = subprocess.run(
        ["git", "show", "-s", "--format=%ct", commit],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "untracked-secret.txt").write_text("not releasable", encoding="utf-8")
    monkeypatch.chdir(repository)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", epoch)
    archive_name = "dupeguru-neo-5.0.0-source.tar.gz"
    first = release_metadata.generate_corresponding_source(
        tmp_path / "first" / archive_name,
        commit=commit,
        version="5.0.0",
    )
    second = release_metadata.generate_corresponding_source(
        tmp_path / "second" / archive_name,
        commit=commit,
        version="5.0.0",
    )

    assert first.read_bytes() == second.read_bytes()
    release_metadata.verify_corresponding_source(
        first,
        commit=commit,
        version="5.0.0",
    )
    with tarfile.open(first, mode="r:gz") as archive:
        names = {member.name for member in archive}
    root = "dupeguru-neo-5.0.0-source"
    assert f"{root}/.github/workflows/release.yml" in names
    assert f"{root}/THIRD_PARTY_NOTICES.md" in names
    assert f"{root}/hscommon/LICENSE" in names
    assert f"{root}/requirements-release.txt" in names
    assert f"{root}/scripts/portable_bundle.py" in names
    assert all("untracked-secret.txt" not in name for name in names)
