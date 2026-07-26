from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from scripts import ci_artifact_smoke


def test_captured_command_failure_reports_bounded_stdout_and_stderr(monkeypatch):
    stdout = "x" * (ci_artifact_smoke.MAX_COMMAND_DIAGNOSTIC_CHARACTERS + 10)
    stderr = "scan root is incomplete"

    def fail(*_args, **_kwargs):
        raise ci_artifact_smoke.subprocess.CalledProcessError(
            4,
            ["dupeguru", "scan"],
            output=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(ci_artifact_smoke.subprocess, "run", fail)

    with pytest.raises(RuntimeError) as caught:
        ci_artifact_smoke._run(
            ["dupeguru", "scan"],
            capture_output=True,
        )

    message = str(caught.value)
    assert "exit code 4" in message
    assert "scan root is incomplete" in message
    assert "x" * ci_artifact_smoke.MAX_COMMAND_DIAGNOSTIC_CHARACTERS in message
    assert "x" * (ci_artifact_smoke.MAX_COMMAND_DIAGNOSTIC_CHARACTERS + 1) not in message


def test_console_script_uses_sysconfig_scripts_directory(tmp_path, monkeypatch):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    suffix = ".exe" if ci_artifact_smoke.os.name == "nt" else ""
    expected = scripts / f"pyproject-build{suffix}"
    expected.write_bytes(b"launcher")
    monkeypatch.setattr(
        ci_artifact_smoke.sysconfig,
        "get_path",
        lambda name: str(scripts) if name == "scripts" else None,
    )

    assert ci_artifact_smoke._console_script("pyproject-build") == expected


def test_console_script_rejects_missing_scripts_directory(monkeypatch):
    monkeypatch.setattr(ci_artifact_smoke.sysconfig, "get_path", lambda _name: None)

    with pytest.raises(
        RuntimeError,
        match="did not report an installed console-script directory",
    ):
        ci_artifact_smoke._console_script("pyproject-build")


def _write_test_wheel(path, members):
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for info, payload in members:
            archive.writestr(info, payload)


def test_wheel_difference_report_names_changed_member_payload(tmp_path):
    original = tmp_path / "original.whl"
    rebuilt = tmp_path / "rebuilt.whl"
    _write_test_wheel(
        original,
        (
            (ZipInfo("package/__init__.py", (2024, 1, 1, 0, 0, 0)), b"same"),
            (ZipInfo("package/native.so", (2024, 1, 1, 0, 0, 0)), b"first native payload"),
        ),
    )
    _write_test_wheel(
        rebuilt,
        (
            (ZipInfo("package/__init__.py", (2024, 1, 1, 0, 0, 0)), b"same"),
            (ZipInfo("package/native.so", (2024, 1, 1, 0, 0, 0)), b"second native payload"),
        ),
    )

    report = ci_artifact_smoke._wheel_difference_report(original, rebuilt)

    assert "'package/native.so': payload differs" in report
    assert "'package/__init__.py'" not in report


def test_wheel_difference_report_names_zip_metadata_change(tmp_path):
    original = tmp_path / "original.whl"
    rebuilt = tmp_path / "rebuilt.whl"
    original_info = ZipInfo("package/module.py", (2024, 1, 1, 0, 0, 0))
    rebuilt_info = ZipInfo("package/module.py", (2025, 1, 1, 0, 0, 0))
    _write_test_wheel(original, ((original_info, b"same payload"),))
    _write_test_wheel(rebuilt, ((rebuilt_info, b"same payload"),))

    report = ci_artifact_smoke._wheel_difference_report(original, rebuilt)

    assert "'package/module.py': ZIP metadata differs (date_time)" in report
    assert "payload differs" not in report


def test_darwin_wheel_validation_requires_loadable_build_uuid(tmp_path, monkeypatch):
    wheel = tmp_path / "package.whl"
    _write_test_wheel(
        wheel,
        ((ZipInfo("package/native.so", (2024, 1, 1, 0, 0, 0)), b"Mach-O placeholder"),),
    )
    monkeypatch.setattr(ci_artifact_smoke.sys, "platform", "darwin")
    monkeypatch.setattr(
        ci_artifact_smoke,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=""),
    )

    with pytest.raises(RuntimeError, match="has no valid LC_UUID"):
        ci_artifact_smoke._validate_darwin_wheel_build_uuids(wheel)


def test_darwin_wheel_validation_accepts_distinct_uuid_per_architecture(tmp_path, monkeypatch):
    wheel = tmp_path / "package.whl"
    _write_test_wheel(
        wheel,
        ((ZipInfo("package/native.so", (2024, 1, 1, 0, 0, 0)), b"Mach-O placeholder"),),
    )
    monkeypatch.setattr(ci_artifact_smoke.sys, "platform", "darwin")

    def fake_dwarfdump(command, **_kwargs):
        path = command[-1]
        return SimpleNamespace(
            stdout=(
                f"UUID: 11111111-1111-1111-1111-111111111111 (arm64) {path}\n"
                f"UUID: 22222222-2222-2222-2222-222222222222 (x86_64) {path}\n"
            )
        )

    monkeypatch.setattr(ci_artifact_smoke, "_run", fake_dwarfdump)

    ci_artifact_smoke._validate_darwin_wheel_build_uuids(wheel)


def test_darwin_wheel_validation_accepts_uuid_reused_by_byte_identical_image(tmp_path, monkeypatch):
    wheel = tmp_path / "package.whl"
    member_payload = b"identical Mach-O placeholder"
    _write_test_wheel(
        wheel,
        (
            (ZipInfo("package/first.so", (2024, 1, 1, 0, 0, 0)), member_payload),
            (ZipInfo("package/second.so", (2024, 1, 1, 0, 0, 0)), member_payload),
        ),
    )
    monkeypatch.setattr(ci_artifact_smoke.sys, "platform", "darwin")

    def fake_dwarfdump(command, **_kwargs):
        path = command[-1]
        return SimpleNamespace(stdout=f"UUID: 11111111-1111-1111-1111-111111111111 (arm64) {path}\n")

    monkeypatch.setattr(ci_artifact_smoke, "_run", fake_dwarfdump)

    ci_artifact_smoke._validate_darwin_wheel_build_uuids(wheel)


def test_darwin_wheel_validation_rejects_uuid_reused_by_different_member_payload(tmp_path, monkeypatch):
    wheel = tmp_path / "package.whl"
    _write_test_wheel(
        wheel,
        (
            (ZipInfo("package/first.so", (2024, 1, 1, 0, 0, 0)), b"first Mach-O placeholder"),
            (ZipInfo("package/second.so", (2024, 1, 1, 0, 0, 0)), b"second Mach-O placeholder"),
        ),
    )
    monkeypatch.setattr(ci_artifact_smoke.sys, "platform", "darwin")

    def fake_dwarfdump(command, **_kwargs):
        path = command[-1]
        return SimpleNamespace(stdout=f"UUID: 11111111-1111-1111-1111-111111111111 (arm64) {path}\n")

    monkeypatch.setattr(ci_artifact_smoke, "_run", fake_dwarfdump)

    with pytest.raises(RuntimeError, match="reuses LC_UUID"):
        ci_artifact_smoke._validate_darwin_wheel_build_uuids(wheel)


def test_darwin_wheel_validation_rejects_uuid_reused_by_different_architecture(tmp_path, monkeypatch):
    wheel = tmp_path / "package.whl"
    member_payload = b"identical Mach-O placeholder"
    _write_test_wheel(
        wheel,
        (
            (ZipInfo("package/first.so", (2024, 1, 1, 0, 0, 0)), member_payload),
            (ZipInfo("package/second.so", (2024, 1, 1, 0, 0, 0)), member_payload),
        ),
    )
    monkeypatch.setattr(ci_artifact_smoke.sys, "platform", "darwin")

    def fake_dwarfdump(command, **_kwargs):
        path = command[-1]
        architecture = "arm64" if path.name == "extension-0.so" else "x86_64"
        return SimpleNamespace(stdout=f"UUID: 11111111-1111-1111-1111-111111111111 ({architecture}) {path}\n")

    monkeypatch.setattr(ci_artifact_smoke, "_run", fake_dwarfdump)

    with pytest.raises(RuntimeError, match="different architecture"):
        ci_artifact_smoke._validate_darwin_wheel_build_uuids(wheel)


@pytest.mark.parametrize(
    ("version_info", "expected_filter"),
    (
        ((3, 11), None),
        ((3, 12), "data"),
    ),
)
def test_validated_tar_extraction_uses_version_compatible_filter(
    tmp_path,
    monkeypatch,
    version_info,
    expected_filter,
):
    calls = []

    class Archive:
        def extractall(self, destination, **kwargs):
            calls.append((destination, kwargs))

    monkeypatch.setattr(ci_artifact_smoke.sys, "version_info", version_info)
    members = (object(),)

    ci_artifact_smoke._extract_validated_tar(
        Archive(),
        Path(tmp_path),
        members=members,
    )

    expected_kwargs = {"members": members}
    if expected_filter is not None:
        expected_kwargs["filter"] = expected_filter
    assert calls == [(Path(tmp_path), expected_kwargs)]
