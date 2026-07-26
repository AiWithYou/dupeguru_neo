# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import argparse
import hashlib
import io
import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import catalog_cli
from core.catalog import Catalog, CatalogSchemaError
from core.catalog_cli import (
    CATALOG_BACKUP_SCHEMA,
    CATALOG_CHANGE_RECORD_SCHEMA,
    CATALOG_CHANGE_RECORD_SCHEMA_VERSION,
    CATALOG_ERROR_SCHEMA,
    CATALOG_GROUP_CHUNK_MAX_MEMBERS,
    CATALOG_GROUP_PAGE_MAX_FILES,
    CATALOG_GROUP_RECORD_SCHEMA,
    CATALOG_GROUP_RECORD_SCHEMA_VERSION,
    CATALOG_MACHINE_MAX_LINE_BYTES,
    CATALOG_MACHINE_MAX_RECORDS,
    CATALOG_MACHINE_MAX_TOTAL_BYTES,
    CATALOG_MAX_CHANGES,
    CATALOG_MAX_GROUP_MEMBERS,
    CATALOG_MAX_GROUPS,
    CATALOG_RESULT_SCHEMA,
    CATALOG_SCHEMA_VERSION,
    CATALOG_SCHEMAS,
    CATALOG_STATUS_SCHEMA,
    CatalogExitCode,
    add_catalog_parser,
    run_catalog_command,
)
from core.catalog_service import CatalogService, CatalogServiceStatus


def _invoke(arguments):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_catalog_parser(subparsers)
    args = parser.parse_args(["catalog", *arguments])
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = run_catalog_command(
        args,
        io.StringIO(),
        stdout,
        stderr,
    )
    records = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    return exit_code, records, stderr.getvalue()


def _create_roots(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    return first, second


def _catalog_family_bytes(database):
    return {path.name: path.read_bytes() for path in database.parent.glob(database.name + "*") if path.is_file()}


def _fake_relation_row(
    *,
    change_type="relocation_candidate",
    relation_evidence="same_catalog_generation",
    identity_proven=0,
    content_changed=0,
    old_content_version_id=41,
    new_content_version_id=41,
):
    row = {
        "change_type": change_type,
        "content_changed": content_changed,
        "identity_proven": identity_proven,
        "relation_evidence": relation_evidence,
        "old_native_file_id": b"stable-file-id",
        "new_native_file_id": b"stable-file-id",
        "old_path_key": "old.bin",
        "new_path_key": "new.bin",
    }
    for prefix, observation_id, path_id, content_version_id, path in (
        ("old", 11, 21, old_content_version_id, "old.bin"),
        ("new", 12, 22, new_content_version_id, "new.bin"),
    ):
        row.update(
            {
                "{}_observation_id".format(prefix): observation_id,
                "{}_root_id".format(prefix): 1,
                "{}_path_id".format(prefix): path_id,
                "{}_physical_file_id".format(prefix): 31,
                "{}_content_version_id".format(prefix): content_version_id,
                "{}_display_path".format(prefix): path,
                "{}_path_state".format(prefix): "active",
                "{}_content_state".format(prefix): "stable",
                "{}_identity_confidence".format(prefix): "stable",
            }
        )
    return row


def _reconstruct_group_records(records):
    groups = {}
    for record in records:
        record_type = record["record_type"]
        if record_type == "group_header":
            header = record["group_header"]
            groups[header["group_id"]] = {
                "header": header,
                "chunks": [],
                "end": None,
            }
        elif record_type == "member_chunk":
            chunk = record["member_chunk"]
            groups[chunk["group_id"]]["chunks"].append(chunk)
        elif record_type == "group_end":
            end = record["group_end"]
            groups[end["group_id"]]["end"] = end
    for value in groups.values():
        value["chunks"].sort(key=lambda chunk: chunk["chunk_index"])
        value["members"] = [member for chunk in value["chunks"] for member in chunk["members"]]
    return groups


def _fake_exact_group(paths):
    files = tuple(
        SimpleNamespace(
            path=Path(path),
            path_id=index,
            physical_file_id=1000 + index,
            content_version_id=2000 + index,
        )
        for index, path in enumerate(paths, 1)
    )
    return SimpleNamespace(
        size=17,
        full_digest=hashlib.sha256(b"fake exact group").digest(),
        files=files,
        verification_ids=tuple(3000 + index for index in range(1, len(files))),
    )


def _invoke_dispatch(monkeypatch, dispatch, *, command="groups", stdout=None):
    monkeypatch.setattr(catalog_cli, "_dispatch_catalog_command", dispatch)
    output = io.StringIO() if stdout is None else stdout
    stderr = io.StringIO()
    exit_code = run_catalog_command(
        SimpleNamespace(
            catalog_command=command,
            database="catalog.sqlite3",
        ),
        io.StringIO(),
        output,
        stderr,
    )
    return exit_code, output, stderr


def _assert_schema_shape(value, schema):
    if "const" in schema:
        assert value == schema["const"]
    if "enum" in schema:
        assert value in schema["enum"]

    expected_types = schema.get("type")
    if isinstance(expected_types, str):
        expected_types = [expected_types]
    if expected_types:
        matches_type = {
            "null": value is None,
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }
        assert any(matches_type[expected_type] for expected_type in expected_types)

    if value is None:
        return
    if isinstance(value, dict) and (schema.get("type") == "object" or "object" in (schema.get("type") or [])):
        required = set(schema.get("required", ()))
        properties = schema.get("properties", {})
        assert required <= set(value)
        if schema.get("additionalProperties") is False:
            assert set(value) <= set(properties)
        for key, item in value.items():
            if key in properties:
                _assert_schema_shape(item, properties[key])
    if isinstance(value, list) and schema.get("type") == "array":
        if "minItems" in schema:
            assert len(value) >= schema["minItems"]
        if "maxItems" in schema:
            assert len(value) <= schema["maxItems"]
        for item in value:
            _assert_schema_shape(item, schema["items"])
    if isinstance(value, str):
        if "minLength" in schema:
            assert len(value) >= schema["minLength"]
        if "pattern" in schema:
            assert re.fullmatch(schema["pattern"], value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema:
            assert value >= schema["minimum"]
        if "maximum" in schema:
            assert value <= schema["maximum"]


def _assert_all_object_schemas_are_closed(schema):
    if not isinstance(schema, dict):
        return
    schema_types = schema.get("type")
    if schema_types == "object" or (isinstance(schema_types, list) and "object" in schema_types):
        assert schema.get("additionalProperties") is False
        assert set(schema.get("required", ())) == set(schema.get("properties", {}))
    for value in schema.values():
        if isinstance(value, dict):
            _assert_all_object_schemas_are_closed(value)
        elif isinstance(value, list):
            for item in value:
                _assert_all_object_schemas_are_closed(item)


def test_catalog_schemas_are_versioned_draft_2020_12_and_fail_closed():
    assert set(CATALOG_SCHEMAS) == {
        "catalog-result",
        "catalog-status",
        "catalog-group-record",
        "catalog-change-record",
        "catalog-backup",
        "catalog-error",
    }
    for name, schema in CATALOG_SCHEMAS.items():
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        expected_version = {
            "catalog-group-record": CATALOG_GROUP_RECORD_SCHEMA_VERSION,
            "catalog-change-record": CATALOG_CHANGE_RECORD_SCHEMA_VERSION,
        }.get(name, CATALOG_SCHEMA_VERSION)
        assert schema["properties"]["schema_version"]["const"] == expected_version
        _assert_all_object_schemas_are_closed(schema)

    group_safety = CATALOG_SCHEMAS["catalog-group-record"]["properties"]["safety"]
    assert group_safety["properties"]["complete_scan_required"]["const"] is True
    assert group_safety["properties"]["allows_automatic_destructive_action"]["const"] is False
    assert group_safety["properties"]["destructive_workflow"]["const"] == "quarantine_then_explicit_finalize"
    assert CATALOG_MAX_GROUPS * 3 + 2 <= CATALOG_MACHINE_MAX_RECORDS
    assert CATALOG_MAX_CHANGES + 2 == CATALOG_MACHINE_MAX_RECORDS
    assert CATALOG_MACHINE_MAX_LINE_BYTES == 8 * 1024 * 1024
    assert CATALOG_MACHINE_MAX_TOTAL_BYTES == 2 * 1024 * 1024 * 1024
    assert CATALOG_MAX_GROUP_MEMBERS == 1_000_000
    assert CATALOG_GROUP_PAGE_MAX_FILES == CATALOG_MAX_GROUP_MEMBERS
    assert CATALOG_GROUP_CHUNK_MAX_MEMBERS == 40_000
    chunk_schema = CATALOG_SCHEMAS["catalog-group-record"]["properties"]["member_chunk"]
    assert chunk_schema["properties"]["members"]["maxItems"] == CATALOG_GROUP_CHUNK_MAX_MEMBERS
    # Every member contributes one object node and ten scalar tokens. Keep a
    # fixed margin for the record envelope below the 500,000-node limits.
    assert CATALOG_GROUP_CHUNK_MAX_MEMBERS * 11 + 1000 < 500_000
    maximum_group_chunks = (
        CATALOG_MAX_GROUP_MEMBERS + CATALOG_GROUP_CHUNK_MAX_MEMBERS - 1
    ) // CATALOG_GROUP_CHUNK_MAX_MEMBERS
    assert 1 + maximum_group_chunks + 1 < CATALOG_MACHINE_MAX_RECORDS


def test_catalog_scan_cold_warm_rename_and_verified_group_stream(tmp_path):
    first, second = _create_roots(tmp_path)
    duplicate = b"catalog CLI exact duplicate"
    original = first / "original.bin"
    duplicate_path = second / "duplicate.bin"
    original.write_bytes(duplicate)
    duplicate_path.write_bytes(duplicate)
    (second / "unique.bin").write_bytes(b"unique")
    database = tmp_path / "catalog.sqlite3"

    cold_code, cold_records, cold_stderr = _invoke(
        [
            "scan",
            str(database),
            str(first),
            str(second),
            "--max-work-items",
            "10",
            "--batch-size",
            "2",
        ]
    )

    assert cold_code == CatalogExitCode.OK
    assert cold_stderr == ""
    assert len(cold_records) == 1
    cold = cold_records[0]
    _assert_schema_shape(cold, CATALOG_SCHEMAS["catalog-result"])
    assert cold["schema"] == CATALOG_RESULT_SCHEMA
    assert cold["state"] == "complete"
    assert cold["partial"] is False
    assert cold["result"]["changed_content"] == 3
    assert cold["result"]["work_completed"] == 3
    assert cold["result"]["status"]["verified_projection_allowed"] is True
    cold_scan_id = cold["result"]["scan_id"]

    groups_code, group_records, groups_stderr = _invoke(["groups", str(database), "--page-size", "1"])

    assert groups_code == CatalogExitCode.OK
    assert groups_stderr == ""
    assert [record["record_type"] for record in group_records] == [
        "header",
        "group_header",
        "member_chunk",
        "group_end",
        "summary",
    ]
    for record in group_records:
        _assert_schema_shape(
            record,
            CATALOG_SCHEMAS["catalog-group-record"],
        )
        assert record["schema"] == CATALOG_GROUP_RECORD_SCHEMA
        assert record["scan_id"] == cold_scan_id
        assert record["safety"]["allows_automatic_destructive_action"] is False
    groups = _reconstruct_group_records(group_records)
    assert len(groups) == 1
    group = next(iter(groups.values()))
    header = group["header"]
    members = group["members"]
    assert header["sha256"] == hashlib.sha256(duplicate).hexdigest()
    assert {member["path"] for member in members} == {
        str(original),
        str(duplicate_path),
    }
    assert sum(member["verification_id"] is not None for member in members) == 1
    assert members[0]["verification_id"] is None
    assert len({member["content_version_id"] for member in members}) == 2
    assert len({member["physical_file_id"] for member in members}) == 2
    assert len({member["path_id"] for member in members}) == 2
    assert header["verification"] == "verified_exact"
    assert header["safety_state"] == "verified_exact_requires_fresh_action_proof"
    assert header["allows_automatic_destructive_action"] is False
    assert group["end"] == {
        "group_id": header["group_id"],
        "chunk_count": 1,
        "total_members": 2,
        "total_verifications": 1,
        "verification_complete": True,
    }
    assert group_records[-1]["summary"] == {
        "groups": 1,
        "files": 2,
        "member_chunks": 1,
        "page_size": 1,
    }

    warm_code, warm_records, warm_stderr = _invoke(["scan", str(database), str(first), str(second)])

    assert warm_code == CatalogExitCode.OK
    assert warm_stderr == ""
    warm = warm_records[0]
    _assert_schema_shape(warm, CATALOG_SCHEMAS["catalog-result"])
    assert warm["result"]["changed_content"] == 0
    assert warm["result"]["work_enqueued"] == 0
    assert warm["result"]["work_completed"] == 0

    renamed = first / "renamed.bin"
    original.rename(renamed)
    rename_code, rename_records, rename_stderr = _invoke(["scan", str(database), str(first), str(second)])

    assert rename_code == CatalogExitCode.OK
    assert rename_stderr == ""
    rename_result = rename_records[0]
    _assert_schema_shape(rename_result, CATALOG_SCHEMAS["catalog-result"])
    assert rename_result["result"]["changed_content"] == 1
    assert rename_result["result"]["work_enqueued"] == 1
    assert rename_result["result"]["work_completed"] == 1

    renamed_groups_code, renamed_groups, renamed_groups_stderr = _invoke(["groups", str(database), "--page-size", "2"])

    assert renamed_groups_code == CatalogExitCode.OK
    assert renamed_groups_stderr == ""
    renamed_group = next(iter(_reconstruct_group_records(renamed_groups).values()))
    renamed_paths = {member["path"] for member in renamed_group["members"]}
    assert renamed_paths == {str(renamed), str(duplicate_path)}
    assert str(original) not in renamed_paths


def test_read_only_groups_requires_repair_and_scan_recovers_in_one_command(
    tmp_path,
):
    first, second = _create_roots(tmp_path)
    payload = b"catalog CLI repair payload"
    (first / "first.bin").write_bytes(payload)
    (second / "second.bin").write_bytes(payload)
    database = tmp_path / "catalog.sqlite3"
    cold_code, _, _ = _invoke(["scan", str(database), str(first), str(second)])
    assert cold_code == CatalogExitCode.OK

    with Catalog(database) as catalog:
        content_version_ids = tuple(row["content_version_id"] for row in catalog._connection.execute("""
                SELECT artifacts.content_version_id
                FROM artifacts
                WHERE artifacts.kind = 'full_hash'
                    AND artifacts.algorithm = 'sha256'
                ORDER BY artifacts.content_version_id
                """))
        with catalog.transaction():
            catalog._connection.execute(
                """
                UPDATE artifacts
                SET value = ?
                WHERE kind = 'full_hash' AND algorithm = 'sha256'
                """,
                (b"\x7f" * 32,),
            )
            for content_version_id in content_version_ids:
                catalog.put_artifact(
                    content_version_id,
                    "perceptual_hash",
                    "test-phash",
                    "1",
                    b"stale-derived-artifact",
                )

    groups_code, group_records, groups_stderr = _invoke(["groups", str(database)])

    assert groups_code == CatalogExitCode.INPUT_ERROR
    assert len(group_records) == 1
    assert group_records[0]["schema"] == CATALOG_ERROR_SCHEMA
    assert "open the catalog writable and run a repair scan" in group_records[0]["issues"][0]["message"]
    assert "ContentGenerationChanged" in groups_stderr
    with Catalog.open_read_only(database) as unchanged:
        assert (
            unchanged._connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE value = ?",
                (b"stale-derived-artifact",),
            ).fetchone()[0]
            == 2
        )

    repair_code, repair_records, repair_stderr = _invoke(["scan", str(database), str(first), str(second)])

    assert repair_code == CatalogExitCode.OK
    assert repair_stderr == ""
    assert repair_records[0]["result"]["changed_content"] == 2
    assert repair_records[0]["result"]["work_completed"] == 2
    with Catalog.open_read_only(database) as repaired:
        assert (
            repaired._connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE value = ?",
                (b"stale-derived-artifact",),
            ).fetchone()[0]
            == 0
        )
    final_groups_code, final_group_records, final_groups_stderr = _invoke(["groups", str(database)])
    assert final_groups_code == CatalogExitCode.OK
    assert final_groups_stderr == ""
    assert final_group_records[-1]["summary"]["groups"] == 1


def test_catalog_resource_limit_status_resume_and_incomplete_group_gate(tmp_path):
    first, second = _create_roots(tmp_path)
    (first / "one.bin").write_bytes(b"one")
    (second / "two.bin").write_bytes(b"two")
    database = tmp_path / "catalog.sqlite3"

    limited_code, limited_records, limited_stderr = _invoke(
        [
            "scan",
            str(database),
            str(first),
            str(second),
            "--max-work-items",
            "1",
            "--batch-size",
            "1",
        ]
    )

    assert limited_code == CatalogExitCode.RESOURCE_LIMIT
    assert limited_stderr == ""
    limited = limited_records[0]
    _assert_schema_shape(limited, CATALOG_SCHEMAS["catalog-result"])
    assert limited["state"] == "resource_limited"
    assert limited["partial"] is True
    assert limited["limits"]["effective_capacity"] == 1
    assert limited["result"]["catalog_status"] == "running"
    assert limited["result"]["status"]["verified_projection_allowed"] is False
    scan_id = limited["result"]["scan_id"]

    groups_code, groups_records, groups_stderr = _invoke(["groups", str(database)])

    assert groups_code == CatalogExitCode.INPUT_ERROR
    assert len(groups_records) == 1
    assert groups_records[0]["schema"] == CATALOG_ERROR_SCHEMA
    assert groups_records[0]["command"] == "groups"
    assert "no complete scan" in groups_records[0]["issues"][0]["message"]
    assert "ValueError" in groups_stderr
    assert all(record.get("record_type") != "group" for record in groups_records)
    _assert_schema_shape(groups_records[0], CATALOG_SCHEMAS["catalog-error"])

    status_code, status_records, status_stderr = _invoke(["status", str(database), str(scan_id)])

    assert status_code == CatalogExitCode.PARTIAL
    assert status_stderr == ""
    status = status_records[0]
    _assert_schema_shape(status, CATALOG_SCHEMAS["catalog-status"])
    assert status["schema"] == CATALOG_STATUS_SCHEMA
    assert status["state"] == "running"
    assert status["partial"] is True
    assert status["status"]["verified_projection_allowed"] is False

    resume_code, resume_records, resume_stderr = _invoke(["resume", str(database), str(scan_id)])

    assert resume_code == CatalogExitCode.OK
    assert resume_stderr == ""
    resumed = resume_records[0]
    _assert_schema_shape(resumed, CATALOG_SCHEMAS["catalog-result"])
    assert resumed["command"] == "resume"
    assert resumed["state"] == "complete"
    assert resumed["partial"] is False
    assert resumed["result"]["scan_id"] == scan_id
    assert resumed["result"]["status"]["verified_projection_allowed"] is True

    complete_status_code, complete_status_records, complete_status_stderr = _invoke(
        ["status", str(database), str(scan_id)]
    )

    assert complete_status_code == CatalogExitCode.OK
    assert complete_status_stderr == ""
    assert complete_status_records[0]["state"] == "complete"
    assert complete_status_records[0]["status"]["verified_projection_allowed"] is True


def test_catalog_changes_streams_added_modified_relocation_candidate_and_missing(
    tmp_path,
):
    root = tmp_path / "library"
    root.mkdir()
    old_move = root / "old-name.bin"
    modified = root / "modified.bin"
    missing = root / "missing.bin"
    unchanged = root / "unchanged.bin"
    old_move.write_bytes(b"move")
    modified.write_bytes(b"before")
    missing.write_bytes(b"missing")
    unchanged.write_bytes(b"unchanged")
    database = tmp_path / "catalog.sqlite3"

    first_code, first_records, first_stderr = _invoke(["scan", str(database), str(root)])
    assert first_code == CatalogExitCode.OK
    assert first_stderr == ""
    first_scan_id = first_records[0]["result"]["scan_id"]

    new_move = root / "new-name.bin"
    old_move.rename(new_move)
    modified.write_bytes(b"after and changed")
    missing.unlink()
    added = root / "added.bin"
    added.write_bytes(b"added")

    second_code, second_records, second_stderr = _invoke(["scan", str(database), str(root)])
    assert second_code == CatalogExitCode.OK
    assert second_stderr == ""
    second_scan_id = second_records[0]["result"]["scan_id"]

    changes_code, records, changes_stderr = _invoke(
        [
            "changes",
            str(database),
            "--from",
            str(first_scan_id),
            "--to",
            str(second_scan_id),
            "--page-size",
            "1",
        ]
    )

    assert changes_code == CatalogExitCode.OK
    assert changes_stderr == ""
    assert records[0]["record_type"] == "header"
    assert records[-1]["record_type"] == "summary"
    assert len(records) == 6
    for record in records:
        _assert_schema_shape(
            record,
            CATALOG_SCHEMAS["catalog-change-record"],
        )
        assert record["schema"] == CATALOG_CHANGE_RECORD_SCHEMA
        assert record["schema_version"] == CATALOG_CHANGE_RECORD_SCHEMA_VERSION
        assert record["before_scan_id"] == first_scan_id
        assert record["after_scan_id"] == second_scan_id
        assert record["safety"]["verification"] == "immutable-complete-snapshot-diff"
        assert record["safety"]["move_classification"] == "trusted-event-journal-only"
        assert (
            record["safety"]["relocation_candidate_classification"]
            == "stable-native-identity-one-to-one-with-content-continuity"
        )
        assert record["safety"]["allows_automatic_destructive_action"] is False

    changes = {
        record["change"]["change_type"]: record["change"] for record in records if record["record_type"] == "change"
    }
    assert set(changes) == {
        "added",
        "modified",
        "relocation_candidate",
        "missing",
    }
    assert changes["added"]["old"] is None
    assert changes["added"]["new"]["path"] == str(added)
    assert changes["missing"]["old"]["path"] == str(missing)
    assert changes["missing"]["new"] is None
    assert changes["modified"]["old"]["path"] == str(modified)
    assert changes["modified"]["new"]["path"] == str(modified)
    assert changes["modified"]["content_changed"] is True
    assert changes["relocation_candidate"]["old"]["path"] == str(old_move)
    assert changes["relocation_candidate"]["new"]["path"] == str(new_move)
    assert changes["relocation_candidate"]["content_changed"] is False
    assert changes["relocation_candidate"]["move_identity_proven"] is False
    assert (
        changes["relocation_candidate"]["classification"]
        == "stable_native_identity_1_to_1_matching_full_sha256_relocation_candidate"
    )
    assert all(change["allows_automatic_destructive_action"] is False for change in changes.values())
    assert records[-1]["summary"] == {
        "total": 4,
        "added": 1,
        "modified": 1,
        "moved": 0,
        "relocation_candidates": 1,
        "missing": 1,
        "page_size": 1,
    }


def test_change_value_distinguishes_generation_and_hash_relocation_evidence():
    generation = catalog_cli._change_value(
        _fake_relation_row(),
        1,
        2,
    )
    hash_match = catalog_cli._change_value(
        _fake_relation_row(
            relation_evidence="matching_full_sha256",
            new_content_version_id=42,
        ),
        1,
        2,
    )

    assert generation["move_identity_proven"] is False
    assert generation["classification"] == "stable_native_identity_1_to_1_same_catalog_generation_relocation_candidate"
    assert hash_match["move_identity_proven"] is False
    assert hash_match["classification"] == "stable_native_identity_1_to_1_matching_full_sha256_relocation_candidate"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("identity_proven", 1, "move identity proof"),
        ("content_changed", 1, "content continuity"),
        ("new_native_file_id", b"different-file-id", "physical identity"),
        ("relation_evidence", "unknown", "unknown content evidence"),
    ),
)
def test_change_value_rejects_unproven_relocation_candidate(
    field,
    value,
    message,
):
    row = _fake_relation_row()
    row[field] = value

    with pytest.raises(ValueError, match=message):
        catalog_cli._change_value(row, 1, 2)


def test_change_value_reserves_moved_for_trusted_event_journal_evidence():
    row = _fake_relation_row(
        change_type="moved",
        relation_evidence="same_catalog_generation",
        identity_proven=1,
    )

    with pytest.raises(ValueError, match="trusted event-journal"):
        catalog_cli._change_value(row, 1, 2)


def test_run_changes_uses_one_bounded_iterator_without_stateless_pages(
    monkeypatch,
):
    calls = []

    class FakeCatalog:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def scan_roots(self, _scan_id):
            return ({"root_id": 7},)

        def iter_scan_changes(self, *args, **kwargs):
            calls.append((args, kwargs))
            return iter(())

        def page_scan_changes(self, *_args, **_kwargs):
            raise AssertionError("full change streams must not repeat stateless page queries")

    monkeypatch.setattr(
        catalog_cli,
        "_database_path",
        lambda _value, *, must_exist: Path("catalog.sqlite3"),
    )
    monkeypatch.setattr(
        catalog_cli.Catalog,
        "open_read_only",
        lambda _database: FakeCatalog(),
    )
    output = io.StringIO()

    result = catalog_cli._run_changes(
        SimpleNamespace(
            database="catalog.sqlite3",
            before_scan_id=1,
            after_scan_id=2,
            page_size=257,
        ),
        output,
    )

    assert result == CatalogExitCode.OK
    assert len(calls) == 1
    assert calls[0] == (
        (1, 2, (7,)),
        {
            "fetch_size": 257,
            "max_rows": CATALOG_MAX_CHANGES + 1,
        },
    )
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [record["record_type"] for record in records] == ["header", "summary"]
    assert records[-1]["summary"]["total"] == 0


def test_catalog_changes_rejects_incomplete_scan_before_streaming(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    (root / "file.bin").write_bytes(b"file")
    database = tmp_path / "catalog.sqlite3"
    first_code, first_records, _ = _invoke(["scan", str(database), str(root)])
    assert first_code == CatalogExitCode.OK
    first_scan_id = first_records[0]["result"]["scan_id"]

    with CatalogService(database, (root,)) as service:
        running_scan_id = service.start_scan(app_version="test")

    changes_code, records, stderr = _invoke(
        [
            "changes",
            str(database),
            "--from",
            str(first_scan_id),
            "--to",
            str(running_scan_id),
        ]
    )

    assert changes_code == CatalogExitCode.INPUT_ERROR
    assert len(records) == 1
    assert records[0]["schema"] == CATALOG_ERROR_SCHEMA
    assert records[0]["command"] == "changes"
    assert "not complete" in records[0]["issues"][0]["message"]
    assert all(record.get("record_type") != "change" for record in records)
    assert "CatalogStateError" in stderr

    with Catalog(database) as catalog:
        catalog.cancel_scan(running_scan_id)


def test_catalog_cancelled_scan_has_explicit_exit_and_never_projects_groups(
    tmp_path,
):
    first, second = _create_roots(tmp_path)
    (first / "one.bin").write_bytes(b"one")
    database = tmp_path / "catalog.sqlite3"
    with CatalogService(database, (first, second)) as service:
        scan_id = service.start_scan(app_version="test")
        service.catalog.cancel_scan(scan_id)

    status_code, status_records, status_stderr = _invoke(["status", str(database), str(scan_id)])

    assert status_code == CatalogExitCode.CANCELLED
    assert status_stderr == ""
    status = status_records[0]
    _assert_schema_shape(status, CATALOG_SCHEMAS["catalog-status"])
    assert status["state"] == "cancelled"
    assert status["partial"] is True
    assert status["status"]["verified_projection_allowed"] is False

    resume_code, resume_records, resume_stderr = _invoke(["resume", str(database), str(scan_id)])

    assert resume_code == CatalogExitCode.CANCELLED
    assert resume_stderr == ""
    resumed = resume_records[0]
    _assert_schema_shape(resumed, CATALOG_SCHEMAS["catalog-result"])
    assert resumed["state"] == "cancelled"
    assert resumed["partial"] is True
    assert resumed["result"]["status"]["verified_projection_allowed"] is False

    groups_code, groups_records, groups_stderr = _invoke(["groups", str(database)])

    assert groups_code == CatalogExitCode.INPUT_ERROR
    assert groups_records[0]["schema"] == CATALOG_ERROR_SCHEMA
    assert all(record.get("record_type") != "group" for record in groups_records)
    assert groups_stderr


def test_catalog_status_remains_available_when_scanned_root_is_offline(
    tmp_path,
):
    root = tmp_path / "library"
    root.mkdir()
    (root / "file.bin").write_bytes(b"file")
    database = tmp_path / "catalog.sqlite3"
    scan_code, scan_records, scan_stderr = _invoke(["scan", str(database), str(root)])
    assert scan_code == CatalogExitCode.OK
    assert scan_stderr == ""
    scan_id = scan_records[0]["result"]["scan_id"]
    offline = tmp_path / "library-offline"
    root.rename(offline)

    status_code, status_records, status_stderr = _invoke(["status", str(database), str(scan_id)])

    assert status_code == CatalogExitCode.OK
    assert status_stderr == ""
    assert status_records[0]["state"] == "complete"
    assert status_records[0]["status"]["scan_id"] == scan_id
    assert status_records[0]["status"]["verified_projection_allowed"] is True


def test_catalog_groups_reject_newer_incomplete_scan_after_complete_projection(
    tmp_path,
):
    first, second = _create_roots(tmp_path)
    duplicate = b"same"
    (first / "a.bin").write_bytes(duplicate)
    (second / "b.bin").write_bytes(duplicate)
    database = tmp_path / "catalog.sqlite3"
    complete_code, _, _ = _invoke(["scan", str(database), str(first), str(second)])
    assert complete_code == CatalogExitCode.OK

    with CatalogService(database, (first, second)) as service:
        running_scan_id = service.start_scan(app_version="test")

    groups_code, groups_records, groups_stderr = _invoke(["groups", str(database)])

    assert groups_code == CatalogExitCode.INPUT_ERROR
    assert len(groups_records) == 1
    assert groups_records[0]["schema"] == CATALOG_ERROR_SCHEMA
    assert "projectable" in groups_records[0]["issues"][0]["message"]
    assert all(record.get("record_type") != "group" for record in groups_records)
    assert "CatalogStateError" in groups_stderr

    with Catalog(database) as catalog:
        catalog.cancel_scan(running_scan_id)


def test_catalog_backup_is_integrity_checked_and_never_overwrites(tmp_path):
    first, second = _create_roots(tmp_path)
    (first / "one.bin").write_bytes(b"one")
    database = tmp_path / "catalog.sqlite3"
    scan_code, _, _ = _invoke(["scan", str(database), str(first), str(second)])
    assert scan_code == CatalogExitCode.OK
    destination = tmp_path / "catalog-backup.sqlite3"

    backup_code, backup_records, backup_stderr = _invoke(["backup", str(database), str(destination)])

    assert backup_code == CatalogExitCode.OK
    assert backup_stderr == ""
    assert len(backup_records) == 1
    backup = backup_records[0]
    _assert_schema_shape(backup, CATALOG_SCHEMAS["catalog-backup"])
    assert backup["schema"] == CATALOG_BACKUP_SCHEMA
    assert backup["integrity_checked"] is True
    assert backup["overwrote_existing"] is False
    assert backup["destination"] == str(destination)
    with Catalog(destination) as copied:
        assert copied.verify_integrity()
    original_backup = destination.read_bytes()

    repeated_code, repeated_records, repeated_stderr = _invoke(["backup", str(database), str(destination)])

    assert repeated_code == CatalogExitCode.INPUT_ERROR
    assert len(repeated_records) == 1
    assert repeated_records[0]["schema"] == CATALOG_ERROR_SCHEMA
    assert "already exists" in repeated_records[0]["issues"][0]["message"]
    assert "FileExistsError" in repeated_stderr
    assert destination.read_bytes() == original_backup


def test_catalog_read_only_commands_do_not_change_source_or_create_sidecars(
    tmp_path,
):
    root = tmp_path / "library"
    root.mkdir()
    duplicate = b"verified duplicate"
    (root / "first.bin").write_bytes(duplicate)
    (root / "second.bin").write_bytes(duplicate)
    database = tmp_path / "catalog.sqlite3"
    first_code, first_records, _ = _invoke(["scan", str(database), str(root)])
    assert first_code == CatalogExitCode.OK
    first_scan_id = first_records[0]["result"]["scan_id"]
    second_code, second_records, _ = _invoke(["scan", str(database), str(root)])
    assert second_code == CatalogExitCode.OK
    second_scan_id = second_records[0]["result"]["scan_id"]
    original_family = _catalog_family_bytes(database)

    command_arguments = (
        ["status", str(database), str(second_scan_id)],
        ["groups", str(database), "--page-size", "1"],
        [
            "changes",
            str(database),
            "--from",
            str(first_scan_id),
            "--to",
            str(second_scan_id),
            "--page-size",
            "1",
        ],
    )
    for arguments in command_arguments:
        exit_code, records, stderr = _invoke(arguments)
        assert exit_code == CatalogExitCode.OK
        assert records
        assert stderr == ""
        assert _catalog_family_bytes(database) == original_family

    assert catalog_cli._scan_roots(database, second_scan_id) == (root,)
    assert _catalog_family_bytes(database) == original_family

    destination = tmp_path / "backup.sqlite3"
    backup_code, backup_records, backup_stderr = _invoke(["backup", str(database), str(destination)])
    assert backup_code == CatalogExitCode.OK
    assert backup_records[0]["schema"] == CATALOG_BACKUP_SCHEMA
    assert backup_stderr == ""
    assert destination.is_file()
    assert _catalog_family_bytes(database) == original_family


def test_unmarked_legacy_catalog_cli_and_writable_open_both_reject_without_mutation(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    (root / "file.bin").write_bytes(b"file")
    database = tmp_path / "catalog.sqlite3"
    scan_code, scan_records, _ = _invoke(["scan", str(database), str(root)])
    assert scan_code == CatalogExitCode.OK
    scan_id = scan_records[0]["result"]["scan_id"]
    connection = sqlite3.connect(str(database))
    connection.execute("DELETE FROM catalog_meta WHERE key = 'owner'")
    connection.execute("UPDATE catalog_meta SET value = '3' WHERE key = 'schema_version'")
    connection.execute("PRAGMA application_id = 0")
    connection.commit()
    connection.close()
    legacy_family = _catalog_family_bytes(database)

    status_code, status_records, status_stderr = _invoke(["status", str(database), str(scan_id)])

    assert status_code == CatalogExitCode.FAILED
    assert status_records[0]["schema"] == CATALOG_ERROR_SCHEMA
    assert "application_id" in status_records[0]["issues"][0]["message"]
    assert status_stderr
    assert _catalog_family_bytes(database) == legacy_family
    with pytest.raises(CatalogSchemaError, match="application_id"):
        Catalog(database)
    assert _catalog_family_bytes(database) == legacy_family


@pytest.mark.parametrize(
    "arguments",
    (
        ("status", "{database}", "1"),
        ("groups", "{database}"),
        (
            "changes",
            "{database}",
            "--from",
            "1",
            "--to",
            "2",
        ),
        ("backup", "{database}", "{destination}"),
        ("resume", "{database}", "1"),
    ),
)
def test_foreign_catalog_cli_commands_fail_without_mutation(
    tmp_path,
    arguments,
):
    database = tmp_path / "foreign.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    connection = sqlite3.connect(str(database))
    connection.execute("CREATE TABLE catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO catalog_meta(key, value) VALUES ('schema_version', '1')")
    connection.execute("CREATE TABLE sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO sentinel(value) VALUES ('untouched')")
    connection.commit()
    connection.close()
    original_family = _catalog_family_bytes(database)
    concrete_arguments = [value.format(database=database, destination=destination) for value in arguments]

    exit_code, records, stderr = _invoke(concrete_arguments)

    assert exit_code == CatalogExitCode.FAILED
    assert len(records) == 1
    assert records[0]["schema"] == CATALOG_ERROR_SCHEMA
    assert stderr
    assert _catalog_family_bytes(database) == original_family
    assert not destination.exists()
    connection = sqlite3.connect(
        "{}?mode=ro&immutable=1".format(database.as_uri()),
        uri=True,
    )
    sentinel = connection.execute("SELECT value FROM sentinel").fetchone()[0]
    connection.close()
    assert sentinel == "untouched"


@pytest.mark.parametrize("database", [":memory:", r"\\server\share\catalog.sqlite3"])
def test_catalog_rejects_nonlocal_or_nonfilesystem_database_paths(
    tmp_path,
    database,
):
    root = tmp_path / "library"
    root.mkdir()

    exit_code, records, stderr = _invoke(["scan", database, str(root)])

    assert exit_code == CatalogExitCode.INPUT_ERROR
    assert len(records) == 1
    _assert_schema_shape(records[0], CATALOG_SCHEMAS["catalog-error"])
    assert records[0]["schema"] == CATALOG_ERROR_SCHEMA
    assert "catalog" in records[0]["issues"][0]["message"].lower()
    assert stderr


def test_catalog_group_cli_streams_one_million_records_with_backpressure(
    monkeypatch,
):
    write_count = 0
    last_summary = None

    def count_write(_stream, payload):
        nonlocal write_count, last_summary
        write_count += 1
        if payload["record_type"] == "summary":
            last_summary = payload

    shared_record = {
        "group_header": {
            "total_members": 2,
        },
    }

    def count_group_records(_output, _base, _record):
        nonlocal write_count
        write_count += 3
        return 1

    class MillionGroupService:
        requested_page_size = None

        def iter_verified_exact_groups(
            self,
            page_size,
            max_page_files,
            max_group_members,
        ):
            self.requested_page_size = page_size
            assert max_page_files == CATALOG_GROUP_PAGE_MAX_FILES
            assert max_group_members == CATALOG_MAX_GROUP_MEMBERS
            for index in range(1_000_000):
                # The header plus every prior group must be written before the
                # producer advances, proving the CLI does not materialize a list.
                assert write_count == 1 + index * 3
                yield None

    service = MillionGroupService()
    monkeypatch.setattr(catalog_cli, "_write_json_line", count_write)
    monkeypatch.setattr(catalog_cli, "_write_group_records", count_group_records)
    monkeypatch.setattr(
        catalog_cli,
        "_group_value",
        lambda _group, _scan_id: shared_record,
    )

    exit_code = catalog_cli._stream_groups(
        service,
        Path("catalog.sqlite3"),
        17,
        777,
        io.StringIO(),
    )

    assert exit_code == CatalogExitCode.OK
    assert service.requested_page_size == 777
    assert write_count == 3_000_002
    assert last_summary["state"] == "complete"
    assert last_summary["partial"] is False
    assert last_summary["summary"] == {
        "groups": 1_000_000,
        "files": 2_000_000,
        "member_chunks": 1_000_000,
        "page_size": 777,
    }


@pytest.mark.parametrize(
    ("failure", "error_code"),
    (
        (OSError("synthetic page failure"), "o_s_error"),
        (TypeError("synthetic group type failure"), "type_error"),
    ),
)
def test_catalog_group_stream_marks_midstream_failure_partial(
    monkeypatch,
    failure,
    error_code,
):
    class FailingService:
        def iter_verified_exact_groups(
            self,
            page_size,
            max_page_files,
            max_group_members,
        ):
            assert page_size == 3
            assert max_page_files == CATALOG_GROUP_PAGE_MAX_FILES
            assert max_group_members == CATALOG_MAX_GROUP_MEMBERS
            yield _fake_exact_group(("first.bin", "second.bin"))
            raise failure

    def dispatch(_args, output):
        return catalog_cli._stream_groups(
            FailingService(),
            Path("catalog.sqlite3"),
            17,
            3,
            output,
        )

    exit_code, stdout, stderr = _invoke_dispatch(monkeypatch, dispatch)
    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert exit_code == CatalogExitCode.PARTIAL
    assert [record["record_type"] for record in records] == [
        "header",
        "group_header",
        "member_chunk",
        "group_end",
        "summary",
    ]
    assert {record["schema"] for record in records} == {
        CATALOG_GROUP_RECORD_SCHEMA,
    }
    assert records[-1]["state"] == "partial"
    assert records[-1]["partial"] is True
    assert records[-1]["summary"] == {
        "groups": 1,
        "files": 2,
        "member_chunks": 1,
        "page_size": 3,
    }
    assert records[-1]["issues"] == [
        {
            "code": error_code,
            "message": str(failure),
        }
    ]
    assert stderr.getvalue() == ""


def test_catalog_group_chunks_long_paths_into_reconstructable_bounded_lines(
    monkeypatch,
):
    monkeypatch.setattr(catalog_cli, "CATALOG_MACHINE_MAX_LINE_BYTES", 4096)
    paths = tuple("library/{:03d}/{}.jpg".format(index, "x" * 900) for index in range(18))
    group = _fake_exact_group(paths)

    def dispatch(_args, output):
        return catalog_cli._stream_groups(
            SimpleNamespace(iter_verified_exact_groups=lambda **_kwargs: iter((group,))),
            Path("catalog.sqlite3"),
            23,
            5,
            output,
        )

    exit_code, stdout, stderr = _invoke_dispatch(monkeypatch, dispatch)
    raw_lines = stdout.getvalue().splitlines(keepends=True)
    records = [json.loads(line) for line in raw_lines]
    reconstructed = next(iter(_reconstruct_group_records(records).values()))

    assert exit_code == CatalogExitCode.OK
    assert stderr.getvalue() == ""
    assert all(len(line.encode("utf-8")) <= 4096 for line in raw_lines)
    assert len(reconstructed["chunks"]) > 1
    assert [member["path"] for member in reconstructed["members"]] == [str(Path(path)) for path in paths]
    assert [chunk["chunk_index"] for chunk in reconstructed["chunks"]] == list(range(len(reconstructed["chunks"])))
    assert [chunk["first_member_index"] for chunk in reconstructed["chunks"]] == [
        sum(len(previous["members"]) for previous in reconstructed["chunks"][:index])
        for index in range(len(reconstructed["chunks"]))
    ]
    verification_ids = [member["verification_id"] for member in reconstructed["members"]]
    assert verification_ids[0] is None
    assert len([value for value in verification_ids if value is not None]) == len(paths) - 1
    assert reconstructed["end"]["chunk_count"] == len(reconstructed["chunks"])
    assert reconstructed["end"]["total_members"] == len(paths)
    assert reconstructed["end"]["total_verifications"] == len(paths) - 1


def test_catalog_group_chunks_by_structural_member_budget(monkeypatch):
    # A small runtime cap exercises the same count boundary used to divide a
    # contract-valid one-million-member group into 25 structural chunks.
    monkeypatch.setattr(catalog_cli, "CATALOG_GROUP_CHUNK_MAX_MEMBERS", 4)
    paths = tuple("short-{:02d}.bin".format(index) for index in range(10))
    group = _fake_exact_group(paths)

    def dispatch(_args, output):
        return catalog_cli._stream_groups(
            SimpleNamespace(iter_verified_exact_groups=lambda **_kwargs: iter((group,))),
            Path("catalog.sqlite3"),
            24,
            5,
            output,
        )

    exit_code, stdout, stderr = _invoke_dispatch(monkeypatch, dispatch)
    raw_lines = stdout.getvalue().splitlines()
    records = [json.loads(line) for line in raw_lines]
    reconstructed = next(iter(_reconstruct_group_records(records).values()))

    assert exit_code == CatalogExitCode.OK
    assert stderr.getvalue() == ""
    assert [len(chunk["members"]) for chunk in reconstructed["chunks"]] == [
        4,
        4,
        2,
    ]
    assert reconstructed["end"]["chunk_count"] == 3
    assert [member["path"] for member in reconstructed["members"]] == [str(Path(path)) for path in paths]
    for line in raw_lines:
        catalog_cli.preflight_json_structure(
            line,
            limits=catalog_cli._CATALOG_MACHINE_JSON_LIMITS,
            label="catalog member-count chunk test",
        )


def _install_fake_group_service(monkeypatch, projection):
    captured = {
        "iter_called": False,
        "iter_arguments": None,
    }

    class FakeCatalogHandle:
        def close(self):
            pass

    class FakeCatalogService:
        def __init__(
            self,
            database,
            roots,
            *,
            catalog,
            selected_root_ids,
        ):
            captured["database"] = database
            captured["roots"] = roots
            captured["catalog"] = catalog
            captured["selected_root_ids"] = selected_root_ids

        def __enter__(self):
            return self

        def __exit__(self, _error_type, _error, _traceback):
            return False

        def status(self, scan_id):
            assert scan_id == 17
            return SimpleNamespace(
                status="complete",
                verified_projection_allowed=True,
            )

        def verified_exact_projection_counts(self):
            return projection

        def iter_verified_exact_groups(
            self,
            *,
            page_size,
            max_page_files,
            max_group_members,
        ):
            captured["iter_called"] = True
            captured["iter_arguments"] = {
                "page_size": page_size,
                "max_page_files": max_page_files,
                "max_group_members": max_group_members,
            }
            return iter(())

    monkeypatch.setattr(
        catalog_cli,
        "_database_path",
        lambda _value, *, must_exist: Path("catalog.sqlite3"),
    )
    monkeypatch.setattr(
        catalog_cli,
        "_latest_complete_scan_roots",
        lambda _database: (17, (Path("library"),), (3,)),
    )
    monkeypatch.setattr(
        catalog_cli.Catalog,
        "open_read_only",
        staticmethod(lambda _database: FakeCatalogHandle()),
    )
    monkeypatch.setattr(catalog_cli, "CatalogService", FakeCatalogService)
    return captured


def test_catalog_groups_preflights_projection_and_passes_finite_query_caps(
    monkeypatch,
):
    captured = _install_fake_group_service(
        monkeypatch,
        SimpleNamespace(
            group_count=0,
            file_count=0,
            max_group_members=0,
        ),
    )

    def dispatch(_args, output):
        return catalog_cli._run_groups(
            SimpleNamespace(database="catalog.sqlite3", page_size=71),
            output,
        )

    exit_code, stdout, stderr = _invoke_dispatch(monkeypatch, dispatch)
    records = [json.loads(line) for line in stdout.getvalue().splitlines()]

    assert exit_code == CatalogExitCode.OK
    assert stderr.getvalue() == ""
    assert [record["record_type"] for record in records] == [
        "header",
        "summary",
    ]
    assert captured["iter_called"] is True
    assert captured["iter_arguments"] == {
        "page_size": 71,
        "max_page_files": CATALOG_GROUP_PAGE_MAX_FILES,
        "max_group_members": CATALOG_MAX_GROUP_MEMBERS,
    }


@pytest.mark.parametrize(
    "projection",
    (
        SimpleNamespace(
            group_count=CATALOG_MAX_GROUPS + 1,
            file_count=(CATALOG_MAX_GROUPS + 1) * 2,
            max_group_members=2,
        ),
        SimpleNamespace(
            group_count=1,
            file_count=CATALOG_MAX_GROUP_MEMBERS + 1,
            max_group_members=CATALOG_MAX_GROUP_MEMBERS + 1,
        ),
    ),
)
def test_catalog_groups_rejects_oversized_projection_before_materialization(
    monkeypatch,
    projection,
):
    captured = _install_fake_group_service(monkeypatch, projection)

    def dispatch(_args, output):
        return catalog_cli._run_groups(
            SimpleNamespace(database="catalog.sqlite3", page_size=100),
            output,
        )

    exit_code, stdout, stderr = _invoke_dispatch(monkeypatch, dispatch)

    assert exit_code == CatalogExitCode.RESOURCE_LIMIT
    assert stdout.getvalue() == ""
    assert "limit" in stderr.getvalue()
    assert captured["iter_called"] is False


def test_catalog_group_id_binds_each_path_to_its_identifiers():
    first = _fake_exact_group(("first.bin", "second.bin"))
    remapped_files = (
        SimpleNamespace(
            path=first.files[0].path,
            path_id=first.files[0].path_id,
            physical_file_id=first.files[0].physical_file_id,
            content_version_id=first.files[1].content_version_id,
        ),
        SimpleNamespace(
            path=first.files[1].path,
            path_id=first.files[1].path_id,
            physical_file_id=first.files[1].physical_file_id,
            content_version_id=first.files[0].content_version_id,
        ),
    )
    second = SimpleNamespace(
        size=first.size,
        full_digest=first.full_digest,
        files=remapped_files,
        verification_ids=first.verification_ids,
    )
    lexical_alias = SimpleNamespace(
        size=first.size,
        full_digest=first.full_digest,
        files=(
            first.files[0],
            SimpleNamespace(
                path=Path("folder") / ".." / first.files[1].path,
                path_id=first.files[1].path_id,
                physical_file_id=first.files[1].physical_file_id,
                content_version_id=first.files[1].content_version_id,
            ),
        ),
        verification_ids=first.verification_ids,
    )
    assert {file.content_version_id for file in first.files} == {file.content_version_id for file in remapped_files}

    first_id = catalog_cli._group_value(first, 9)["group_header"]["group_id"]
    repeated_id = catalog_cli._group_value(first, 9)["group_header"]["group_id"]
    remapped_id = catalog_cli._group_value(second, 9)["group_header"]["group_id"]
    lexical_alias_id = catalog_cli._group_value(lexical_alias, 9)["group_header"]["group_id"]

    assert first_id == repeated_id
    assert first_id != remapped_id
    assert first_id != lexical_alias_id


def test_catalog_single_oversized_group_member_publishes_nothing(monkeypatch):
    monkeypatch.setattr(catalog_cli, "CATALOG_MACHINE_MAX_LINE_BYTES", 4096)
    group = _fake_exact_group(("first.bin", "x" * 6000))

    def dispatch(_args, output):
        return catalog_cli._stream_groups(
            SimpleNamespace(iter_verified_exact_groups=lambda **_kwargs: iter((group,))),
            Path("catalog.sqlite3"),
            4,
            10,
            output,
        )

    exit_code, stdout, stderr = _invoke_dispatch(monkeypatch, dispatch)

    assert exit_code == CatalogExitCode.RESOURCE_LIMIT
    assert stdout.getvalue() == ""
    assert "_MachineOutputResourceLimit" in stderr.getvalue()


@pytest.mark.parametrize(
    ("constant_name", "limit"),
    (
        ("CATALOG_MACHINE_MAX_RECORDS", 3),
        ("CATALOG_MACHINE_MAX_TOTAL_BYTES", 1),
    ),
)
def test_catalog_machine_record_and_total_limits_publish_nothing(
    monkeypatch,
    constant_name,
    limit,
):
    monkeypatch.setattr(catalog_cli, constant_name, limit)
    group = _fake_exact_group(("first.bin", "second.bin"))

    def dispatch(_args, output):
        return catalog_cli._stream_groups(
            SimpleNamespace(iter_verified_exact_groups=lambda **_kwargs: iter((group,))),
            Path("catalog.sqlite3"),
            8,
            10,
            output,
        )

    exit_code, stdout, stderr = _invoke_dispatch(monkeypatch, dispatch)

    assert exit_code == CatalogExitCode.RESOURCE_LIMIT
    assert stdout.getvalue() == ""
    assert "limit" in stderr.getvalue()


def test_catalog_change_limit_publishes_nothing(monkeypatch):
    monkeypatch.setattr(catalog_cli, "CATALOG_MAX_CHANGES", 2)
    monkeypatch.setattr(
        catalog_cli,
        "_change_value",
        lambda _row, _before, _after: {"change_type": "added"},
    )
    rows = (
        {"sort_root_id": 1, "sort_path_key": "a", "change_type": "added"},
        {"sort_root_id": 1, "sort_path_key": "b", "change_type": "added"},
        {"sort_root_id": 1, "sort_path_key": "c", "change_type": "added"},
    )

    def dispatch(_args, output):
        return catalog_cli._stream_changes(
            SimpleNamespace(),
            Path("catalog.sqlite3"),
            1,
            2,
            (1,),
            3,
            rows,
            output,
        )

    exit_code, stdout, stderr = _invoke_dispatch(
        monkeypatch,
        dispatch,
        command="changes",
    )

    assert exit_code == CatalogExitCode.RESOURCE_LIMIT
    assert stdout.getvalue() == ""
    assert str(CATALOG_MAX_CHANGES) not in stderr.getvalue()
    assert "2 change limit" in stderr.getvalue()


def test_catalog_surrogate_path_publishes_nothing(monkeypatch):
    group = _fake_exact_group(("first.bin", "bad\ud800name.bin"))

    def dispatch(_args, output):
        return catalog_cli._stream_groups(
            SimpleNamespace(iter_verified_exact_groups=lambda **_kwargs: iter((group,))),
            Path("catalog.sqlite3"),
            11,
            10,
            output,
        )

    exit_code, stdout, stderr = _invoke_dispatch(monkeypatch, dispatch)

    assert exit_code == CatalogExitCode.FAILED
    assert stdout.getvalue() == ""
    assert "strict UTF-8" in stderr.getvalue()


def test_catalog_oversized_service_and_error_records_publish_nothing(monkeypatch):
    monkeypatch.setattr(catalog_cli, "CATALOG_MACHINE_MAX_LINE_BYTES", 4096)

    status = CatalogServiceStatus(
        scan_id=1,
        status="complete",
        phase="z" * 6000,
        directory_counts={
            "complete": 1,
            "failed": 0,
            "in_progress": 0,
            "pending": 0,
            "unreachable": 0,
            "total": 1,
        },
        work_counts={
            "complete": 1,
            "failed": 0,
            "in_progress": 0,
            "pending": 0,
            "total": 1,
        },
        error_count=0,
        verified_projection_allowed=True,
        started_at=1.0,
        finished_at=2.0,
    )

    class FakeCatalogContext:
        def __enter__(self):
            return self

        def __exit__(self, _error_type, _error, _traceback):
            return False

    monkeypatch.setattr(
        catalog_cli,
        "_database_path",
        lambda _value, *, must_exist: Path("catalog.sqlite3"),
    )
    monkeypatch.setattr(
        catalog_cli.Catalog,
        "open_read_only",
        staticmethod(lambda _database: FakeCatalogContext()),
    )
    monkeypatch.setattr(
        catalog_cli.CatalogService,
        "status_for_catalog",
        staticmethod(lambda _catalog, _scan_id: status),
    )

    def status_dispatch(_args, output):
        return catalog_cli._run_status(
            SimpleNamespace(database="catalog.sqlite3", scan_id=1),
            output,
        )

    status_code, status_stdout, status_stderr = _invoke_dispatch(
        monkeypatch,
        status_dispatch,
        command="status",
    )
    assert status_code == CatalogExitCode.RESOURCE_LIMIT
    assert status_stdout.getvalue() == ""
    assert "line" in status_stderr.getvalue()

    class OversizedResult:
        catalog_status = "complete"
        outcome = "finished"
        errors = ("x" * 6000,)

        def to_dict(self):
            return {"unreachable": "the line limit is checked before schema publication"}

    def service_dispatch(_args, output):
        return catalog_cli._write_service_result(
            "scan",
            Path("catalog.sqlite3"),
            OversizedResult(),
            {
                "max_work_items": 1,
                "batch_size": 1,
                "max_worker_batches": 1,
                "effective_capacity": 1,
            },
            output,
        )

    service_code, service_stdout, service_stderr = _invoke_dispatch(
        monkeypatch,
        service_dispatch,
        command="scan",
    )
    assert service_code == CatalogExitCode.RESOURCE_LIMIT
    assert service_stdout.getvalue() == ""
    assert "line" in service_stderr.getvalue()

    def error_dispatch(_args, _output):
        raise ValueError("y" * 6000)

    error_code, error_stdout, error_stderr = _invoke_dispatch(
        monkeypatch,
        error_dispatch,
        command="status",
    )
    assert error_code == CatalogExitCode.RESOURCE_LIMIT
    assert error_stdout.getvalue() == ""
    assert "line" in error_stderr.getvalue()


def test_catalog_temp_spool_failure_publishes_nothing(monkeypatch):
    def fail_temporary_file(**_kwargs):
        raise OSError("synthetic temp failure")

    monkeypatch.setattr(catalog_cli.tempfile, "TemporaryFile", fail_temporary_file)
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_catalog_command(
        SimpleNamespace(catalog_command="status", database="catalog.sqlite3"),
        io.StringIO(),
        stdout,
        stderr,
    )

    assert exit_code == CatalogExitCode.FAILED
    assert stdout.getvalue() == ""
    assert "spool" in stderr.getvalue()


def test_catalog_command_error_rolls_back_prior_document_before_error_record(
    monkeypatch,
):
    class EmptyService:
        def iter_verified_exact_groups(
            self,
            page_size,
            max_page_files,
            max_group_members,
        ):
            assert page_size == 2
            assert max_page_files == CATALOG_GROUP_PAGE_MAX_FILES
            assert max_group_members == CATALOG_MAX_GROUP_MEMBERS
            return iter(())

    def dispatch(_args, output):
        assert (
            catalog_cli._stream_groups(
                EmptyService(),
                Path("catalog.sqlite3"),
                31,
                2,
                output,
            )
            == CatalogExitCode.OK
        )
        raise ValueError("failure after a complete group document")

    exit_code, stdout, stderr = _invoke_dispatch(monkeypatch, dispatch)
    records = [json.loads(line) for line in stdout.getvalue().splitlines()]

    assert exit_code == CatalogExitCode.INPUT_ERROR
    assert len(records) == 1
    assert records[0]["schema"] == CATALOG_ERROR_SCHEMA
    assert records[0]["issues"][0]["message"] == "failure after a complete group document"
    assert CATALOG_GROUP_RECORD_SCHEMA not in stdout.getvalue()
    assert "ValueError" in stderr.getvalue()


def test_catalog_empty_normal_error_message_still_publishes_valid_error(monkeypatch):
    def dispatch(_args, _output):
        raise ValueError()

    exit_code, stdout, stderr = _invoke_dispatch(
        monkeypatch,
        dispatch,
        command="status",
    )
    records = [json.loads(line) for line in stdout.getvalue().splitlines()]

    assert exit_code == CatalogExitCode.INPUT_ERROR
    assert len(records) == 1
    assert records[0]["issues"] == [
        {
            "code": "value_error",
            "message": "ValueError",
        }
    ]
    assert "ValueError" in stderr.getvalue()


def test_catalog_non_json_number_is_encoding_failure_with_empty_stdout(
    monkeypatch,
):
    def dispatch(_args, output):
        catalog_cli._write_json_line(output, {"invalid": float("nan")})
        return CatalogExitCode.OK

    exit_code, stdout, stderr = _invoke_dispatch(
        monkeypatch,
        dispatch,
        command="status",
    )

    assert exit_code == CatalogExitCode.FAILED
    assert stdout.getvalue() == ""
    assert "cannot be encoded as JSON" in stderr.getvalue()


def test_catalog_staged_count_validation_prevents_publication(monkeypatch):
    group = _fake_exact_group(("first.bin", "second.bin"))
    real_write_group_records = catalog_cli._write_group_records

    def miscount_group_records(output, base, record):
        return real_write_group_records(output, base, record) + 1

    monkeypatch.setattr(
        catalog_cli,
        "_write_group_records",
        miscount_group_records,
    )

    def dispatch(_args, output):
        return catalog_cli._stream_groups(
            SimpleNamespace(iter_verified_exact_groups=lambda **_kwargs: iter((group,))),
            Path("catalog.sqlite3"),
            44,
            10,
            output,
        )

    exit_code, stdout, stderr = _invoke_dispatch(monkeypatch, dispatch)

    assert exit_code == CatalogExitCode.FAILED
    assert stdout.getvalue() == ""
    assert "summary counts" in stderr.getvalue()


def test_catalog_stdout_publication_failure_is_distinct(monkeypatch):
    class ShortWriter(io.StringIO):
        def write(self, value):
            super().write(value[:5])
            return 5

    def dispatch(_args, output):
        raise ValueError("publish me")

    stdout = ShortWriter()
    exit_code, stdout, stderr = _invoke_dispatch(
        monkeypatch,
        dispatch,
        command="status",
        stdout=stdout,
    )

    assert exit_code == CatalogExitCode.FAILED
    assert stdout.getvalue()
    assert CATALOG_ERROR_SCHEMA not in stdout.getvalue()
    assert "_MachineOutputPublicationError" in stderr.getvalue()


def test_catalog_binary_stdout_preserves_validated_utf8_and_lf(monkeypatch):
    def dispatch(_args, _output):
        raise ValueError("日本語🙂")

    raw_stdout = io.BytesIO()
    text_stdout = io.TextIOWrapper(
        raw_stdout,
        encoding="cp932",
        newline=None,
    )
    exit_code, _stdout, stderr = _invoke_dispatch(
        monkeypatch,
        dispatch,
        command="status",
        stdout=text_stdout,
    )
    published = raw_stdout.getvalue()
    payload = json.loads(published.decode("utf-8"))
    expected = catalog_cli._encode_machine_json_line(payload)

    assert exit_code == CatalogExitCode.INPUT_ERROR
    assert published == expected
    assert published.endswith(b"\n")
    assert b"\r\n" not in published
    assert "日本語🙂".encode("utf-8") in published
    assert len(published) <= CATALOG_MACHINE_MAX_LINE_BYTES
    assert len(published) <= CATALOG_MACHINE_MAX_TOTAL_BYTES
    assert stderr.getvalue()


def test_catalog_binary_stdout_short_write_is_publication_failure(monkeypatch):
    class ShortBinaryWriter(io.BytesIO):
        def write(self, value):
            super().write(value[:5])
            return 5

    class BinaryStdout:
        def __init__(self):
            self.buffer = ShortBinaryWriter()

        def flush(self):
            pass

    def dispatch(_args, _output):
        raise ValueError("publish me as bytes")

    stdout = BinaryStdout()
    exit_code, _stdout, stderr = _invoke_dispatch(
        monkeypatch,
        dispatch,
        command="status",
        stdout=stdout,
    )

    assert exit_code == CatalogExitCode.FAILED
    assert len(stdout.buffer.getvalue()) == 5
    assert "short binary write" in stderr.getvalue()
