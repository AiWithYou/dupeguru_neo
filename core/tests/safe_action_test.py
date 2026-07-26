import errno
import inspect
import json
import os
import threading
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from core import safe_action
from hscommon import atomic_rename
from core.safe_action import (
    ActionState,
    AppendOnlyJournal,
    FailureCode,
    JournalEventType,
    OperationPlan,
    PosixFileSystemAdapter,
    SafeActionExecutor,
    build_operation_plan,
    platform_file_system,
)

PAYLOAD = (b"verified duplicate payload\n" * 4096) + b"end"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound cleanup contract")
def test_created_file_cleanup_never_unlinks_a_racing_name_replacement(tmp_path, monkeypatch):
    created = tmp_path / "created.tmp"
    replacement = tmp_path / "replacement.tmp"
    stolen = tmp_path / "stolen-created.tmp"
    created.write_bytes(b"owned temporary")
    replacement.write_bytes(b"external replacement")
    created_stat = os.lstat(created)
    created_identity = (int(created_stat.st_dev), int(created_stat.st_ino))
    replacement_blocked = False
    real_disposition = atomic_rename._set_windows_delete_disposition

    def race_before_disposition(descriptor, path):
        nonlocal replacement_blocked
        try:
            os.replace(created, stolen)
        except OSError:
            replacement_blocked = True
        else:
            os.replace(replacement, created)
        return real_disposition(descriptor, path)

    monkeypatch.setattr(
        atomic_rename,
        "_set_windows_delete_disposition",
        race_before_disposition,
    )

    removed = safe_action.cleanup_created_regular_file(
        created,
        created_identity,
        platform_file_system(),
    )

    assert removed
    assert replacement_blocked
    assert not created.exists()
    assert replacement.read_bytes() == b"external replacement"
    assert not stolen.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound cleanup contract")
def test_created_file_cleanup_has_no_path_unlink_fallback(tmp_path, monkeypatch):
    created = tmp_path / "created.tmp"
    created.write_bytes(b"owned temporary")
    created_stat = os.lstat(created)
    created_identity = (int(created_stat.st_dev), int(created_stat.st_ino))

    def unsupported(_descriptor, _path):
        raise OSError(errno.ENOTSUP, "simulated unsupported disposition API")

    monkeypatch.setattr(
        atomic_rename,
        "_set_windows_delete_disposition",
        unsupported,
    )

    removed = safe_action.cleanup_created_regular_file(
        created,
        created_identity,
        platform_file_system(),
    )

    assert not removed
    assert created.read_bytes() == b"owned temporary"


def test_opened_proof_rejects_generation_change_with_stable_stat(tmpdir, monkeypatch):
    path = Path(str(tmpdir)).joinpath("payload")
    path.write_bytes(PAYLOAD)
    with platform_file_system().open_readonly(path) as handle:
        before = os.fstat(handle.fileno())
        generation_before = safe_action.get_file_generation_token_from_fd(
            handle.fileno(),
            stat_result=before,
        )
        changed_generation = safe_action.FileGenerationToken(
            generation_before.namespace,
            generation_before.value + 1,
            generation_before.version,
        )
        monkeypatch.setattr(
            safe_action,
            "get_file_generation_token_from_fd",
            lambda *_args, **_kwargs: changed_generation,
        )

        with pytest.raises(safe_action._SafetyFailure) as caught:
            safe_action._opened_proof(
                handle,
                "0" * 64,
                before,
                before,
                generation_before,
            )

    assert caught.value.code is FailureCode.UNSTABLE_CONTENT


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific destructive identity contract")
def test_plan_rejects_legacy_windows_file_index_identity(tmpdir, monkeypatch):
    base, allowed, quarantine, target, keeper, _result = make_environment(tmpdir)
    legacy = safe_action.PhysicalFileIdentity(
        namespace="windows",
        volume_id=1,
        file_id=2,
        capability=safe_action.IdentityCapability.WINDOWS_FILE_INDEX_64,
        confidence=safe_action.IdentityConfidence.MEDIUM,
    )
    monkeypatch.setattr(safe_action, "get_file_identity_from_fd", lambda *_args, **_kwargs: legacy)

    result = build_operation_plan(target, keeper, [allowed], quarantine)

    assert not result.ok
    assert result.failure.code is FailureCode.IDENTITY_UNAVAILABLE


@pytest.mark.parametrize(
    "identity",
    (
        {
            "namespace": "other",
            "capability": "posix-device-inode",
            "confidence": 3,
            "volume_id": 1,
            "file_id": "2",
        },
        {
            "namespace": "windows",
            "capability": "windows-file-id-128",
            "confidence": 3,
            "volume_id": 0,
            "file_id": "01" + ("00" * 15),
        },
    ),
)
def test_plan_identity_schema_rejects_foreign_namespace_or_zero_windows_volume(identity):
    with pytest.raises(ValueError):
        safe_action.FileIdentity.from_dict(identity)


@pytest.mark.skipif(os.name != "posix", reason="POSIX native rename primitive")
def test_posix_no_replace_move_never_overwrites_destination(tmpdir):
    base = Path(str(tmpdir))
    source = base.joinpath("source")
    destination = base.joinpath("destination")
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")

    with pytest.raises(FileExistsError):
        PosixFileSystemAdapter().rename_no_replace(source, destination)

    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"destination"


@pytest.mark.skipif(os.name != "posix", reason="POSIX native rename primitive")
def test_posix_no_replace_move_has_one_atomic_winner_under_race(tmpdir):
    base = Path(str(tmpdir))
    sources = [base.joinpath("source-a"), base.joinpath("source-b")]
    payloads = [b"source-a", b"source-b"]
    for source, payload in zip(sources, payloads):
        source.write_bytes(payload)
    destination = base.joinpath("destination")
    barrier = threading.Barrier(2)
    outcomes = []

    def move(index):
        barrier.wait()
        try:
            PosixFileSystemAdapter().rename_no_replace(
                sources[index],
                destination,
            )
        except FileExistsError:
            outcomes.append(("lost", index))
        else:
            outcomes.append(("won", index))

    threads = [
        threading.Thread(target=move, args=(0,)),
        threading.Thread(target=move, args=(1,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    [winner] = [index for outcome, index in outcomes if outcome == "won"]
    [loser] = [index for outcome, index in outcomes if outcome == "lost"]
    assert destination.read_bytes() == payloads[winner]
    assert not sources[winner].exists()
    assert sources[loser].read_bytes() == payloads[loser]


@pytest.mark.skipif(os.name != "posix", reason="POSIX native rename dispatch")
def test_posix_no_replace_rejects_unsupported_platform_without_fallback(
    tmpdir,
    monkeypatch,
):
    base = Path(str(tmpdir))
    source = base.joinpath("source")
    destination = base.joinpath("destination")
    source.write_bytes(b"source")
    monkeypatch.setattr(atomic_rename.sys, "platform", "freebsd-test")

    with pytest.raises(OSError) as raised:
        PosixFileSystemAdapter().rename_no_replace(source, destination)

    assert raised.value.errno == errno.ENOTSUP
    assert source.read_bytes() == b"source"
    assert not destination.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX native rename dispatch")
def test_posix_no_replace_dispatch_has_no_link_unlink_emulation(
    tmpdir,
    monkeypatch,
):
    base = Path(str(tmpdir))
    source = base.joinpath("source")
    destination = base.joinpath("destination")
    source.write_bytes(b"source")
    adapter = PosixFileSystemAdapter()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("link/unlink emulation must not be used")

    monkeypatch.setattr(os, "link", forbidden)
    monkeypatch.setattr(os, "unlink", forbidden)

    adapter.rename_no_replace(source, destination)

    assert not source.exists()
    assert destination.read_bytes() == b"source"


def test_verified_rename_requires_a_live_proof_handle(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"verified source")
    file_system = platform_file_system()
    with file_system.open_readonly(source) as verified_handle:
        pass

    with pytest.raises(ValueError, match="closed file"):
        file_system.rename_no_replace_verified(
            source,
            destination,
            verified_handle,
        )

    assert source.read_bytes() == b"verified source"
    assert not destination.exists()


def test_unverified_rename_error_preserves_the_committed_result(tmp_path):
    adapter_base = type(platform_file_system())

    class UnverifiedCommitFileSystem(adapter_base):
        def rename_no_replace_bound(self, *args, **kwargs):
            commit = super().rename_no_replace_bound(*args, **kwargs)
            return replace(
                commit,
                postcondition_verified=False,
                verification_error="injected postcondition failure",
            )

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"verified source")
    file_system = UnverifiedCommitFileSystem()

    with file_system.open_readonly(source) as verified_handle:
        with pytest.raises(safe_action.UnverifiedRenameCommitError) as caught:
            file_system.rename_no_replace_verified(
                source,
                destination,
                verified_handle,
            )

    assert caught.value.commit.postcondition_verified is False
    assert caught.value.destination == destination
    assert not source.exists()
    assert destination.read_bytes() == b"verified source"


@pytest.mark.skipif(os.name != "nt", reason="Windows preopened rename capability")
def test_windows_verified_rename_rejects_a_dropped_preopened_capability(tmp_path):
    adapter_base = type(platform_file_system())

    class DroppedCapabilityFileSystem(adapter_base):
        def rename_no_replace_bound(self, *args, **kwargs):
            commit = super().rename_no_replace_bound(*args, **kwargs)
            return replace(commit, preopened_source_used=False)

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"verified source")
    file_system = DroppedCapabilityFileSystem()

    with file_system.open_readonly(source) as verified_handle:
        with pytest.raises(safe_action.UnverifiedRenameCommitError) as caught:
            file_system.rename_no_replace_verified(
                source,
                destination,
                verified_handle,
            )

    assert "preopened source capability" in caught.value.reason
    assert not source.exists()
    assert destination.read_bytes() == b"verified source"


@pytest.mark.skipif(os.name != "nt", reason="Windows preopened rename lease")
def test_windows_rename_commit_survives_preopened_lease_close_failure(
    tmp_path,
    monkeypatch,
    caplog,
):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"verified source")
    file_system = platform_file_system()
    tracked = {"descriptor": None}
    real_open = atomic_rename._open_windows_preverified_source
    real_close = atomic_rename.os.close

    def tracking_open(path):
        descriptor = real_open(path)
        tracked["descriptor"] = descriptor
        return descriptor

    def close_then_report_failure(descriptor):
        result = real_close(descriptor)
        if descriptor == tracked["descriptor"]:
            raise OSError(errno.EIO, "injected rename lease close failure")
        return result

    monkeypatch.setattr(
        atomic_rename,
        "_open_windows_preverified_source",
        tracking_open,
    )
    monkeypatch.setattr(atomic_rename.os, "close", close_then_report_failure)
    caplog.set_level("WARNING")

    with file_system.open_readonly(source) as verified_handle:
        commit = file_system.rename_no_replace_verified(
            source,
            destination,
            verified_handle,
        )

    assert commit.postcondition_verified
    assert commit.preopened_source_used
    assert not source.exists()
    assert destination.read_bytes() == b"verified source"
    assert "preverified rename-source lease" in caplog.text
    assert "preserving the already-determined operation outcome" in caplog.text


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound delete lease")
def test_windows_delete_commit_survives_disposition_lease_close_failure(
    tmp_path,
    monkeypatch,
    caplog,
):
    target = tmp_path / "target"
    target.write_bytes(b"verified target")
    target_stat = os.lstat(target)
    tracked = {"descriptor": None}
    real_disposition = atomic_rename._set_windows_delete_disposition
    real_close = atomic_rename.os.close

    def tracking_disposition(descriptor, path):
        tracked["descriptor"] = descriptor
        return real_disposition(descriptor, path)

    def close_then_report_failure(descriptor):
        result = real_close(descriptor)
        if descriptor == tracked["descriptor"]:
            raise OSError(errno.EIO, "injected disposition lease close failure")
        return result

    monkeypatch.setattr(
        atomic_rename,
        "_set_windows_delete_disposition",
        tracking_disposition,
    )
    monkeypatch.setattr(atomic_rename.os, "close", close_then_report_failure)
    caplog.set_level("WARNING")

    removed = atomic_rename.delete_tracked_windows_entry(
        target,
        (int(target_stat.st_dev), int(target_stat.st_ino)),
        int(target_stat.st_mode),
    )

    assert removed
    assert not target.exists()
    assert "verified delete-disposition lease" in caplog.text
    assert "preserving the already-determined operation outcome" in caplog.text


def test_safety_critical_executors_do_not_call_weak_namespace_mutators():
    from core.dataset_executor import DatasetBundleExecutor

    for executor_class in (SafeActionExecutor, DatasetBundleExecutor):
        source = inspect.getsource(executor_class)
        assert ".rename_no_replace(" not in source
        assert ".unlink(" not in source


def test_oversized_journal_fails_closed_before_target_moves(
    tmpdir,
    monkeypatch,
):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal_path = base.joinpath("actions.jsonl")
    journal_path.write_bytes(b"x" * 129)
    monkeypatch.setattr(safe_action, "MAX_JOURNAL_BYTES", 128)
    executor = SafeActionExecutor(AppendOnlyJournal(journal_path))

    action = executor.stage(plan)

    assert not action.ok
    assert action.code is FailureCode.JOURNAL_CORRUPT
    assert target.read_bytes() == PAYLOAD
    assert keeper.read_bytes() == PAYLOAD
    assert journal_path.read_bytes() == b"x" * 129
    assert not plan.quarantine_path.exists()


def test_overlong_journal_record_fails_closed_before_target_moves(
    tmpdir,
    monkeypatch,
):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal_path = base.joinpath("actions.jsonl")
    journal_path.write_bytes((b"x" * 129) + b"\n")
    monkeypatch.setattr(safe_action, "MAX_JOURNAL_BYTES", 4096)
    monkeypatch.setattr(safe_action, "MAX_JOURNAL_LINE_BYTES", 128)
    executor = SafeActionExecutor(AppendOnlyJournal(journal_path))

    action = executor.stage(plan)

    assert not action.ok
    assert action.code is FailureCode.JOURNAL_CORRUPT
    assert target.read_bytes() == PAYLOAD
    assert keeper.read_bytes() == PAYLOAD
    assert journal_path.read_bytes() == (b"x" * 129) + b"\n"
    assert not plan.quarantine_path.exists()


@pytest.mark.parametrize(
    "malformed_record",
    (
        b'{"schema_version":1,"schema_version":1}\n',
        b'{"value":NaN}\n',
        b'{"value":1e9999}\n',
        ('{"value":' + str(1 << 300) + "}\n").encode("ascii"),
    ),
    ids=("duplicate-key", "nan", "overflowing-float", "huge-integer"),
)
def test_ambiguous_journal_json_fails_closed_before_target_moves(
    tmpdir,
    malformed_record,
):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal_path = base.joinpath("actions.jsonl")
    journal_path.write_bytes(malformed_record)
    executor = SafeActionExecutor(AppendOnlyJournal(journal_path))

    action = executor.stage(plan)

    assert not action.ok
    assert action.code is FailureCode.JOURNAL_CORRUPT
    assert target.read_bytes() == PAYLOAD
    assert keeper.read_bytes() == PAYLOAD
    assert journal_path.read_bytes() == malformed_record
    assert not plan.quarantine_path.exists()


def test_journal_event_limit_is_enforced_without_whole_file_read(
    tmpdir,
    monkeypatch,
):
    base, _, _, _, _, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal = AppendOnlyJournal(base.joinpath("actions.jsonl"))
    journal.append(plan, JournalEventType.PREPARED)
    monkeypatch.setattr(safe_action, "MAX_JOURNAL_EVENTS", 0)

    with pytest.raises(safe_action.JournalError, match="event safety limit"):
        journal.read()


def test_journal_reader_streams_without_path_read_bytes(
    tmpdir,
    monkeypatch,
):
    base, _, _, _, _, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal = AppendOnlyJournal(base.joinpath("actions.jsonl"))
    journal.append(plan, JournalEventType.PREPARED)

    def forbidden_read_bytes(_path):
        raise AssertionError("journal replay must stream")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)

    [event] = journal.read()
    assert event.event is JournalEventType.PREPARED


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlinks are unavailable")
def test_hardlinked_journal_fails_closed_without_moving_target(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal_path = base.joinpath("actions.jsonl")
    journal = AppendOnlyJournal(journal_path)
    journal.append(plan, JournalEventType.PREPARED)
    alias = base.joinpath("journal-alias.jsonl")
    try:
        os.link(journal_path, alias)
    except OSError as error:
        pytest.skip("hardlinks are unavailable: {}".format(error))
    executor = SafeActionExecutor(journal)

    action = executor.stage(plan)

    assert not action.ok
    assert action.code is FailureCode.JOURNAL_CORRUPT
    assert target.read_bytes() == PAYLOAD
    assert keeper.read_bytes() == PAYLOAD
    assert not plan.quarantine_path.exists()


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlinks are unavailable")
def test_journal_append_rejects_hardlink_added_after_stable_replay(
    tmpdir,
    monkeypatch,
):
    base, _, _, _, _, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal_path = base.joinpath("actions.jsonl")
    journal = AppendOnlyJournal(journal_path)
    journal.append(plan, JournalEventType.PREPARED)
    before = journal_path.read_bytes()
    alias = base.joinpath("journal-race-alias.jsonl")
    original_read = journal._read_locked
    raced = False

    def add_alias_after_replay(*args, **kwargs):
        nonlocal raced
        replay = original_read(*args, **kwargs)
        if not raced:
            os.link(journal_path, alias)
            raced = True
        return replay

    monkeypatch.setattr(journal, "_read_locked", add_alias_after_replay)

    with pytest.raises(safe_action.JournalError, match="Journal path or size changed"):
        journal.append(plan, JournalEventType.FAILED)

    assert raced
    assert journal_path.read_bytes() == before
    assert alias.read_bytes() == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner/mode policy")
def test_world_writable_quarantine_root_is_rejected_without_moving_target(
    tmpdir,
):
    base, _, quarantine, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    quarantine.chmod(0o777)
    executor = SafeActionExecutor(AppendOnlyJournal(base.joinpath("actions.jsonl")))
    try:
        action = executor.stage(plan)
    finally:
        quarantine.chmod(0o700)

    assert not action.ok
    assert action.code is FailureCode.INVALID_PLAN
    assert target.read_bytes() == PAYLOAD
    assert keeper.read_bytes() == PAYLOAD
    assert not plan.quarantine_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner/mode policy")
def test_world_writable_journal_parent_is_rejected_without_moving_target(
    tmpdir,
):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    base.chmod(0o777)
    executor = SafeActionExecutor(AppendOnlyJournal(base.joinpath("actions.jsonl")))
    try:
        action = executor.stage(plan)
    finally:
        base.chmod(0o700)

    assert not action.ok
    assert action.code is FailureCode.JOURNAL_CORRUPT
    assert target.read_bytes() == PAYLOAD
    assert keeper.read_bytes() == PAYLOAD
    assert not plan.quarantine_path.exists()


class SimulatedCrash(BaseException):
    pass


class CrashBeforeEventJournal(AppendOnlyJournal):
    def __init__(self, path, event):
        super().__init__(path)
        self.event = event

    def append(self, plan, event, details=None):
        if event is self.event:
            raise SimulatedCrash()
        return super().append(plan, event, details)


def make_environment(tmpdir, target_bytes=PAYLOAD, keeper_bytes=PAYLOAD, plan_id=None):
    base = Path(str(tmpdir))
    library = base.joinpath("library")
    quarantine = base.joinpath("quarantine")
    library.mkdir()
    quarantine.mkdir()
    target = library.joinpath("target.bin")
    keeper = library.joinpath("keeper.bin")
    target.write_bytes(target_bytes)
    keeper.write_bytes(keeper_bytes)
    result = build_operation_plan(
        target=target,
        keeper=keeper,
        allowed_roots=[library],
        quarantine_root=quarantine,
        plan_id=plan_id,
    )
    return base, library, quarantine, target, keeper, result


def make_executor(base):
    journal = AppendOnlyJournal(base.joinpath("actions.jsonl"))
    return journal, SafeActionExecutor(journal)


def assert_plan(result):
    assert result.ok, result.failure
    assert result.plan is not None
    return result.plan


def test_build_plan_binds_equal_regular_files(tmpdir):
    _, library, quarantine, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)

    assert plan.target.path == str(target)
    assert plan.keeper.path == str(keeper)
    assert plan.target.digest_hex == plan.keeper.digest_hex
    assert plan.target.identity != plan.keeper.identity
    assert plan.allowed_roots == (str(library.resolve()),)
    assert plan.quarantine_root == str(quarantine.resolve())


def test_plan_serialization_is_strict_and_stable(tmpdir):
    _, _, _, _, _, result = make_environment(tmpdir)
    plan = assert_plan(result)

    restored = OperationPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
    assert restored == plan
    assert restored.fingerprint == plan.fingerprint

    invalid = plan.to_dict()
    invalid["unexpected"] = True
    with pytest.raises(ValueError):
        OperationPlan.from_dict(invalid)


def test_plan_rejects_different_content(tmpdir):
    _, _, _, target, keeper, result = make_environment(tmpdir, keeper_bytes=b"x" * len(PAYLOAD))

    assert not result.ok
    assert result.failure.code is FailureCode.CONTENT_MISMATCH
    assert target.exists()
    assert keeper.exists()


def test_plan_rejects_target_outside_allowed_roots(tmpdir):
    base = Path(str(tmpdir))
    allowed = base.joinpath("allowed")
    outside = base.joinpath("outside")
    quarantine = base.joinpath("quarantine")
    allowed.mkdir()
    outside.mkdir()
    quarantine.mkdir()
    target = outside.joinpath("target")
    keeper = allowed.joinpath("keeper")
    target.write_bytes(PAYLOAD)
    keeper.write_bytes(PAYLOAD)

    result = build_operation_plan(target, keeper, [allowed], quarantine)

    assert not result.ok
    assert result.failure.code is FailureCode.PATH_OUTSIDE_ALLOWED_ROOTS
    assert target.exists()


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlinks are unavailable")
def test_plan_rejects_same_underlying_file(tmpdir):
    base = Path(str(tmpdir))
    library = base.joinpath("library")
    quarantine = base.joinpath("quarantine")
    library.mkdir()
    quarantine.mkdir()
    target = library.joinpath("target")
    keeper = library.joinpath("keeper")
    target.write_bytes(PAYLOAD)
    os.link(str(target), str(keeper))

    result = build_operation_plan(target, keeper, [library], quarantine)

    assert not result.ok
    assert result.failure.code is FailureCode.SAME_IDENTITY
    assert target.exists()
    assert keeper.exists()


def test_stage_moves_only_revalidated_target(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal, executor = make_executor(base)

    action = executor.stage(plan)

    assert action.ok
    assert action.state is ActionState.STAGED
    assert action.changed
    assert not target.exists()
    assert keeper.read_bytes() == PAYLOAD
    assert plan.quarantine_path.read_bytes() == PAYLOAD
    assert [event.event for event in journal.events_for(plan.plan_id)] == [
        JournalEventType.PREPARED,
        JournalEventType.STAGED,
    ]


def test_stage_handles_files_whose_write_and_creation_times_differ(tmpdir):
    base = Path(str(tmpdir))
    library = base.joinpath("library")
    quarantine = base.joinpath("quarantine")
    library.mkdir()
    quarantine.mkdir()
    target = library.joinpath("target.bin")
    keeper = library.joinpath("keeper.bin")
    target.write_bytes(PAYLOAD)
    keeper.write_bytes(PAYLOAD)
    older_ns = 1_600_000_000_000_000_000
    os.utime(target, ns=(older_ns, older_ns))
    os.utime(keeper, ns=(older_ns, older_ns))
    plan = assert_plan(build_operation_plan(target, keeper, [library], quarantine))
    _, executor = make_executor(base)

    action = executor.stage(plan)

    assert action.ok
    assert not target.exists()
    assert plan.quarantine_path.read_bytes() == PAYLOAD


def test_stage_rejects_same_path_replacement_even_when_size_matches(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    replacement = target.with_name("replacement")
    replacement_bytes = b"z" * len(PAYLOAD)
    replacement.write_bytes(replacement_bytes)
    os.replace(str(replacement), str(target))
    _, executor = make_executor(base)

    action = executor.stage(plan)

    assert not action.ok
    assert action.code is FailureCode.IDENTITY_MISMATCH
    assert action.state is ActionState.FAILED
    assert target.read_bytes() == replacement_bytes
    assert keeper.read_bytes() == PAYLOAD
    assert not plan.quarantine_path.exists()


def test_stage_rejects_in_place_content_change(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    target.write_bytes(b"q" * len(PAYLOAD))
    _, executor = make_executor(base)

    action = executor.stage(plan)

    assert not action.ok
    assert action.code in {FailureCode.METADATA_MISMATCH, FailureCode.CONTENT_MISMATCH}
    assert target.exists()
    assert keeper.exists()
    assert not plan.quarantine_path.exists()


def test_stage_rejects_missing_keeper_without_touching_target(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    keeper.unlink()
    _, executor = make_executor(base)

    action = executor.stage(plan)

    assert not action.ok
    assert action.code is FailureCode.MISSING_KEEPER
    assert target.read_bytes() == PAYLOAD
    assert not plan.quarantine_path.exists()


def test_stage_rejects_file_replaced_by_directory(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    target.unlink()
    target.mkdir()
    sentinel = target.joinpath("sentinel")
    sentinel.write_bytes(b"must survive")
    _, executor = make_executor(base)

    action = executor.stage(plan)

    assert not action.ok
    assert action.code is FailureCode.TYPE_MISMATCH
    assert sentinel.read_bytes() == b"must survive"
    assert keeper.exists()
    assert not plan.quarantine_path.exists()


def test_stage_replays_after_crash_between_rename_and_staged_record(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal_path = base.joinpath("actions.jsonl")
    crashing = SafeActionExecutor(CrashBeforeEventJournal(journal_path, JournalEventType.STAGED))

    with pytest.raises(SimulatedCrash):
        crashing.stage(plan)

    assert not target.exists()
    assert plan.quarantine_path.read_bytes() == PAYLOAD
    assert keeper.exists()

    journal = AppendOnlyJournal(journal_path)
    replay = SafeActionExecutor(journal).stage(plan)
    assert replay.ok
    assert replay.state is ActionState.STAGED
    assert not replay.changed
    assert JournalEventType.STAGED_RECOVERED in [event.event for event in journal.events_for(plan.plan_id)]


def test_restore_is_idempotent(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    _, executor = make_executor(base)
    assert executor.stage(plan).ok

    first = executor.restore(plan)
    second = executor.restore(plan)

    assert first.ok
    assert first.state is ActionState.RESTORED
    assert first.changed
    assert second.ok
    assert second.state is ActionState.RESTORED
    assert not second.changed
    assert target.read_bytes() == PAYLOAD
    assert keeper.read_bytes() == PAYLOAD
    assert not plan.quarantine_path.exists()


def test_restore_replays_after_crash_between_rename_and_restored_record(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal_path = base.joinpath("actions.jsonl")
    normal = SafeActionExecutor(AppendOnlyJournal(journal_path))
    assert normal.stage(plan).ok
    crashing = SafeActionExecutor(
        CrashBeforeEventJournal(
            journal_path,
            JournalEventType.RESTORED,
        )
    )

    with pytest.raises(SimulatedCrash):
        crashing.restore(plan)

    assert target.read_bytes() == PAYLOAD
    assert not plan.quarantine_path.exists()

    journal = AppendOnlyJournal(journal_path)
    replay = SafeActionExecutor(journal).restore(plan)

    assert replay.ok
    assert replay.state is ActionState.RESTORED
    assert not replay.changed
    assert JournalEventType.RESTORED_RECOVERED in {event.event for event in journal.events_for(plan.plan_id)}


def test_restore_never_overwrites_recreated_target(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    _, executor = make_executor(base)
    assert executor.stage(plan).ok
    target.write_bytes(b"new unrelated file")

    action = executor.restore(plan)

    assert not action.ok
    assert action.code is FailureCode.TARGET_CONFLICT
    assert target.read_bytes() == b"new unrelated file"
    assert plan.quarantine_path.read_bytes() == PAYLOAD
    assert keeper.exists()


def test_restore_rejects_tampered_staged_payload(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    _, executor = make_executor(base)
    assert executor.stage(plan).ok
    plan.quarantine_path.write_bytes(b"tampered" + PAYLOAD)

    action = executor.restore(plan)

    assert not action.ok
    assert action.code in {FailureCode.METADATA_MISMATCH, FailureCode.CONTENT_MISMATCH}
    assert not target.exists()
    assert plan.quarantine_path.exists()
    assert keeper.exists()


def test_finalize_is_idempotent_and_keeps_verified_keeper(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    _, executor = make_executor(base)
    assert executor.stage(plan).ok

    first = executor.finalize(plan)
    second = executor.finalize(plan)

    assert first.ok
    assert first.state is ActionState.FINALIZED
    assert first.changed
    assert second.ok
    assert second.state is ActionState.FINALIZED
    assert not second.changed
    assert not target.exists()
    assert not plan.quarantine_path.exists()
    assert keeper.read_bytes() == PAYLOAD


def test_finalize_rejects_missing_keeper_and_retains_quarantine(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    _, executor = make_executor(base)
    assert executor.stage(plan).ok
    keeper.unlink()

    action = executor.finalize(plan)

    assert not action.ok
    assert action.code is FailureCode.MISSING_KEEPER
    assert plan.quarantine_path.read_bytes() == PAYLOAD
    assert not target.exists()


def test_finalize_replays_after_crash_between_unlink_and_finalized_record(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal_path = base.joinpath("actions.jsonl")
    normal = SafeActionExecutor(AppendOnlyJournal(journal_path))
    assert normal.stage(plan).ok
    crashing = SafeActionExecutor(CrashBeforeEventJournal(journal_path, JournalEventType.FINALIZED))

    with pytest.raises(SimulatedCrash):
        crashing.finalize(plan)

    assert not target.exists()
    assert not plan.quarantine_path.exists()
    assert keeper.exists()

    journal = AppendOnlyJournal(journal_path)
    replay = SafeActionExecutor(journal).finalize(plan)
    assert replay.ok
    assert replay.state is ActionState.FINALIZED
    assert not replay.changed
    assert JournalEventType.FINALIZED_RECOVERED in [event.event for event in journal.events_for(plan.plan_id)]


def test_finalize_replays_after_crash_with_identity_bound_tombstone(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal_path = base.joinpath("actions.jsonl")
    normal = SafeActionExecutor(AppendOnlyJournal(journal_path))
    assert normal.stage(plan).ok
    crashing = SafeActionExecutor(
        CrashBeforeEventJournal(
            journal_path,
            JournalEventType.FINALIZE_TOMBSTONED,
        )
    )

    with pytest.raises(SimulatedCrash):
        crashing.finalize(plan)

    assert not target.exists()
    assert not plan.quarantine_path.exists()
    assert plan.finalize_path.read_bytes() == PAYLOAD
    assert keeper.read_bytes() == PAYLOAD

    journal = AppendOnlyJournal(journal_path)
    replay = SafeActionExecutor(journal).finalize(plan)

    assert replay.ok
    assert replay.state is ActionState.FINALIZED
    assert not plan.finalize_path.exists()
    assert keeper.read_bytes() == PAYLOAD


def test_finalize_never_unlinks_replacement_moved_into_tombstone(tmpdir):
    adapter_base = type(platform_file_system())

    class ReplaceDuringFinalizeFileSystem(adapter_base):
        def __init__(self):
            self.raced = False
            self.preserved = None

        def rename_no_replace_verified(self, source, destination, verified_handle):
            if source.name == "payload" and destination.name == "finalizing":
                self.raced = True
                self.preserved = source.with_name("verified-payload-preserved")
                os.rename(str(source), str(self.preserved))
                source.write_bytes(b"unrelated replacement")
            return super().rename_no_replace_verified(
                source,
                destination,
                verified_handle,
            )

    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    file_system = ReplaceDuringFinalizeFileSystem()
    journal = AppendOnlyJournal(base.joinpath("actions.jsonl"), fs=file_system)
    executor = SafeActionExecutor(journal, fs=file_system)
    assert executor.stage(plan).ok

    action = executor.finalize(plan)

    assert file_system.raced
    assert not action.ok
    assert action.code is FailureCode.IO_ERROR
    assert file_system.preserved.read_bytes() == PAYLOAD
    assert plan.quarantine_path.read_bytes() == b"unrelated replacement"
    assert not plan.finalize_path.exists()
    assert keeper.read_bytes() == PAYLOAD
    assert not target.exists()


def test_finalize_delete_never_unlinks_a_racing_name_replacement(tmpdir):
    adapter_base = type(platform_file_system())

    class ReplaceBeforeVerifiedDeleteFileSystem(adapter_base):
        def __init__(self):
            self.raced = False
            self.preserved = None

        def delete_verified_regular_file(self, path, verified_handle):
            self.raced = True
            self.preserved = path.with_name("verified-tombstone-preserved")
            os.rename(str(path), str(self.preserved))
            path.write_bytes(b"unrelated replacement")
            return super().delete_verified_regular_file(path, verified_handle)

    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    file_system = ReplaceBeforeVerifiedDeleteFileSystem()
    journal = AppendOnlyJournal(base.joinpath("actions.jsonl"), fs=file_system)
    executor = SafeActionExecutor(journal, fs=file_system)
    assert executor.stage(plan).ok

    action = executor.finalize(plan)

    assert file_system.raced
    assert not action.ok
    assert action.code is FailureCode.IDENTITY_MISMATCH
    assert file_system.preserved.read_bytes() == PAYLOAD
    assert plan.finalize_path.read_bytes() == b"unrelated replacement"
    assert keeper.read_bytes() == PAYLOAD
    assert not target.exists()
    assert JournalEventType.FINALIZED not in [event.event for event in journal.events_for(plan.plan_id)]


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound finalize contract")
def test_finalize_has_no_path_unlink_fallback(tmpdir, monkeypatch):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal, executor = make_executor(base)
    assert executor.stage(plan).ok

    def unsupported(_descriptor, _path):
        raise OSError(errno.ENOTSUP, "simulated unsupported disposition API")

    monkeypatch.setattr(
        atomic_rename,
        "_set_windows_delete_disposition",
        unsupported,
    )

    action = executor.finalize(plan)

    assert not action.ok
    assert action.code is FailureCode.IO_ERROR
    assert action.changed
    assert plan.finalize_path.read_bytes() == PAYLOAD
    assert keeper.read_bytes() == PAYLOAD
    assert not target.exists()
    assert JournalEventType.FINALIZED not in [event.event for event in journal.events_for(plan.plan_id)]


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlinks are unavailable")
def test_finalize_rejects_two_names_without_unlinking_either(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    _, executor = make_executor(base)
    assert executor.stage(plan).ok
    try:
        os.link(plan.quarantine_path, plan.finalize_path)
    except OSError as error:
        pytest.skip("hardlinks are unavailable: {}".format(error))

    action = executor.finalize(plan)

    assert not action.ok
    assert action.code is FailureCode.QUARANTINE_CONFLICT
    assert plan.quarantine_path.read_bytes() == PAYLOAD
    assert plan.finalize_path.read_bytes() == PAYLOAD
    assert keeper.read_bytes() == PAYLOAD
    assert not target.exists()


def test_journal_fingerprint_prevents_plan_id_reuse(tmpdir):
    base, library, quarantine, target, keeper, first_result = make_environment(tmpdir, plan_id=str(uuid.uuid4()))
    first = assert_plan(first_result)
    journal, executor = make_executor(base)
    assert executor.stage(first).ok

    second_target = library.joinpath("second-target")
    second_keeper = library.joinpath("second-keeper")
    second_target.write_bytes(PAYLOAD)
    second_keeper.write_bytes(PAYLOAD)
    second_result = build_operation_plan(
        second_target,
        second_keeper,
        [library],
        quarantine,
        plan_id=first.plan_id,
    )

    assert not second_result.ok
    assert second_result.failure.code is FailureCode.QUARANTINE_CONFLICT
    assert journal.events_for(first.plan_id)


def test_complete_corrupt_journal_record_fails_closed(tmpdir):
    base, _, _, target, keeper, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal, executor = make_executor(base)
    assert executor.stage(plan).ok
    with journal.path.open("ab") as file_handle:
        file_handle.write(b"not-json\n")

    action = executor.finalize(plan)

    assert not action.ok
    assert action.code is FailureCode.JOURNAL_CORRUPT
    assert plan.quarantine_path.read_bytes() == PAYLOAD
    assert keeper.exists()
    assert not target.exists()


def test_truncated_final_journal_record_is_ignored_for_replay(tmpdir):
    base, _, _, _, _, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal, executor = make_executor(base)
    assert executor.stage(plan).ok
    with journal.path.open("ab") as file_handle:
        file_handle.write(b'{"partial":')

    events = journal.events_for(plan.plan_id)

    assert events[-1].event is JournalEventType.STAGED


def test_journal_refuses_to_append_after_truncated_final_record(tmpdir):
    base, _, _, _, _, result = make_environment(tmpdir)
    plan = assert_plan(result)
    journal, executor = make_executor(base)
    assert executor.stage(plan).ok
    with journal.path.open("ab") as file_handle:
        file_handle.write(b'{"partial":')
    before = journal.path.read_bytes()

    with pytest.raises(
        safe_action.JournalError,
        match="partial record",
    ):
        journal.append(plan, JournalEventType.FAILED)

    assert journal.path.read_bytes() == before
