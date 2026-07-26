import hashlib
import os
from pathlib import Path

import pytest

from core import action_plan as action_plan_module
from core import engine, fs
from core.action_plan import ActionPlanError, build_bound_deletion_plan
from core.destructive_eligibility import BatchEligibility
from core.file_identity import get_file_identity, identity_record_parts
from core.quarantine import QuarantineManager
from core.results import Results
from core.scan_receipt import ScanReceipt
from core.services.models import FileRecord
from core.tests.base import DupeGuru


def _verified_results(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    keeper_path = root / "keeper.bin"
    target_path = root / "target.bin"
    keeper_path.write_bytes(b"verified payload")
    target_path.write_bytes(b"verified payload")
    keeper = fs.File(keeper_path)
    target = fs.File(target_path)
    keeper.is_ref = False
    target.is_ref = False
    keeper.comparison_pool = "incoming"
    target.comparison_pool = "incoming"
    digest, _ = keeper._calc_digest_with_snapshot()
    group = engine.build_verified_exact_group(
        [keeper, target],
        digest=digest,
        size=keeper_path.stat().st_size,
        algorithm=keeper.digest_algorithm,
    )
    results = Results(DupeGuru())
    results.groups = [group]
    results.scan_receipt = ScanReceipt.completed(2)
    results.mark(target)
    return root, results, keeper, target


def test_builds_quarantine_plan_bound_to_live_exact_result(tmp_path):
    root, results, keeper, target = _verified_results(tmp_path)

    bound = build_bound_deletion_plan(results, [target], [root])

    assert len(bound.plan.actions) == 1
    action = bound.plan.actions[0]
    assert action.operation == "quarantine"
    assert action.target.path == os.path.abspath(target.path)
    assert action.reference.path == os.path.abspath(keeper.path)
    assert action.target.digest_algorithm == "sha256"
    assert action.reference.digest_algorithm == "sha256"
    assert action.target.digest == hashlib.sha256(b"verified payload").hexdigest()
    assert action.target.digest == action.reference.digest
    assert (action.target.volume_id, action.target.file_id) == identity_record_parts(
        get_file_identity(Path(action.target.path), follow_symlinks=False)
    )
    assert (
        action.reference.volume_id,
        action.reference.file_id,
    ) == identity_record_parts(get_file_identity(Path(action.reference.path), follow_symlinks=False))
    assert bound.dupe_for_action(action.action_id) is target
    assert not root.joinpath(".dupeguru-neo-quarantine").exists()


def test_bound_gui_plan_rejects_same_bytes_replacement_before_persistence(
    tmp_path,
):
    root, results, _, target = _verified_results(tmp_path)
    bound = build_bound_deletion_plan(results, [target], [root])
    action = bound.plan.actions[0]
    target_path = Path(action.target.path)
    before = target_path.stat()
    payload = target_path.read_bytes()
    replacement = root / "replacement.tmp"
    replacement.write_bytes(payload)
    os.utime(
        replacement,
        ns=(before.st_atime_ns, action.target.mtime_ns),
    )
    os.replace(replacement, target_path)
    os.utime(
        target_path,
        ns=(before.st_atime_ns, action.target.mtime_ns),
    )

    batch = QuarantineManager().prepare(bound.plan)

    assert not batch.ok
    assert [failure.code for failure in batch.failures] == ["identity_mismatch"]
    assert target_path.read_bytes() == payload
    assert not root.joinpath(".dupeguru-neo-quarantine").exists()


def test_bound_plan_has_no_permanent_delete_mode(tmp_path):
    root, results, _, target = _verified_results(tmp_path)

    with pytest.raises(TypeError, match="permanent"):
        build_bound_deletion_plan(
            results,
            [target],
            [root],
            permanent=True,
        )


def test_plan_is_stable_except_for_creation_timestamp(tmp_path):
    root, results, _, target = _verified_results(tmp_path)

    first = build_bound_deletion_plan(results, [target], [root])
    second = build_bound_deletion_plan(results, [target], [root])

    assert first.plan.plan_id == second.plan.plan_id
    assert first.plan.actions == second.plan.actions


def test_size_change_after_scan_fails_without_writing(tmp_path):
    root, results, _, target = _verified_results(tmp_path)
    Path(target.path).write_bytes(b"different-sized payload")

    with pytest.raises(ActionPlanError, match="size changed"):
        build_bound_deletion_plan(results, [target], [root])

    assert not root.joinpath(".dupeguru-neo-quarantine").exists()


def test_same_size_content_change_fails_final_byte_proof_without_writing(tmp_path):
    root, results, _, target = _verified_results(tmp_path)
    Path(target.path).write_bytes(b"tampered payload")

    with pytest.raises(ActionPlanError, match="no longer byte-identical"):
        build_bound_deletion_plan(results, [target], [root])

    assert not root.joinpath(".dupeguru-neo-quarantine").exists()


def test_identical_post_scan_rewrite_fails_original_exact_evidence(tmp_path):
    root, results, keeper, target = _verified_results(tmp_path)
    Path(keeper.path).write_bytes(b"tampered payload")
    Path(target.path).write_bytes(b"tampered payload")

    with pytest.raises(ActionPlanError, match="full digest no longer matches"):
        build_bound_deletion_plan(results, [target], [root])

    assert not root.joinpath(".dupeguru-neo-quarantine").exists()


def test_protected_target_is_rejected(tmp_path):
    root, results, _, target = _verified_results(tmp_path)
    target.comparison_pool = "protected"

    with pytest.raises(ActionPlanError, match="protected"):
        build_bound_deletion_plan(results, [target], [root])


def test_current_pool_is_rechecked_after_live_byte_proof(tmp_path):
    root, results, _, target = _verified_results(tmp_path)
    target_path = Path(target.path)
    calls = {}

    def changing_pool(path):
        path = Path(path)
        calls[path] = calls.get(path, 0) + 1
        if path == target_path and calls[path] > 1:
            return "excluded"
        return "incoming"

    with pytest.raises(ActionPlanError, match="Run a new scan"):
        build_bound_deletion_plan(
            results,
            [target],
            [root],
            current_pool_resolver=changing_pool,
        )

    assert not root.joinpath(".dupeguru-neo-quarantine").exists()


def test_large_group_id_is_computed_once_while_live_proofs_remain_per_target(
    tmp_path,
    monkeypatch,
):
    duplicate_count = 10_000

    class SyntheticFile:
        __slots__ = ("comparison_pool", "is_ref", "path", "size")

        def __init__(self, path):
            self.path = path
            self.size = 1
            self.is_ref = False
            self.comparison_pool = "incoming"

    keeper = SyntheticFile(tmp_path / "keeper.bin")
    duplicates = [SyntheticFile(tmp_path / "duplicate-{:05d}.bin".format(index)) for index in range(duplicate_count)]
    evidence = engine.ExactEvidence(
        kind=engine.VerificationKind.VERIFIED_EXACT,
        algorithm="sha256",
        digest=b"\x01" * 32,
        size=1,
    )
    group = engine.Group.from_exact_files([keeper, *duplicates], evidence)

    class SyntheticResults:
        loaded_report = False
        scan_receipt = ScanReceipt.completed(duplicate_count + 1)

        @staticmethod
        def get_group_of_duplicate(dupe):
            return group if dupe in group.unordered else None

    group_id_calls = 0
    live_proof_calls = 0

    def count_group_id(scan_id, candidate_group):
        nonlocal group_id_calls
        group_id_calls += 1
        assert scan_id == SyntheticResults.scan_receipt.scan_id
        assert candidate_group is group
        return "a" * 64

    def build_synthetic_records(target, reference, candidate_evidence, _file_system):
        nonlocal live_proof_calls
        live_proof_calls += 1
        assert reference is keeper
        assert candidate_evidence is evidence

        def record(file):
            return FileRecord(
                path=os.path.abspath(file.path),
                size=1,
                mtime_ns=1,
                digest_algorithm="sha256",
                digest="01" * 32,
                volume_id="1",
                file_id=str(id(file)),
            )

        return record(target), record(reference)

    def allow_every_candidate(_results, candidates, _resolver):
        return BatchEligibility(tuple(candidates), ())

    monkeypatch.setattr(action_plan_module, "_group_id", count_group_id)
    monkeypatch.setattr(
        action_plan_module,
        "_verified_pair_records",
        build_synthetic_records,
    )
    monkeypatch.setattr(action_plan_module, "evaluate_batch", allow_every_candidate)

    bound = build_bound_deletion_plan(
        SyntheticResults(),
        duplicates,
        [tmp_path],
    )

    assert group_id_calls == 1
    assert live_proof_calls == duplicate_count
    assert len(bound.plan.actions) == duplicate_count
