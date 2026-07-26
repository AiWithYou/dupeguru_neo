import hashlib
import io
import json
from email.parser import Parser
from pathlib import Path
from pathlib import PurePosixPath
import subprocess
import tarfile
import zipfile

import pytest

from scripts import (
    dependency_license_inventory,
    frozen_runtime_license_inventory,
    portable_bundle,
)


class FakeDistribution:
    def __init__(self, root):
        self.root = root
        self.version = "1.0"
        self.metadata = Parser().parsestr(
            "Metadata-Version: 2.4\n"
            "Name: Example\n"
            "Version: 1.0\n"
            "License: BSD-2-Clause\n"
            "License-File: LICENSE\n"
        )
        self.files = [
            PurePosixPath("example-1.0.dist-info/licenses/LICENSE"),
            PurePosixPath("example-1.0.dist-info/RECORD"),
            PurePosixPath("example/__init__.py"),
        ]

    def locate_file(self, path):
        return self.root.joinpath(str(path))


def _fake_distribution(parent):
    installation_root = parent / "installed-python"
    site_packages = installation_root / "Lib" / "site-packages"
    distribution = FakeDistribution(site_packages)
    contents = {
        "example-1.0.dist-info/licenses/LICENSE": b"Example license\n",
        "example-1.0.dist-info/RECORD": (
            b"example/__init__.py,,\n" b"example-1.0.dist-info/licenses/LICENSE,,\n" b"example-1.0.dist-info/RECORD,,\n"
        ),
        "example/__init__.py": b'__version__ = "1.0"\n',
    }
    for relative_path, content in contents.items():
        _write_file(site_packages / relative_path, content)
    return distribution, installation_root


def _write_file(path, content=b"payload", executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if executable:
        path.chmod(0o755)


def _zip_bytes(entries, *, compression=zipfile.ZIP_STORED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return stream.getvalue()


def _tar_bytes(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return stream.getvalue()


def _windows_tree(parent):
    root = parent / "dupeguru-neo"
    _write_file(root / "dupeguru-neo.exe", executable=True)
    _write_file(root / "dupeguru.exe", executable=True)
    _write_file(root / "_internal" / "PORTABLE-NOTICE.txt")
    _write_file(root / "_internal" / "LICENSE")
    _write_file(root / "_internal" / "THIRD_PARTY_NOTICES.md")
    _write_file(root / "_internal" / "hscommon" / "LICENSE")
    _write_file(root / "_internal" / "requirements-release.txt")
    _write_file(root / "_internal" / "release-sources.json")
    _write_file(root / "_internal" / "THIRD-PARTY-LICENSES" / "index.json")
    _write_file(root / "_internal" / "THIRD-PARTY-LICENSES" / "index.txt")
    _write_file(root / "_internal" / "FROZEN-RUNTIME-LICENSES" / "index.json")
    _write_file(root / "_internal" / "FROZEN-RUNTIME-LICENSES" / "index.txt")
    return root


def _linux_tree(parent):
    root = parent / "dupeguru-neo"
    _write_file(root / "dupeguru-neo", executable=True)
    _write_file(root / "_internal" / "PORTABLE-NOTICE.txt")
    _write_file(root / "_internal" / "LICENSE")
    _write_file(root / "_internal" / "THIRD_PARTY_NOTICES.md")
    _write_file(root / "_internal" / "hscommon" / "LICENSE")
    _write_file(root / "_internal" / "requirements-release.txt")
    _write_file(root / "_internal" / "release-sources.json")
    _write_file(root / "_internal" / "THIRD-PARTY-LICENSES" / "index.json")
    _write_file(root / "_internal" / "THIRD-PARTY-LICENSES" / "index.txt")
    _write_file(root / "_internal" / "FROZEN-RUNTIME-LICENSES" / "index.json")
    _write_file(root / "_internal" / "FROZEN-RUNTIME-LICENSES" / "index.txt")
    return root


def _macos_tree(parent):
    root = parent / "dupeguru-neo.app"
    _write_file(root / "Contents" / "MacOS" / "dupeguru-neo", executable=True)
    _write_file(root / "Contents" / "Resources" / "PORTABLE-NOTICE.txt")
    _write_file(root / "Contents" / "Resources" / "LICENSE")
    _write_file(root / "Contents" / "Resources" / "THIRD_PARTY_NOTICES.md")
    _write_file(root / "Contents" / "Resources" / "hscommon" / "LICENSE")
    _write_file(root / "Contents" / "Resources" / "requirements-release.txt")
    _write_file(root / "Contents" / "Resources" / "release-sources.json")
    _write_file(root / "Contents" / "Resources" / "THIRD-PARTY-LICENSES" / "index.json")
    _write_file(root / "Contents" / "Resources" / "THIRD-PARTY-LICENSES" / "index.txt")
    _write_file(root / "Contents" / "Resources" / "FROZEN-RUNTIME-LICENSES" / "index.json")
    _write_file(root / "Contents" / "Resources" / "FROZEN-RUNTIME-LICENSES" / "index.txt")
    return root


def _write_fake_license_inventory(
    data_root,
    lock,
    system,
    distribution,
    installation_root,
):
    license_content = b"Example license\n"
    copied_path = "packages/example-1.0/01-LICENSE"
    _write_file(
        data_root / "THIRD-PARTY-LICENSES" / copied_path,
        license_content,
    )
    _write_fake_frozen_runtime_inventory(data_root, lock, system)
    source_lock = lock.with_name("release-sources.json")
    source = dependency_license_inventory._source_providers(source_lock)["example"]
    provenance = dependency_license_inventory._installed_distribution_provenance(
        distribution,
        dependency_license_inventory._installation_root(installation_root),
    )
    document = {
        "schema": "dupeguru.third-party-license-inventory",
        "schema_version": 2,
        "generated_at": "2023-11-14T22:13:20Z",
        "lock": {
            "path": "requirements-release.txt",
            "sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        },
        "source_lock": {
            "path": "release-sources.json",
            "sha256": hashlib.sha256(source_lock.read_bytes()).hexdigest(),
        },
        "platform": {"machine": "x86_64", "system": system},
        "packages": [
            {
                "canonical_name": "example",
                "files": [
                    {
                        "copied_path": copied_path,
                        "declared_by_metadata": True,
                        "sha256": hashlib.sha256(license_content).hexdigest(),
                        "size": len(license_content),
                        "source_path": "example-1.0.dist-info/licenses/LICENSE",
                    }
                ],
                "installed_provenance": provenance,
                "license": "BSD-2-Clause",
                "license_classifiers": [],
                "license_expression": None,
                "license_files_declared": ["LICENSE"],
                "metadata_warnings": ["License-Expression metadata is absent"],
                "name": "Example",
                "source_archive": source,
                "source_provider": "example",
                "version": "1.0",
            }
        ],
        "inactive_constraints": [],
    }
    data_root.joinpath("requirements-release.txt").write_bytes(lock.read_bytes())
    data_root.joinpath("THIRD-PARTY-LICENSES", "index.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    data_root.joinpath("THIRD-PARTY-LICENSES", "index.txt").write_text(
        dependency_license_inventory._render_text(document),
        encoding="utf-8",
        newline="\n",
    )
    return data_root.joinpath("THIRD-PARTY-LICENSES", copied_path)


def _write_fake_frozen_runtime_inventory(data_root, lock, system):
    runtime_sources = {
        "cpython-runtime": {
            "filename": "Python-3.12.13.tar.xz",
            "name": "CPython",
            "sha256": "1" * 64,
            "url": "https://www.python.org/Python-3.12.13.tar.xz",
            "version": "3.12.13",
        },
        "pyinstaller-bootloader": {
            "filename": "pyinstaller-6.21.0.tar.gz",
            "name": "PyInstaller",
            "sha256": "2" * 64,
            "url": "https://files.pythonhosted.org/pyinstaller-6.21.0.tar.gz",
            "version": "6.21.0",
        },
    }
    dependency_source = {
        "filename": "example-1.0.tar.gz",
        "kind": "pypi-sdist",
        "name": "Example",
        "provides": ["example"],
        "sha256": "3" * 64,
        "size": 1024,
        "url": "https://files.pythonhosted.org/example-1.0.tar.gz",
        "version": "1.0",
    }
    source_lock = lock.with_name("release-sources.json")
    source_lock.write_text(
        json.dumps(
            {
                "schema": "dupeguru.release-source-lock",
                "schema_version": 1,
                "sources": [
                    dependency_source,
                    *[
                        {
                            **source,
                            "kind": "test-source",
                            "provides": [provider],
                            "size": 1024,
                        }
                        for provider, source in runtime_sources.items()
                    ],
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    data_root.joinpath("release-sources.json").write_bytes(source_lock.read_bytes())
    root = data_root / "FROZEN-RUNTIME-LICENSES"
    files = {
        "components/cpython-runtime-3.12.13/LICENSE.txt": (b"PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n"),
        "components/pyinstaller-bootloader-6.21.0/COPYING.txt": (b"Bootloader Exception\n"),
    }
    components = []
    for provider, (path, content) in zip(
        ("cpython-runtime", "pyinstaller-bootloader"),
        files.items(),
        strict=True,
    ):
        _write_file(root / path, content)
        components.append(
            {
                "component": provider,
                "files": [
                    {
                        "copied_path": path,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                        "source_path": Path(path).name,
                    }
                ],
                "license_designation": "test license designation",
                "name": runtime_sources[provider]["name"],
                "source_archive": runtime_sources[provider],
                "version": runtime_sources[provider]["version"],
            }
        )
    document = {
        "components": components,
        "generated_at": "2023-11-14T22:13:20Z",
        "platform": {"machine": "x86_64", "system": system},
        "schema": "dupeguru.frozen-runtime-license-inventory",
        "schema_version": 1,
        "source_lock": {
            "path": "release-sources.json",
            "sha256": hashlib.sha256(source_lock.read_bytes()).hexdigest(),
        },
    }
    root.joinpath("index.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    root.joinpath("index.txt").write_text(
        frozen_runtime_license_inventory._render_text(document),
        encoding="utf-8",
        newline="\n",
    )


def test_portable_zip_is_deterministic_and_structurally_verified(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    root = _windows_tree(tmp_path / "source")
    name = portable_bundle.portable_archive_name(
        "5.0.0",
        "windows",
        "x86_64",
    )
    first = portable_bundle.create_deterministic_zip(root, tmp_path / "one" / name)
    second = portable_bundle.create_deterministic_zip(root, tmp_path / "two" / name)

    assert first.read_bytes() == second.read_bytes()
    portable_bundle.verify_portable_archive(first)


def test_portable_zip_streams_regular_files(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    root = _windows_tree(tmp_path / "source")
    payload = root / "_internal" / "large-qt-runtime.dll"
    payload.write_bytes(b"runtime" * (1024 * 1024))
    original_read_bytes = Path.read_bytes

    def reject_payload_read_bytes(path):
        if path == payload:
            raise AssertionError("portable ZIP source files must be streamed")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_payload_read_bytes)
    name = portable_bundle.portable_archive_name(
        "5.0.0",
        "windows",
        "x86_64",
    )
    archive_path = portable_bundle.create_deterministic_zip(
        root,
        tmp_path / name,
    )

    with zipfile.ZipFile(archive_path) as archive:
        assert archive.getinfo("dupeguru-neo/_internal/large-qt-runtime.dll").file_size == payload.stat().st_size


def test_windows_portable_requires_the_console_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    root = _windows_tree(tmp_path / "source")
    root.joinpath("dupeguru.exe").unlink()
    name = portable_bundle.portable_archive_name(
        "5.0.0",
        "windows",
        "x86_64",
    )
    archive_path = portable_bundle.create_deterministic_zip(
        root,
        tmp_path / name,
    )

    with pytest.raises(RuntimeError, match="dupeguru.exe"):
        portable_bundle.verify_portable_archive(archive_path)


def test_windows_cli_freezer_is_console_only_and_excludes_qt(tmp_path):
    project_root = tmp_path / "project"
    arguments = portable_bundle._windows_cli_pyinstaller_arguments(
        project_root,
        tmp_path / "build",
    )

    assert "--name=dupeguru" in arguments
    assert "--onefile" in arguments
    assert "--console" in arguments
    assert "--windowed" not in arguments
    assert "--exclude-module=PyQt6" in arguments
    assert "--exclude-module=qt" in arguments
    assert not any(argument.startswith("--add-data") for argument in arguments)
    assert arguments[-1] == str(project_root / "run_cli.py")


def test_windows_bundle_smokes_gui_and_qt_free_cli(tmp_path, monkeypatch):
    root = tmp_path / "dupeguru-neo"
    _write_file(root / "dupeguru-neo.exe", executable=True)
    _write_file(root / "dupeguru.exe", executable=True)
    calls = []

    def run_frozen(command, *, env):
        rendered = tuple(str(item) for item in command)
        calls.append(rendered)
        executable = Path(rendered[0]).name
        arguments = rendered[1:]
        if executable == "dupeguru.exe" and arguments == ("--version",):
            stdout = "5.0.0\n"
        elif executable == "dupeguru.exe" and arguments == ("doctor",):
            stdout = json.dumps(
                {
                    "schema": "dupeguru.doctor-report",
                    "pyqt_imported": False,
                }
            )
        elif executable == "dupeguru.exe" and arguments == (
            "schema",
            "deletion-plan",
        ):
            stdout = json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "urn:dupeguru-neo:schema:deletion-plan:1",
                }
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(rendered, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(portable_bundle, "_run_frozen", run_frozen)

    portable_bundle.smoke_frozen_bundle(root, "windows", "5.0.0")

    assert [call[1:] for call in calls] == [
        ("--version",),
        ("--self-test",),
        ("--version",),
        ("doctor",),
        ("schema", "deletion-plan"),
    ]


@pytest.mark.parametrize(
    ("platform_name", "tree_factory"),
    [
        ("linux", _linux_tree),
        ("macos", _macos_tree),
    ],
)
def test_portable_tar_is_deterministic_and_structurally_verified(
    tmp_path,
    monkeypatch,
    platform_name,
    tree_factory,
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    root = tree_factory(tmp_path / "source")
    name = portable_bundle.portable_archive_name(
        "5.0.0",
        platform_name,
        "x86_64",
    )
    first = portable_bundle.create_deterministic_tar(root, tmp_path / "one" / name)
    second = portable_bundle.create_deterministic_tar(root, tmp_path / "two" / name)

    assert first.read_bytes() == second.read_bytes()
    portable_bundle.verify_portable_archive(first)


def test_archive_verification_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    archive_path = tmp_path / portable_bundle.portable_archive_name(
        "5.0.0",
        "windows",
        "x86_64",
    )
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape", b"unsafe")

    with pytest.raises(RuntimeError, match="unsafe archive member"):
        portable_bundle.verify_portable_archive(archive_path)


def test_archive_verification_revalidates_embedded_license_inventory(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    distribution, installation_root = _fake_distribution(tmp_path)
    monkeypatch.setattr(
        dependency_license_inventory.metadata,
        "distribution",
        lambda _name: distribution,
    )
    lock = tmp_path / "requirements-release.txt"
    lock.write_text("Example==1.0\n", encoding="utf-8", newline="\n")
    root = _windows_tree(tmp_path / "source")
    embedded_license = _write_fake_license_inventory(
        root / "_internal",
        lock,
        "Windows",
        distribution,
        installation_root,
    )
    name = portable_bundle.portable_archive_name(
        "5.0.0",
        "windows",
        "x86_64",
    )
    archive = portable_bundle.create_deterministic_zip(root, tmp_path / "ok" / name)

    portable_bundle.verify_portable_archive(
        archive,
        lock,
        installation_root=installation_root,
    )

    installed_module = distribution.root / "example" / "__init__.py"
    original_module = installed_module.read_bytes()
    installed_module.write_bytes(b"changed after portable assembly\n")
    with pytest.raises(RuntimeError, match="installed distribution provenance mismatch"):
        portable_bundle.verify_portable_archive(
            archive,
            lock,
            installation_root=installation_root,
        )
    installed_module.write_bytes(original_module)

    embedded_license.write_bytes(b"tampered")
    tampered = portable_bundle.create_deterministic_zip(
        root,
        tmp_path / "tampered" / name,
    )
    with pytest.raises(RuntimeError, match="(?:size|digest) mismatch"):
        portable_bundle.verify_portable_archive(
            tampered,
            lock,
            installation_root=installation_root,
        )

    embedded_license.write_bytes(b"Example license\n")
    frozen_notice = (
        root / "_internal" / "FROZEN-RUNTIME-LICENSES" / "components" / "pyinstaller-bootloader-6.21.0" / "COPYING.txt"
    )
    frozen_notice.write_bytes(b"Bootloader notice removed\n")
    tampered_frozen = portable_bundle.create_deterministic_zip(
        root,
        tmp_path / "tampered-frozen" / name,
    )
    with pytest.raises(RuntimeError, match="frozen-runtime license digest mismatch"):
        portable_bundle.verify_portable_archive(
            tampered_frozen,
            lock,
            installation_root=installation_root,
        )


def test_release_policy_accepts_payload_without_portable_artifacts(tmp_path):
    wheel = tmp_path / "dupeguru_neo-5.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("dupeguru_neo-5.0.0.dist-info/METADATA", b"Name: dupeguru-neo\n")
        archive.writestr("core/__init__.py", b"")
    _write_file(tmp_path / "SHA256SUMS")

    portable_bundle.enforce_release_policy(tmp_path, "5.0.0")


def test_release_policy_accepts_normal_wheel_sdist_and_source_archives(tmp_path):
    wheel = tmp_path / "dupeguru_neo-5.0.0-cp312-cp312-win_amd64.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("core/__init__.py", b"")
        archive.writestr(
            "core/pe/_block.cp312-win_amd64.pyd",
            b"MZ\x90\x00" + b"native Python extension",
        )
    for name, root in (
        ("dupeguru_neo-5.0.0.tar.gz", "dupeguru_neo-5.0.0"),
        ("dupeguru-neo-5.0.0-source.tar.gz", "dupeguru-neo-5.0.0-source"),
    ):
        with tarfile.open(tmp_path / name, "w:gz") as archive:
            content = b"from setuptools import setup\n"
            member = tarfile.TarInfo(f"{root}/setup.py")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))

    portable_bundle.enforce_release_policy(tmp_path, "5.0.0")


@pytest.mark.parametrize(
    "name",
    [
        "dupeguru-neo-5.0.0-windows-x86_64-unsigned-portable.zip",
        "dupeguru-neo-5.0.0-linux-x86_64-unsigned-portable.tar.gz",
        "Dupe_Guru-Neo-5.0.0-win64-standalone-bundle.bin",
    ],
)
def test_release_policy_rejects_portable_artifact_name_variants(tmp_path, name):
    _write_file(tmp_path / name)

    with pytest.raises(RuntimeError, match="portable release artifacts are disabled"):
        portable_bundle.enforce_release_policy(tmp_path, "5.0.0")


@pytest.mark.parametrize(
    ("archive_name", "tree_factory", "archive_factory"),
    [
        ("renamed-release-assets.zip", _windows_tree, portable_bundle.create_deterministic_zip),
        ("renamed-release-assets.data", _linux_tree, portable_bundle.create_deterministic_tar),
    ],
)
def test_release_policy_rejects_renamed_portable_archive_by_content(
    tmp_path,
    monkeypatch,
    archive_name,
    tree_factory,
    archive_factory,
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    payload = tmp_path / "payload"
    payload.mkdir()
    root = tree_factory(tmp_path / "source")
    archive_factory(root, payload / archive_name)

    with pytest.raises(RuntimeError, match="portable release artifacts are disabled"):
        portable_bundle.enforce_release_policy(payload, "5.0.0")


def test_release_policy_rejects_nested_portable_archive_name(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    payload = tmp_path / "payload"
    payload.mkdir()
    companion = tmp_path / "dupeguru-neo-5.0.0-source-companion"
    _write_file(
        companion / "upstream" / "DupeGuru-Neo-Windows-Portable.zip",
    )
    portable_bundle.create_deterministic_tar(
        companion,
        payload / "dupeguru-neo-5.0.0-source-companion.tar",
    )

    with pytest.raises(RuntimeError, match="release artifacts are disabled"):
        portable_bundle.enforce_release_policy(payload, "5.0.0")


def test_release_policy_rejects_top_level_source_companion_independently(tmp_path):
    archive_path = tmp_path / "dupeguru-neo-5.0.0-source-companion.tar"
    with tarfile.open(archive_path, "w") as archive:
        content = b'{"schema":"dupeguru.source-companion-manifest"}'
        member = tarfile.TarInfo("dupeguru-neo-5.0.0-source-companion/SOURCE-MANIFEST.json")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))

    with pytest.raises(RuntimeError, match="source-companion release artifacts are disabled"):
        portable_bundle.enforce_release_policy(tmp_path, "5.0.0")


def test_release_policy_rejects_renamed_nested_portable_by_magic(tmp_path):
    inner = _zip_bytes(
        {
            "renamed/application.data": b"MZ\x90\x00" + b"portable executable",
            "renamed/runtime.data": b"runtime",
        }
    )
    wheel = tmp_path / "dupeguru_neo-5.0.0-cp312-cp312-win_amd64.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("dupeguru_neo/payload.data", inner)

    with pytest.raises(RuntimeError, match="portable release artifacts are disabled"):
        portable_bundle.enforce_release_policy(tmp_path, "5.0.0")


def test_release_policy_rejects_renamed_nested_source_companion(tmp_path):
    companion = _tar_bytes(
        {
            "renamed-root/renamed-manifest.data": (
                b"\xef\xbb\xbf" + b" " * 700 + b'{"schema":"dupeguru.source-companion-manifest","schema_version":1}'
            ),
        }
    )
    wheel = tmp_path / "dupeguru_neo-5.0.0-cp312-cp312-win_amd64.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("dupeguru_neo/payload.data", companion)

    with pytest.raises(RuntimeError, match="portable release artifacts are disabled"):
        portable_bundle.enforce_release_policy(tmp_path, "5.0.0")


def test_release_policy_rejects_corrupt_renamed_nested_archive(tmp_path):
    wheel = tmp_path / "dupeguru_neo-5.0.0-cp312-cp312-win_amd64.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("dupeguru_neo/payload.data", b"PK\x03\x04not-a-valid-archive")

    with pytest.raises(RuntimeError, match="cannot prove nested release archive is portable-free"):
        portable_bundle.enforce_release_policy(tmp_path, "5.0.0")


def test_release_policy_bounds_nested_archive_recursion(tmp_path, monkeypatch):
    monkeypatch.setattr(portable_bundle, "_MAX_RELEASE_ARCHIVE_DEPTH", 1)
    deepest = _zip_bytes({"payload.txt": b"ordinary data"})
    middle = _zip_bytes({"nested.data": deepest})
    wheel = tmp_path / "dupeguru_neo-5.0.0-cp312-cp312-win_amd64.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("dupeguru_neo/payload.data", middle)

    with pytest.raises(RuntimeError, match="recursion-depth limit"):
        portable_bundle.enforce_release_policy(tmp_path, "5.0.0")


def test_release_policy_bounds_member_count_and_uncompressed_bytes(tmp_path, monkeypatch):
    wheel = tmp_path / "dupeguru_neo-5.0.0-cp312-cp312-win_amd64.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("first.txt", b"123456")
        archive.writestr("second.txt", b"123456")

    monkeypatch.setattr(portable_bundle, "_MAX_RELEASE_ARCHIVE_MEMBERS", 1)
    with pytest.raises(RuntimeError, match="member count"):
        portable_bundle.enforce_release_policy(tmp_path, "5.0.0")

    monkeypatch.setattr(portable_bundle, "_MAX_RELEASE_ARCHIVE_MEMBERS", 100)
    monkeypatch.setattr(portable_bundle, "_MAX_RELEASE_ARCHIVE_MEMBER_SIZE", 5)
    with pytest.raises(RuntimeError, match="member exceeds the size limit"):
        portable_bundle.enforce_release_policy(tmp_path, "5.0.0")

    monkeypatch.setattr(portable_bundle, "_MAX_RELEASE_ARCHIVE_MEMBER_SIZE", 100)
    monkeypatch.setattr(portable_bundle, "_MAX_RELEASE_ARCHIVE_TOTAL_SIZE", 10)
    with pytest.raises(RuntimeError, match="uncompressed bytes"):
        portable_bundle.enforce_release_policy(tmp_path, "5.0.0")


def test_release_policy_bounds_archive_compression_ratio(tmp_path, monkeypatch):
    monkeypatch.setattr(portable_bundle, "_MAX_RELEASE_ARCHIVE_COMPRESSION_RATIO", 2)
    wheel = tmp_path / "dupeguru_neo-5.0.0-cp312-cp312-win_amd64.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("highly-compressible.txt", b"\0" * 4096)

    with pytest.raises(RuntimeError, match="compression-ratio limit"):
        portable_bundle.enforce_release_policy(tmp_path, "5.0.0")


def test_release_policy_rejects_tar_links(tmp_path):
    source = tmp_path / "dupeguru-neo-5.0.0-source.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        member = tarfile.TarInfo("dupeguru-neo-5.0.0-source/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "target"
        archive.addfile(member)

    with pytest.raises(RuntimeError, match="release TAR contains a link"):
        portable_bundle.enforce_release_policy(tmp_path, "5.0.0")


def test_release_policy_allows_source_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    payload = tmp_path / "payload"
    payload.mkdir()
    source = tmp_path / "dupeguru-neo-5.0.0"
    _write_file(source / "setup.py", b"from setuptools import setup\n")
    portable_bundle.create_deterministic_tar(
        source,
        payload / "dupeguru-neo-5.0.0-source.tar.gz",
    )

    portable_bundle.enforce_release_policy(payload, "5.0.0")


def test_release_policy_rejects_native_artifacts_and_disguised_executables(
    tmp_path,
):
    _write_file(tmp_path / "dupeguru-neo-5.0.0-installer.msi")
    with pytest.raises(RuntimeError, match="native installer/application"):
        portable_bundle.enforce_release_policy(tmp_path, "5.0.0")

    (tmp_path / "dupeguru-neo-5.0.0-installer.msi").unlink()
    _write_file(tmp_path / "renamed-native-payload.bin", b"MZ\x90\x00")
    with pytest.raises(RuntimeError, match="native executable"):
        portable_bundle.enforce_release_policy(tmp_path, "5.0.0")


def test_release_policy_fails_closed_for_unreadable_archive(tmp_path):
    _write_file(tmp_path / "release-assets.zip", b"not a ZIP archive")

    with pytest.raises(RuntimeError, match="cannot prove release archive is portable-free"):
        portable_bundle.enforce_release_policy(tmp_path, "5.0.0")


def test_archive_creation_requires_source_date_epoch(tmp_path, monkeypatch):
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    root = _windows_tree(tmp_path / "source")
    name = portable_bundle.portable_archive_name(
        "5.0.0",
        "windows",
        "x86_64",
    )

    with pytest.raises(RuntimeError, match="SOURCE_DATE_EPOCH"):
        portable_bundle.create_deterministic_zip(root, tmp_path / name)
