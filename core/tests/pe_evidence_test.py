import pytest

from core.pe.evidence import (
    CropRect,
    ExactGroup,
    ExactProof,
    FileSnapshot,
    ImageTransform,
    MatchEvidence,
    OrientationTransform,
    RelationType,
    ReviewGroup,
    ScanReceipt,
    ScanState,
    SemanticGroup,
    build_leakage_components,
)

DIGEST = b"\x12" * 32


def snapshot(asset_id, size=10, digest=DIGEST):
    return FileSnapshot(
        asset_id=asset_id,
        path="/library/{}.png".format(asset_id),
        size=size,
        mtime_ns=123,
        digest_algorithm="sha256",
        digest=digest,
        volume_id="volume",
        file_id="file-{}".format(asset_id),
    )


def exact_proof(first_id, second_id, size=10):
    return ExactProof(snapshot(first_id, size=size), snapshot(second_id, size=size), bytes_compared=size)


def visual_evidence(first_id, second_id, score=0.95, relation=RelationType.NEAR_DUPLICATE):
    return MatchEvidence(
        first_id,
        second_id,
        relation,
        score,
        "visual-refine",
        "1",
    )


def semantic_evidence(first_id, second_id, score=0.8):
    return MatchEvidence(
        first_id,
        second_id,
        RelationType.SEMANTIC_RELATED,
        score,
        "semantic",
        "1",
    )


def test_exact_proof_requires_complete_byte_comparison():
    with pytest.raises(ValueError, match="full byte comparison"):
        ExactProof(snapshot("a"), snapshot("b"), bytes_compared=9)


def test_exact_proof_rejects_digest_mismatch():
    with pytest.raises(ValueError, match="equal full digests"):
        ExactProof(snapshot("a"), snapshot("b", digest=b"\x34" * 32), bytes_compared=10)


def test_verified_exact_evidence_requires_proof_and_identity_transform():
    with pytest.raises(ValueError, match="requires ExactProof"):
        MatchEvidence("a", "b", RelationType.VERIFIED_EXACT, 1, "exact", "1")
    proof = exact_proof("a", "b")
    with pytest.raises(ValueError, match="identity transform"):
        MatchEvidence(
            "a",
            "b",
            RelationType.VERIFIED_EXACT,
            1,
            "exact",
            "1",
            transform=ImageTransform(orientation=OrientationTransform.ROTATE_90),
            exact_proof=proof,
        )
    evidence = MatchEvidence(
        "a",
        "b",
        RelationType.VERIFIED_EXACT,
        1,
        "exact",
        "1",
        exact_proof=proof,
    )
    assert evidence.relation.allows_automatic_destructive_action
    assert evidence.to_dict()["exact_proof"]["bytes_compared"] == 10


def test_transform_validates_normalized_crop_and_scale():
    transform = ImageTransform(
        orientation=OrientationTransform.FLIP_HORIZONTAL,
        scale_x=0.5,
        scale_y=0.5,
        crop=CropRect(0.1, 0.2, 0.8, 0.7),
        alignment_score=0.99,
    )
    assert not transform.is_identity
    with pytest.raises(ValueError):
        CropRect(0.5, 0.5, 0.6, 0.6)
    with pytest.raises(ValueError):
        ImageTransform(scale_x=0)


def test_exact_group_accepts_connected_star_proofs_in_linear_space():
    group = ExactGroup.from_proofs(
        [
            exact_proof("canonical", "b"),
            exact_proof("canonical", "c"),
        ],
        canonical_id="canonical",
    )
    assert group.members == ("b", "c", "canonical")
    assert len(group.proofs) == 2
    assert group.group_id.startswith("exact:")


def test_exact_group_rejects_disconnected_proofs():
    with pytest.raises(ValueError, match="connected"):
        ExactGroup(
            members=("a", "b", "c", "d"),
            canonical_id="a",
            digest_algorithm="sha256",
            digest=DIGEST,
            size=10,
            proofs=(exact_proof("a", "b"), exact_proof("c", "d")),
        )


def test_review_group_distinguishes_clique_from_connected_chain():
    chain = [visual_evidence("a", "b"), visual_evidence("b", "c")]
    with pytest.raises(ValueError, match="every pair"):
        ReviewGroup.from_evidence(chain, require_clique=True)
    connected = ReviewGroup.from_evidence(chain, representative_id="b", require_clique=False)
    assert connected.members == ("a", "b", "c")
    clique = ReviewGroup.from_evidence(chain + [visual_evidence("a", "c")])
    assert clique.require_clique


def test_leakage_components_use_transitive_closure_but_exclude_semantic_edges():
    evidence = [
        visual_evidence("a", "b"),
        visual_evidence("b", "c"),
        visual_evidence("x", "y"),
        semantic_evidence("c", "x"),
    ]
    components = build_leakage_components(evidence)
    assert [component.members for component in components] == [("a", "b", "c"), ("x", "y")]
    assert all("x" not in component.members for component in components if "a" in component.members)


def test_semantic_group_is_centered_and_never_a_review_group():
    evidence = [semantic_evidence("query", "a"), semantic_evidence("b", "query")]
    group = SemanticGroup.from_evidence("query", evidence)
    assert group.related_ids == ("a", "b")
    with pytest.raises(ValueError, match="semantic-only"):
        ReviewGroup.from_evidence(evidence, require_clique=False)


def test_scan_receipt_only_allows_automatic_action_when_fully_complete():
    complete = ScanReceipt("scan", ScanState.COMPLETE, discovered=3, indexed=3, analyzed=3)
    skipped = ScanReceipt(
        "scan-skipped",
        ScanState.COMPLETE_WITH_SKIPS,
        discovered=3,
        indexed=3,
        analyzed=2,
        skipped=1,
    )
    assert complete.allows_automatic_destructive_action
    assert not skipped.allows_automatic_destructive_action
