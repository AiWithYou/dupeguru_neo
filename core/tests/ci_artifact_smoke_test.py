from pathlib import Path

import pytest

from scripts import ci_artifact_smoke


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
