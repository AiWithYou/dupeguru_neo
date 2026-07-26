import hashlib
import io
import json
from pathlib import Path
import platform
import tarfile
import zipfile

import pytest

from scripts import source_companion

ROOT = Path(__file__).parents[2]


def _source_entry(
    *,
    filename,
    content,
    name,
    provides,
    version,
    kind="pypi-sdist",
    url=None,
):
    return {
        "filename": filename,
        "kind": kind,
        "name": name,
        "provides": provides,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "url": url or f"https://files.pythonhosted.org/source/{filename}",
        "version": version,
    }


def _write_source_inputs(parent):
    dependency_lock = parent / "requirements-release.txt"
    dependency_lock.write_text(
        "Example==1.0\n",
        encoding="utf-8",
        newline="\n",
    )
    source_contents = {
        "example-1.0.tar.gz": b"example source\n",
        "pyinstaller-9.9.tar.gz": b"pyinstaller source\n",
        "Python-test.tar.xz": b"cpython source\n",
    }
    source_document = {
        "portable_builder": {
            "name": "PyInstaller",
            "version": "9.9",
        },
        "portable_python_version": platform.python_version(),
        "schema": "dupeguru.release-source-lock",
        "schema_version": 1,
        "sources": [
            _source_entry(
                filename="example-1.0.tar.gz",
                content=source_contents["example-1.0.tar.gz"],
                name="Example",
                provides=["example"],
                version="1.0",
            ),
            _source_entry(
                filename="pyinstaller-9.9.tar.gz",
                content=source_contents["pyinstaller-9.9.tar.gz"],
                name="PyInstaller",
                provides=["pyinstaller-bootloader"],
                version="9.9",
            ),
            _source_entry(
                filename="Python-test.tar.xz",
                content=source_contents["Python-test.tar.xz"],
                kind="python-official-source",
                name="CPython",
                provides=["cpython-runtime"],
                version=platform.python_version(),
                url="https://www.python.org/ftp/python/test/Python-test.tar.xz",
            ),
        ],
    }
    source_lock = parent / "release-sources.json"
    source_lock.write_text(
        json.dumps(source_document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    source_directory = parent / "upstream"
    source_directory.mkdir()
    for filename, content in source_contents.items():
        source_directory.joinpath(filename).write_bytes(content)
    return dependency_lock, source_lock, source_directory, source_document


def _portable_member_root(platform_name):
    if platform_name == "macos":
        return "dupeguru-neo.app/Contents/Resources"
    return "dupeguru-neo/_internal"


def _write_portable_archive(parent, platform_name, architecture):
    extension = ".zip" if platform_name == "windows" else ".tar.gz"
    name = f"dupeguru-neo-5.0.0-{platform_name}-{architecture}" f"-unsigned-portable{extension}"
    path = parent / name
    data_root = _portable_member_root(platform_name)
    prefix = f"{data_root}/THIRD-PARTY-LICENSES"
    frozen_prefix = f"{data_root}/FROZEN-RUNTIME-LICENSES"
    members = {
        f"{prefix}/index.json": b'{"platform": "test"}\n',
        f"{prefix}/index.txt": b"license inventory\n",
        f"{prefix}/packages/example/LICENSE": b"Example license\n",
        f"{frozen_prefix}/index.json": b'{"components": []}\n',
        f"{frozen_prefix}/index.txt": b"frozen runtime inventory\n",
        f"{frozen_prefix}/components/cpython/LICENSE.txt": b"PSF license\n",
    }
    if platform_name == "windows":
        with zipfile.ZipFile(path, mode="w") as archive:
            for member_name, content in members.items():
                archive.writestr(member_name, content)
    else:
        with tarfile.open(path, mode="w:gz") as archive:
            for member_name, content in members.items():
                member = tarfile.TarInfo(member_name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    return path


def _write_portables(parent):
    parent.mkdir()
    return [
        _write_portable_archive(parent, "linux", "x86_64"),
        _write_portable_archive(parent, "macos", "arm64"),
        _write_portable_archive(parent, "windows", "x86_64"),
    ]


def _write_static_inputs(parent):
    paths = {}
    for name, content in {
        "recipe": b"# Build recipe\n",
        "notice": b"# Third-party notices\n",
        "license": b"GPLv3\n",
        "hscommon_license": b"BSD-3-Clause\n",
    }.items():
        path = parent / f"{name}.txt"
        path.write_bytes(content)
        paths[name] = path
    return paths


def test_repository_source_lock_covers_every_release_runtime_and_frozen_component():
    document, records = source_companion.validate_source_lock(
        ROOT / "release-sources.json",
        ROOT / "requirements-release.txt",
    )

    assert document["portable_python_version"] == "3.13.14"
    assert document["portable_builder"] == {
        "name": "PyInstaller",
        "version": "6.21.0",
    }
    providers = {provider for record in records for provider in record.provides}
    assert providers == {
        "cpython-runtime",
        "distro",
        "mutagen",
        "pillow",
        "pyinstaller-bootloader",
        "pyqt6",
        "pyqt6-qt6",
        "pyqt6-sip",
        "pywin32",
        "semantic-version",
        "xxhash",
    }
    assert next(record for record in records if record.name == "Qt").size == 1017723080


def test_source_lock_rejects_missing_runtime_source(tmp_path):
    dependency_lock, source_lock, _, document = _write_source_inputs(tmp_path)
    document["sources"] = [entry for entry in document["sources"] if "example" not in entry["provides"]]
    source_lock.write_text(
        json.dumps(document),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(RuntimeError, match="coverage mismatch"):
        source_companion.validate_source_lock(source_lock, dependency_lock)


def test_fetch_reuses_only_complete_digest_verified_sources(tmp_path, monkeypatch):
    dependency_lock, source_lock, source_directory, _ = _write_source_inputs(tmp_path)

    def unexpected_download(url, output):
        raise AssertionError(f"unexpected download: {url} -> {output}")

    monkeypatch.setattr(source_companion, "_run_curl", unexpected_download)

    fetched = source_companion.fetch_sources(
        source_lock,
        dependency_lock,
        source_directory,
    )

    assert {path.name for path in fetched} == {
        "Python-test.tar.xz",
        "example-1.0.tar.gz",
        "pyinstaller-9.9.tar.gz",
    }

    source_directory.joinpath("example-1.0.tar.gz").write_bytes(b"tampered data!\n")
    with pytest.raises(RuntimeError, match="digest mismatch"):
        source_companion.fetch_sources(
            source_lock,
            dependency_lock,
            source_directory,
        )


def test_portable_archives_verify_each_platform_before_local_companion_build(
    tmp_path,
    monkeypatch,
):
    portable_directory = tmp_path / "portable"
    portable_archives = _write_portables(portable_directory)
    dependency_lock = tmp_path / "requirements-release.txt"
    dependency_lock.write_text("Example==1.0\n", encoding="utf-8")
    verified = []

    def record_verification(archive, lock):
        verified.append((archive, lock))

    monkeypatch.setattr(
        source_companion,
        "verify_portable_archive",
        record_verification,
    )

    result = source_companion._portable_archives(
        portable_directory,
        version="5.0.0",
        dependency_lock_path=dependency_lock,
    )

    assert [platform_name for _, platform_name, _ in result] == [
        "linux",
        "macos",
        "windows",
    ]
    assert {archive for archive, _ in verified} == set(portable_archives)
    assert {lock for _, lock in verified} == {dependency_lock}


def test_portable_archives_require_exactly_one_archive_per_platform(
    tmp_path,
    monkeypatch,
):
    portable_directory = tmp_path / "portable"
    portable_archives = _write_portables(portable_directory)
    portable_archives[-1].unlink()
    dependency_lock = tmp_path / "requirements-release.txt"
    dependency_lock.write_text("Example==1.0\n", encoding="utf-8")
    monkeypatch.setattr(
        source_companion,
        "verify_portable_archive",
        lambda archive, lock: None,
    )

    with pytest.raises(RuntimeError, match="exactly one archive for every release target"):
        source_companion._portable_archives(
            portable_directory,
            version="5.0.0",
            dependency_lock_path=dependency_lock,
        )


def test_portable_archives_reject_a_different_release_version(
    tmp_path,
    monkeypatch,
):
    portable_directory = tmp_path / "portable"
    _write_portables(portable_directory)
    dependency_lock = tmp_path / "requirements-release.txt"
    dependency_lock.write_text("Example==1.0\n", encoding="utf-8")
    monkeypatch.setattr(
        source_companion,
        "verify_portable_archive",
        lambda archive, lock: None,
    )

    with pytest.raises(RuntimeError, match="release version mismatch"):
        source_companion._portable_archives(
            portable_directory,
            version="5.0.1",
            dependency_lock_path=dependency_lock,
        )


def test_local_companion_is_deterministic_and_proof_bound(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    dependency_lock, source_lock, source_directory, _ = _write_source_inputs(tmp_path)
    portable_directory = tmp_path / "portable"
    portable_archives = _write_portables(portable_directory)
    application_source = tmp_path / "dupeguru-neo-5.0.0-source.tar.gz"
    application_source.write_bytes(b"tagged application source\n")
    static = _write_static_inputs(tmp_path)
    monkeypatch.setattr(
        source_companion,
        "verify_portable_archive",
        lambda archive, lock: None,
    )
    monkeypatch.setattr(
        source_companion,
        "verify_corresponding_source",
        lambda archive, commit, version: None,
    )
    commit = "a" * 40

    first, first_proof = source_companion.build_source_companion(
        version="5.0.0",
        commit=commit,
        source_lock_path=source_lock,
        dependency_lock_path=dependency_lock,
        source_directory=source_directory,
        application_source_path=application_source,
        portable_directory=portable_directory,
        output_path=tmp_path / "first" / "dupeguru-neo-5.0.0-source-companion.tar",
        proof_path=tmp_path / "first-proof.json",
        recipe_path=static["recipe"],
        notice_path=static["notice"],
        license_path=static["license"],
        hscommon_license_path=static["hscommon_license"],
    )
    second, second_proof = source_companion.build_source_companion(
        version="5.0.0",
        commit=commit,
        source_lock_path=source_lock,
        dependency_lock_path=dependency_lock,
        source_directory=source_directory,
        application_source_path=application_source,
        portable_directory=portable_directory,
        output_path=tmp_path / "second" / "dupeguru-neo-5.0.0-source-companion.tar",
        proof_path=tmp_path / "second-proof.json",
        recipe_path=static["recipe"],
        notice_path=static["notice"],
        license_path=static["license"],
        hscommon_license_path=static["hscommon_license"],
    )

    assert first.read_bytes() == second.read_bytes()
    assert first_proof.read_bytes() == second_proof.read_bytes()
    proof = json.loads(first_proof.read_text(encoding="utf-8"))
    assert proof["archive"] == {
        "name": first.name,
        "sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
        "size": first.stat().st_size,
    }
    assert {entry["name"]: entry["sha256"] for entry in proof["portable_archives"]} == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in portable_archives
    }
    with tarfile.open(first, mode="r:") as archive:
        names = {member.name for member in archive}
        manifest_member = archive.getmember("dupeguru-neo-5.0.0-source-companion/SOURCE-MANIFEST.json")
        manifest = json.load(archive.extractfile(manifest_member))
    root = "dupeguru-neo-5.0.0-source-companion"
    assert f"{root}/application/{application_source.name}" in names
    assert f"{root}/upstream/Python-test.tar.xz" in names
    for platform_name, architecture in (
        ("linux", "x86_64"),
        ("macos", "arm64"),
        ("windows", "x86_64"),
    ):
        for category in ("frozen-runtime", "third-party"):
            assert f"{root}/license-inventories/" f"{platform_name}-{architecture}/{category}/index.json" in names
    assert manifest["upstream_sources"][0]["url"].startswith("https://")
    assert {provider for entry in manifest["frozen_runtime_sources"] for provider in entry["provides"]} == {
        "cpython-runtime",
        "pyinstaller-bootloader",
    }

    source_companion.verify_source_companion(
        archive_path=first,
        proof_path=first_proof,
        source_lock_path=source_lock,
        dependency_lock_path=dependency_lock,
        version="5.0.0",
        commit=commit,
    )

    mismatched_proof = json.loads(first_proof.read_text(encoding="utf-8"))
    mismatched_proof["archive"]["sha256"] = "0" * 64
    mismatched_proof_path = tmp_path / "mismatched-proof.json"
    mismatched_proof_path.write_text(
        json.dumps(mismatched_proof, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(RuntimeError, match="archive digest differs from proof"):
        source_companion.verify_source_companion(
            archive_path=first,
            proof_path=mismatched_proof_path,
            source_lock_path=source_lock,
            dependency_lock_path=dependency_lock,
            version="5.0.0",
            commit=commit,
        )

    with first.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(RuntimeError, match="(?:size|digest) differs from proof"):
        source_companion.verify_source_companion(
            archive_path=first,
            proof_path=first_proof,
            source_lock_path=source_lock,
            dependency_lock_path=dependency_lock,
            version="5.0.0",
            commit=commit,
        )
