import hashlib
import os
from pathlib import Path
import stat
import subprocess
import zipfile

import pytest

from scripts import desktop_bundle


def _write(path, content=b"payload", *, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if executable:
        path.chmod(0o755)


def _macos_app(parent):
    root = parent / "dupeguru-neo.app"
    executable = root / "Contents" / "MacOS" / "dupeguru-neo"
    resources = root / "Contents" / "Resources"
    _write(executable, executable=True)
    for name in (
        "LICENSE",
        "PORTABLE-NOTICE.txt",
        "THIRD_PARTY_NOTICES.md",
        "requirements-release.txt",
        "release-sources.json",
        "hscommon/LICENSE",
        "THIRD-PARTY-LICENSES/index.json",
        "THIRD-PARTY-LICENSES/index.txt",
        "FROZEN-RUNTIME-LICENSES/index.json",
        "FROZEN-RUNTIME-LICENSES/index.txt",
    ):
        _write(resources / name)
    return root


@pytest.mark.parametrize(
    ("platform_name", "expected"),
    [
        (
            "windows",
            "dupeguru-neo-5.0.0-windows-x86_64-unsigned.exe",
        ),
        (
            "macos",
            "dupeguru-neo-5.0.0-macos-x86_64-adhoc.app.zip",
        ),
    ],
)
def test_desktop_artifact_names_are_explicit_about_platform_trust(platform_name, expected):
    assert desktop_bundle.desktop_artifact_name("5.0.0", platform_name, "x86_64") == expected


def test_desktop_artifact_name_rejects_unsupported_targets_and_unsafe_architecture():
    with pytest.raises(RuntimeError, match="unsupported"):
        desktop_bundle.desktop_artifact_name("5.0.0", "linux", "x86_64")
    with pytest.raises(RuntimeError, match="unsafe desktop architecture"):
        desktop_bundle.desktop_artifact_name("5.0.0", "windows", "../x64")


def test_windows_standalone_freezer_is_windowed_onefile_with_legal_data_and_version(tmp_path):
    project_root = tmp_path / "project"
    build_root = tmp_path / "build"
    arguments = desktop_bundle._windows_pyinstaller_arguments(
        project_root,
        build_root,
        tmp_path / "licenses",
        tmp_path / "runtime-licenses",
        tmp_path / "version.txt",
    )

    assert "--onefile" in arguments
    assert "--onedir" not in arguments
    assert "--windowed" in arguments
    assert "--console" not in arguments
    assert f"--version-file={tmp_path / 'version.txt'}" in arguments
    assert any("PORTABLE-NOTICE.txt" in argument for argument in arguments)
    assert any("THIRD-PARTY-LICENSES" in argument for argument in arguments)
    assert any("FROZEN-RUNTIME-LICENSES" in argument for argument in arguments)
    assert arguments[-1] == str(project_root / "run.py")


def test_windows_version_resource_is_user_facing_and_numeric(tmp_path):
    path = desktop_bundle._windows_version_file(tmp_path / "version.txt", "5.0.0rc1")
    text = path.read_text(encoding="utf-8")

    assert "filevers=(5, 0, 0, 0)" in text
    assert "ProductName', u'dupeGuru Neo'" in text
    assert "FileDescription', u'dupeGuru Neo'" in text
    assert "OriginalFilename', u'dupeguru-neo.exe'" in text
    assert "ProductVersion', u'5.0.0rc1'" in text


def test_windows_smoke_runs_version_and_offscreen_self_test(tmp_path, monkeypatch):
    executable = tmp_path / "dupeguru-neo.exe"
    executable.write_bytes(b"MZ")
    calls = []

    def run_frozen(command, *, env):
        calls.append((tuple(command), env))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_bundle.portable_bundle, "_run_frozen", run_frozen)

    desktop_bundle._smoke_windows_executable(executable)

    assert [call[0][1:] for call in calls] == [
        ("--version",),
        ("--self-test",),
    ]
    assert all(call[1]["QT_QPA_PLATFORM"] == "offscreen" for call in calls)
    assert all(call[1]["PYTHONUTF8"] == "1" for call in calls)


def test_macos_app_zip_is_deterministic_and_preserves_executable_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    app = _macos_app(tmp_path / "source")
    name = desktop_bundle.desktop_artifact_name("5.0.0", "macos", "arm64")

    first = desktop_bundle.create_macos_app_zip(app, tmp_path / "one" / name)
    second = desktop_bundle.create_macos_app_zip(app, tmp_path / "two" / name)

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        names = {info.filename for info in archive.infolist()}
        executable = archive.getinfo("dupeguru-neo.app/Contents/MacOS/dupeguru-neo")
        mode = executable.external_attr >> 16
    assert "dupeguru-neo.app/" in names
    assert "dupeguru-neo.app/Contents/Resources/LICENSE" in names
    assert stat.S_ISREG(mode)
    if os.name != "nt":
        assert stat.S_IMODE(mode) == 0o755


def test_macos_app_zip_round_trip_is_smoked_after_safe_extraction(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    app = _macos_app(tmp_path / "source")
    artifact = desktop_bundle.create_macos_app_zip(
        app,
        tmp_path / desktop_bundle.desktop_artifact_name("5.0.0", "macos", "arm64"),
    )
    calls = []

    monkeypatch.setattr(
        desktop_bundle.portable_bundle,
        "_verify_embedded_license_inventory",
        lambda *args, **kwargs: calls.append(("licenses", args, kwargs)),
    )
    monkeypatch.setattr(
        desktop_bundle.portable_bundle,
        "verify_unsigned_native_trust",
        lambda root, platform_name: calls.append(("trust", root, platform_name)),
    )
    monkeypatch.setattr(
        desktop_bundle,
        "_verify_macos_code_signature",
        lambda root: calls.append(("codesign", root)),
    )

    def smoke(root, platform_name, version):
        executable = root / "Contents" / "MacOS" / "dupeguru-neo"
        assert executable.is_file()
        if os.name != "nt":
            assert executable.stat().st_mode & stat.S_IXUSR
        calls.append(("smoke", platform_name, version))

    monkeypatch.setattr(desktop_bundle.portable_bundle, "smoke_frozen_bundle", smoke)

    desktop_bundle.verify_macos_app_zip(
        artifact,
        "5.0.0",
        Path(__file__).resolve().parents[2],
    )

    assert [call[0] for call in calls] == [
        "licenses",
        "trust",
        "codesign",
        "smoke",
    ]


def test_macos_app_zip_rejects_escaping_symlink_before_extraction(tmp_path):
    artifact = tmp_path / "unsafe.app.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        info = zipfile.ZipInfo("dupeguru-neo.app/escape")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "../../outside")

    with pytest.raises(RuntimeError, match="escapes its root|unsafe archive member"):
        desktop_bundle._extract_verified_macos_zip(
            artifact,
            tmp_path / "extracted",
        )


def test_desktop_sidecars_include_checksum_usage_and_exact_source(tmp_path, monkeypatch):
    artifact = tmp_path / "dupeguru-neo-5.0.0-windows-x86_64-unsigned.exe"
    artifact.write_bytes(b"desktop executable")
    commit = "1" * 40
    monkeypatch.setenv("GITHUB_SHA", commit)
    monkeypatch.setenv("GITHUB_REPOSITORY", "AiWithYou/dupeguru_neo")

    desktop_bundle._write_sidecars(
        artifact,
        "5.0.0",
        "windows",
        Path(__file__).resolve().parents[2],
        commit,
    )

    checksum = artifact.with_name(f"{artifact.name}.sha256").read_text(encoding="utf-8")
    guide = artifact.with_name("README-WINDOWS.txt").read_text(encoding="utf-8")
    assert checksum == f"{hashlib.sha256(artifact.read_bytes()).hexdigest()} *{artifact.name}\n"
    assert "EXEをダブルクリック" in guide
    assert "not Authenticode-signed" in guide
    assert f"https://github.com/AiWithYou/dupeguru_neo/tree/{commit}" in guide


def test_desktop_source_identity_rejects_a_dirty_tree(tmp_path, monkeypatch):
    commit = "1" * 40
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    results = iter(
        [
            subprocess.CompletedProcess([], 0, stdout=f"{commit}\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=" M run.py\n", stderr=""),
        ]
    )
    monkeypatch.setattr(desktop_bundle.subprocess, "run", lambda *args, **kwargs: next(results))

    with pytest.raises(RuntimeError, match="clean tracked and untracked source tree"):
        desktop_bundle._verified_source_commit(tmp_path)


def test_desktop_source_identity_rejects_checkout_normalized_legal_bytes(tmp_path, monkeypatch):
    source = tmp_path / "docs" / "PORTABLE-NOTICE.txt"
    _write(source, b"working tree\r\n")
    commit = "1" * 40
    monkeypatch.setattr(
        desktop_bundle,
        "_LEGAL_FILES",
        {"PORTABLE-NOTICE.txt": Path("docs", "PORTABLE-NOTICE.txt")},
    )
    monkeypatch.setattr(
        desktop_bundle.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout=b"committed tree\n",
            stderr=b"",
        ),
    )

    with pytest.raises(RuntimeError, match="differs byte-for-byte from its committed Git blob"):
        desktop_bundle._verify_committed_legal_source_bytes(tmp_path, commit)


def test_desktop_artifact_verification_rejects_a_foreign_host(tmp_path, monkeypatch):
    artifact = tmp_path / "dupeguru-neo-5.0.0-macos-arm64-adhoc.app.zip"
    artifact.write_bytes(b"placeholder")
    monkeypatch.setattr(desktop_bundle.portable_bundle, "_platform_name", lambda: "windows")

    with pytest.raises(RuntimeError, match="cannot be verified on this host"):
        desktop_bundle.verify_desktop_artifact(artifact, tmp_path)
