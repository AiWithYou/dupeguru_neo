from core.keeper import (
    KeeperCandidate,
    KeeperDecision,
    choose_keeper,
)
from core.tests.base import NamedObject


def test_protected_file_is_forced_to_the_top():
    large = NamedObject("large.png", size=10_000)
    protected = NamedObject("original.jpg", size=1)
    protected.is_ref = True
    protected.comparison_pool = "protected"

    decision = choose_keeper([large, protected])

    assert decision.keeper is protected
    assert "protected library" in decision.explanation(protected)


def test_protected_file_is_a_hard_constraint_even_against_all_quality_bonuses():
    preferred = NamedObject("archive/original.raw", size=10_000_000)
    preferred.dimensions = (20_000, 20_000)
    preferred.bit_depth = 32
    preferred.exif_count = 1_000
    preferred.bitrate = 1_000_000
    protected = NamedObject("protected/reference.jpg", size=1)
    protected.is_ref = True
    protected.comparison_pool = "protected"

    decision = choose_keeper([preferred, protected])

    assert decision.keeper is protected
    assert decision.sort_key(protected) < decision.sort_key(preferred)


def test_compare_only_reference_is_explained_without_claiming_protected_library():
    incoming = NamedObject("incoming/original.png", size=10_000)
    compare_only = NamedObject("external/review.jpg", size=1)
    compare_only.is_ref = True
    compare_only.comparison_pool = "compare_only"

    decision = choose_keeper([incoming, compare_only])

    assert decision.keeper is compare_only
    explanation = decision.explanation(compare_only)
    assert "immutable Compare Only source" in explanation
    assert "protected library" not in explanation


def test_quality_rules_are_explainable_and_deterministic():
    original = NamedObject("portrait.png", size=5_000)
    original.dimensions = (4000, 3000)
    original.bit_depth = 16
    original.exif_count = 40
    copy = NamedObject("portrait copy.jpg", size=1_000, folder="Downloads")
    copy.dimensions = (1000, 750)
    copy.bit_depth = 8
    copy.jpeg_artifact_score = 0.8

    forward = choose_keeper([copy, original])
    reverse = choose_keeper([original, copy])

    assert forward.keeper is original
    assert reverse.keeper is original
    assert forward.candidate_for(original).score == reverse.candidate_for(original).score
    explanation = forward.explanation(copy)
    assert "copy/backup-style filename" in explanation
    assert "temporary/download folder" in explanation
    assert "lower resolution than the keeper" in explanation


def test_score_does_not_claim_deletion_safety():
    first = NamedObject("a.raw", size=10)
    second = NamedObject("b.jpg", size=1)

    decision = choose_keeper([first, second])

    assert decision.keeper is first
    assert "delete" not in decision.explanation(second).casefold()


def test_stable_path_breaks_an_exact_score_tie():
    second = NamedObject("z.bin", size=1)
    first = NamedObject("a.bin", size=1)

    decision = choose_keeper([second, first])

    assert decision.keeper is first


def test_candidate_lookup_does_not_rescan_ranked_candidates():
    class CountingCandidates(tuple):
        def __new__(cls, values):
            instance = super().__new__(cls, values)
            instance.iterations = 0
            return instance

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    files = tuple(NamedObject("asset-{:04d}.bin".format(index), size=index + 1) for index in range(2_000))
    candidates = CountingCandidates(KeeperCandidate(file, float(index), ()) for index, file in enumerate(files))
    decision = KeeperDecision(candidates)
    candidates.iterations = 0

    for file in files:
        assert decision.candidate_for(file).file is file

    assert candidates.iterations == 0
