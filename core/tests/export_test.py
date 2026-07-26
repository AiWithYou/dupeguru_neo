# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import csv
from pathlib import Path

import pytest

from core import export


def test_xhtml_export_escapes_untrusted_headers_names_and_metadata():
    destination = Path(
        export.export_to_xhtml(
            ['Name"><script>header()</script>', "Metadata"],
            [
                [0, '<img src=x onerror="filename()">', "<script>cell() & more</script>"],
            ],
        )
    )
    payload = destination.read_text(encoding="utf-8")

    assert "<script>" not in payload
    assert "<img src=x" not in payload
    assert "&lt;script&gt;cell() &amp; more&lt;/script&gt;" in payload
    assert "&quot;" in payload


def test_xhtml_export_replaces_non_xml_unicode_and_rejects_bad_row_shape():
    destination = Path(export.export_to_xhtml(["Name"], [[0, "bad\0\ud800name"]]))
    payload = destination.read_text(encoding="utf-8")
    assert "\0" not in payload
    assert "\ud800" not in payload
    assert "bad\ufffd\ufffdname" in payload

    with pytest.raises(ValueError, match="expected 2"):
        export.export_to_xhtml(["Name"], [[0, "name", "extra"]])


def test_xhtml_export_streams_rows_from_an_iterator():
    rows = ([group_id, "file-{}.bin".format(group_id)] for group_id in range(10_000))
    destination = Path(export.export_to_xhtml(["Name"], rows))
    payload = destination.read_text(encoding="utf-8")
    assert "file-0.bin" in payload
    assert "file-9999.bin" in payload


@pytest.mark.parametrize(
    "dangerous",
    [
        '=HYPERLINK("https://example.invalid")',
        "+SUM(1,2)",
        "-1+2",
        "@SUM(1,2)",
        "\t=cmd",
        "\r=cmd",
        "\n=cmd",
        "  =cmd",
    ],
)
def test_csv_export_neutralizes_spreadsheet_formula_cells(tmp_path, dangerous):
    destination = tmp_path / "results.csv"
    export.export_to_csv(destination, ["Name"], [[0, dangerous]])

    with destination.open("r", encoding="utf-8", newline="") as stream:
        records = list(csv.reader(stream))
    assert records[0] == ["Group ID", "Name"]
    assert records[1] == ["0", "'" + dangerous]


def test_csv_export_preserves_numeric_values_and_atomically_replaces_destination(tmp_path):
    destination = tmp_path / "results.csv"
    destination.write_bytes(b"old content")

    export.export_to_csv(destination, ["Size"], [[7, -12]])

    with destination.open("r", encoding="utf-8", newline="") as stream:
        assert list(csv.reader(stream)) == [["Group ID", "Size"], ["7", "-12"]]
    assert not list(tmp_path.glob(".results.csv.*.tmp"))


def test_csv_export_failure_preserves_existing_destination_and_cleans_temporary(tmp_path, monkeypatch):
    destination = tmp_path / "results.csv"
    destination.write_bytes(b"must survive")

    def fail_replace(_source, _destination):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(export.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated publish failure"):
        export.export_to_csv(destination, ["Name"], [[0, "safe"]])

    assert destination.read_bytes() == b"must survive"
    assert not list(tmp_path.glob(".results.csv.*.tmp"))


def test_csv_export_rejects_bad_row_shape_without_replacing_destination(tmp_path):
    destination = tmp_path / "results.csv"
    destination.write_bytes(b"must survive")

    with pytest.raises(ValueError, match="expected 2"):
        export.export_to_csv(destination, ["Name"], [[0, "name", "extra"]])

    assert destination.read_bytes() == b"must survive"
    assert not list(tmp_path.glob(".results.csv.*.tmp"))
