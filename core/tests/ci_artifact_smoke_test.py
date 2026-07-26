from pathlib import Path

import pytest

from scripts import ci_artifact_smoke


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
