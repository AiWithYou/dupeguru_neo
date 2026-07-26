import json
from pathlib import Path
import zipfile

import pytest

from core.observability import (
    BuildIdentity,
    ObservabilityError,
    PathRedactor,
    StructuredEventLogger,
    create_crash_bundle,
)


def test_structured_event_redacts_sensitive_fields(tmp_path):
    log_path = tmp_path / "events.jsonl"
    redactor = PathRedactor(b"a deterministic test key")
    with StructuredEventLogger(
        log_path,
        identity=BuildIdentity("5.0.0", "abc123"),
        redactor=redactor,
        session_id="session-1",
    ) as logger:
        record = logger.event(
            "SCAN_FILE",
            scan_id="scan-1",
            fields={
                "path": r"C:\Users\secret\photo.png",
                "count": 4,
                "nested": {"reference_path": "/private/library/reference.png"},
            },
        )

    assert record["path"].startswith("path:")
    assert record["nested"]["reference_path"].startswith("path:")
    serialized = log_path.read_text(encoding="utf-8")
    assert "Users" not in serialized
    assert "private" not in serialized
    parsed = json.loads(serialized)
    assert parsed["count"] == 4
    assert parsed["build_commit"] == "abc123"


def test_path_redaction_is_stable_only_for_same_key():
    path = Path("/library/private.png")
    first = PathRedactor(b"first deterministic key").redact(path)
    assert first == PathRedactor(b"first deterministic key").redact(path)
    assert first != PathRedactor(b"other deterministic key").redact(path)


def test_crash_bundle_contains_only_explicit_files(tmp_path):
    log_path = tmp_path / "events.jsonl"
    log_path.write_text('{"event":"SAFE"}\n', encoding="utf-8")
    output = tmp_path / "crash.zip"
    create_crash_bundle(
        output,
        identity=BuildIdentity("5.0.0", "abc123"),
        crash_id="crash-1",
        traceback_text="safe traceback",
        metadata={"target_path": r"C:\private\file.jpg", "error_count": 1},
        redactor=PathRedactor(b"crash bundle test key"),
        log_paths=[log_path],
    )

    with zipfile.ZipFile(output) as bundle:
        assert set(bundle.namelist()) == {
            "traceback.txt",
            "manifest.json",
            "logs/event-00.jsonl",
        }
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["metadata"]["target_path"].startswith("path:")
        assert "private" not in bundle.read("manifest.json").decode("utf-8")


def test_crash_bundle_does_not_overwrite_existing_file(tmp_path):
    output = tmp_path / "crash.zip"
    output.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        create_crash_bundle(
            output,
            identity=BuildIdentity("5.0.0", "abc123"),
            crash_id="crash-1",
            traceback_text="trace",
            metadata={},
            redactor=PathRedactor(b"crash bundle test key"),
        )
    assert output.read_bytes() == b"existing"


def test_crash_bundle_requires_zip_suffix(tmp_path):
    with pytest.raises(ObservabilityError):
        create_crash_bundle(
            tmp_path / "crash.tar",
            identity=BuildIdentity("5.0.0", "abc123"),
            crash_id="crash-1",
            traceback_text="trace",
            metadata={},
            redactor=PathRedactor(b"crash bundle test key"),
        )
