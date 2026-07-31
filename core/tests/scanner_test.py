# Copyright 2016 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import pytest

from hscommon.jobprogress import job
from pathlib import Path
from hscommon.testutil import eq_

from core import engine, fs
from core.engine import getwords, Match
from core.ignore import IgnoreList
from core.me.fs import MusicFile
from core.scanner import Scanner, ScanType
from core.me.scanner import ScannerME


# TODO update this to be able to inherit from fs.File
class NamedObject:
    def __init__(self, name="foobar", size=1, path=None):
        if path is None:
            path = Path(name)
        else:
            path = Path(path, name)
        self.name = name
        self.size = size
        self.path = path
        self.words = getwords(name)

    def __repr__(self):
        return "<NamedObject {!r} {!r}>".format(self.name, self.path)

    def exists(self):
        return self.path.exists()

    def compare_bytes(self, other):
        return self.digest == other.digest


no = NamedObject


@pytest.fixture
def fake_fileexists(request):
    # This is a hack to avoid invalidating all previous tests since the scanner started to test
    # for file existence before doing the match grouping.
    monkeypatch = request.getfixturevalue("monkeypatch")
    monkeypatch.setattr(Path, "exists", lambda _: True)


def test_empty(fake_fileexists):
    s = Scanner()
    r = s.get_dupe_groups([])
    eq_(r, [])


def test_default_settings(fake_fileexists):
    s = Scanner()
    eq_(s.min_match_percentage, 80)
    eq_(s.scan_type, ScanType.FILENAME)
    eq_(s.mix_file_kind, True)
    eq_(s.word_weighting, False)
    eq_(s.match_similar_words, False)
    eq_(s.size_threshold, 0)
    eq_(s.large_size_threshold, 0)
    eq_(s.big_file_size_threshold, 0)


def test_simple_with_default_settings(fake_fileexists):
    s = Scanner()
    f = [no("foo bar", path="p1"), no("foo bar", path="p2"), no("foo bleh")]
    r = s.get_dupe_groups(f)
    eq_(len(r), 1)
    g = r[0]
    # 'foo bleh' cannot be in the group because the default min match % is 80
    eq_(len(g), 2)
    assert g.ref in f[:2]
    assert g.dupes[0] in f[:2]


def test_simple_with_lower_min_match(fake_fileexists):
    s = Scanner()
    s.min_match_percentage = 50
    f = [no("foo bar", path="p1"), no("foo bar", path="p2"), no("foo bleh")]
    r = s.get_dupe_groups(f)
    eq_(len(r), 1)
    g = r[0]
    eq_(len(g), 3)


def test_trim_all_ref_groups(fake_fileexists):
    # When all files of a group are ref, don't include that group in the results, but also don't
    # count the files from that group as discarded.
    s = Scanner()
    f = [
        no("foo", path="p1"),
        no("foo", path="p2"),
        no("bar", path="p1"),
        no("bar", path="p2"),
    ]
    f[2].is_ref = True
    f[3].is_ref = True
    r = s.get_dupe_groups(f)
    eq_(len(r), 1)
    eq_(s.discarded_file_count, 0)


def test_prioritize(fake_fileexists):
    s = Scanner()
    f = [
        no("foo", path="p1"),
        no("foo", path="p2"),
        no("bar", path="p1"),
        no("bar", path="p2"),
    ]
    f[1].size = 2
    f[2].size = 3
    f[3].is_ref = True
    r = s.get_dupe_groups(f)
    g1, g2 = r
    assert f[1] in (g1.ref, g2.ref)
    assert f[0] in (g1.dupes[0], g2.dupes[0])
    assert f[3] in (g1.ref, g2.ref)
    assert f[2] in (g1.dupes[0], g2.dupes[0])


def test_content_scan(fake_fileexists):
    s = Scanner()
    s.scan_type = ScanType.CONTENTS
    f = [no("foo"), no("bar"), no("bleh")]
    f[0].digest = f[0].digest_partial = f[0].digest_samples = "foobar"
    f[1].digest = f[1].digest_partial = f[1].digest_samples = "foobar"
    f[2].digest = f[2].digest_partial = f[1].digest_samples = "bleh"
    r = s.get_dupe_groups(f)
    eq_(len(r), 1)
    eq_(len(r[0]), 2)
    eq_(s.discarded_file_count, 0)  # don't count the different digest as discarded!


def test_content_scan_compare_sizes_first(fake_fileexists):
    class MyFile(no):
        @property
        def digest(self):
            raise AssertionError()

    s = Scanner()
    s.scan_type = ScanType.CONTENTS
    f = [MyFile("foo", 1), MyFile("bar", 2)]
    eq_(len(s.get_dupe_groups(f)), 0)


def test_content_scan_checks_for_cancellation_inside_engine(monkeypatch):
    checks = []

    class ProbeJob:
        def start_subjob(self, *args, **kwargs):
            return self

        def check_if_cancelled(self):
            checks.append(True)

    def getgroups_by_contents(files, *, bigsize, j, stop_check):
        assert stop_check() is False
        return []

    monkeypatch.setattr(engine, "getgroups_by_contents", getgroups_by_contents)
    scanner = Scanner()
    scanner.scan_type = ScanType.CONTENTS

    assert scanner._getmatches([no("foo")], ProbeJob()) == []
    assert checks == [True]


def test_music_content_scan_uses_strict_direct_hashing(tmp_path):
    first_path = tmp_path / "first.mp3"
    second_path = tmp_path / "second.mp3"
    first_path.write_bytes(b"same music payload")
    second_path.write_bytes(b"same music payload")
    scanner = ScannerME()
    scanner.scan_type = ScanType.CONTENTS

    [group] = scanner.get_dupe_groups(
        [MusicFile(first_path), MusicFile(second_path)],
    )

    assert {file.path for file in group} == {first_path, second_path}
    assert group.verification_kind is engine.VerificationKind.VERIFIED_EXACT


def test_ignore_file_size(fake_fileexists):
    s = Scanner()
    s.scan_type = ScanType.CONTENTS
    small_size = 10  # 10KB
    s.size_threshold = 0
    large_size = 100 * 1024 * 1024  # 100MB
    s.large_size_threshold = 0
    f = [
        no("smallignore1", small_size - 1),
        no("smallignore2", small_size - 1),
        no("small1", small_size),
        no("small2", small_size),
        no("large1", large_size),
        no("large2", large_size),
        no("largeignore1", large_size + 1),
        no("largeignore2", large_size + 1),
    ]
    f[0].digest = f[0].digest_partial = f[0].digest_samples = "smallignore"
    f[1].digest = f[1].digest_partial = f[1].digest_samples = "smallignore"
    f[2].digest = f[2].digest_partial = f[2].digest_samples = "small"
    f[3].digest = f[3].digest_partial = f[3].digest_samples = "small"
    f[4].digest = f[4].digest_partial = f[4].digest_samples = "large"
    f[5].digest = f[5].digest_partial = f[5].digest_samples = "large"
    f[6].digest = f[6].digest_partial = f[6].digest_samples = "largeignore"
    f[7].digest = f[7].digest_partial = f[7].digest_samples = "largeignore"

    r = s.get_dupe_groups(f)
    # No ignores
    eq_(len(r), 4)
    # Ignore smaller
    s.size_threshold = small_size
    r = s.get_dupe_groups(f)
    eq_(len(r), 3)
    # Ignore larger
    s.size_threshold = 0
    s.large_size_threshold = large_size
    r = s.get_dupe_groups(f)
    eq_(len(r), 3)
    # Ignore both
    s.size_threshold = small_size
    r = s.get_dupe_groups(f)
    eq_(len(r), 2)


def test_big_file_partial_hashes(fake_fileexists):
    s = Scanner()
    s.scan_type = ScanType.CONTENTS

    smallsize = 1
    bigsize = 100 * 1024 * 1024  # 100MB
    s.big_file_size_threshold = bigsize

    f = [no("bigfoo", bigsize), no("bigbar", bigsize), no("smallfoo", smallsize), no("smallbar", smallsize)]
    f[0].digest = f[0].digest_partial = f[0].digest_samples = "foobar"
    f[1].digest = f[1].digest_partial = f[1].digest_samples = "foobar"
    f[2].digest = f[2].digest_partial = "bleh"
    f[3].digest = f[3].digest_partial = "bleh"
    r = s.get_dupe_groups(f)
    eq_(len(r), 2)

    # digest_partial is still the same, but the file is actually different
    f[1].digest = f[1].digest_samples = "difffoobar"
    # here we compare the full digests, as the user disabled the optimization
    s.big_file_size_threshold = 0
    r = s.get_dupe_groups(f)
    eq_(len(r), 1)

    # here we should compare the digest_samples, and see they are different
    s.big_file_size_threshold = bigsize
    r = s.get_dupe_groups(f)
    eq_(len(r), 1)


def test_content_scan_keeps_extensions_separate_when_requested(fake_fileexists):
    scanner = Scanner()
    scanner.scan_type = ScanType.CONTENTS
    scanner.mix_file_kind = False
    files = [no("first.txt"), no("second.txt"), no("third.bin")]
    for file in files:
        file.digest = file.digest_partial = file.digest_samples = "same"
    [group] = scanner.get_dupe_groups(files)
    assert set(group) == set(files[:2])


def test_content_scan_respects_ignored_pairs_without_pairwise_storage(fake_fileexists):
    scanner = Scanner()
    scanner.scan_type = ScanType.CONTENTS
    files = [no("first"), no("second"), no("third")]
    for file in files:
        file.digest = file.digest_partial = file.digest_samples = "same"
    ignore_list = IgnoreList()
    ignore_list.ignore(str(files[0].path), str(files[1].path))
    groups = scanner.get_dupe_groups(files, ignore_list)
    assert all(not (files[0] in group and files[1] in group) for group in groups)


def test_exact_partitioning_is_linear_when_global_ignore_is_unrelated():
    class CountingIgnoreList(IgnoreList):
        def __init__(self):
            super().__init__()
            self.pair_checks = 0

        def are_ignored(self, first, second):
            self.pair_checks += 1
            return super().are_ignored(first, second)

    scanner = Scanner()
    scanner.scan_type = ScanType.CONTENTS
    files = [no("duplicate-{}.bin".format(index), path="library") for index in range(10_000)]
    ignore_list = CountingIgnoreList()
    ignore_list.ignore("outside-a", "outside-b")
    ignore_list.pair_checks = 0

    partitions = scanner._partition_exact_candidates(files, ignore_list)

    assert len(partitions) == 1
    assert len(partitions[0]["files"]) == len(files)
    assert ignore_list.pair_checks == 0


def test_content_scan_preserves_protected_references(fake_fileexists):
    scanner = Scanner()
    scanner.scan_type = ScanType.CONTENTS
    files = [no("protected-one"), no("protected-two"), no("normal")]
    for file in files:
        file.digest = file.digest_partial = file.digest_samples = "same"
    files[0].is_ref = True
    files[1].is_ref = True
    [group] = scanner.get_dupe_groups(files)
    assert set(group) == set(files)
    assert group.ref.is_ref
    assert group.verification_kind is engine.VerificationKind.VERIFIED_EXACT


def test_min_match_perc_doesnt_matter_for_content_scan(fake_fileexists):
    s = Scanner()
    s.scan_type = ScanType.CONTENTS
    f = [no("foo"), no("bar"), no("bleh")]
    f[0].digest = f[0].digest_partial = f[0].digest_samples = "foobar"
    f[1].digest = f[1].digest_partial = f[1].digest_samples = "foobar"
    f[2].digest = f[2].digest_partial = f[2].digest_samples = "bleh"
    s.min_match_percentage = 101
    r = s.get_dupe_groups(f)
    eq_(len(r), 1)
    eq_(len(r[0]), 2)
    s.min_match_percentage = 0
    r = s.get_dupe_groups(f)
    eq_(len(r), 1)
    eq_(len(r[0]), 2)


def test_content_scan_doesnt_put_digest_in_words_at_the_end(fake_fileexists):
    s = Scanner()
    s.scan_type = ScanType.CONTENTS
    f = [no("foo"), no("bar")]
    f[0].digest = f[0].digest_partial = f[0].digest_samples = (
        "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"
    )
    f[1].digest = f[1].digest_partial = f[1].digest_samples = (
        "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"
    )
    r = s.get_dupe_groups(f)
    # FIXME looks like we are missing something here?
    r[0]


def test_content_byte_verification_failure_marks_scan_incomplete():
    class UnreadableFile(no):
        def compare_bytes(self, other):
            raise OSError("simulated byte read failure")

    first = UnreadableFile("first", size=1)
    second = UnreadableFile("second", size=1)
    first.digest_partial = second.digest_partial = "same-partial"
    first.digest = second.digest = "same-full"
    scanner = Scanner()
    scanner.scan_type = ScanType.CONTENTS

    assert scanner.get_dupe_groups([first, second]) == []
    assert not scanner.scan_receipt.complete
    assert not scanner.scan_receipt.allows_destructive_actions
    assert scanner.scan_receipt.failed == 2
    assert scanner.scan_receipt.issues[0].code == "byte_verification_failed"


def test_content_scan_byte_compares_each_group_member_only_once(fake_fileexists):
    comparisons = []

    class ComparedFile(no):
        def compare_bytes(self, other):
            comparisons.append((self, other))
            return self.digest == other.digest

    files = [ComparedFile(name, size=4) for name in ("first", "second", "third")]
    for file in files:
        file.digest_partial = file.digest_samples = file.digest = b"same"
    scanner = Scanner()
    scanner.scan_type = ScanType.CONTENTS

    [group] = scanner.get_dupe_groups(files)

    assert len(group) == 3
    assert len(comparisons) == 2


def test_content_full_hash_change_marks_entire_scan_incomplete(
    tmp_path,
    monkeypatch,
):
    paths = [
        tmp_path / "first.bin",
        tmp_path / "second.bin",
        tmp_path / "changed.bin",
    ]
    payload = b"same exact payload" * 4096
    for path in paths:
        path.write_bytes(payload)
    files = [fs.File(path) for path in paths]
    uncached = fs.FilesDB()
    monkeypatch.setattr(fs, "filesdb", uncached)
    original = fs.File._calc_digest_with_snapshot

    def changed_during_full_hash(file):
        if file.path == paths[-1]:
            raise fs.FileChangedError("simulated generation change")
        return original(file)

    monkeypatch.setattr(
        fs.File,
        "_calc_digest_with_snapshot",
        changed_during_full_hash,
    )
    scanner = Scanner()
    scanner.scan_type = ScanType.CONTENTS

    groups = scanner.get_dupe_groups(files)

    assert len(groups) == 1
    assert set(groups[0]) == set(files[:2])
    assert not scanner.scan_receipt.complete
    assert not scanner.scan_receipt.allows_destructive_actions
    assert scanner.scan_receipt.failed == 1
    assert scanner.scan_receipt.issues[0].code == "exact_hash_failed"
    assert "digest" in scanner.scan_receipt.issues[0].message


def test_content_cache_read_error_is_not_treated_as_a_cache_miss(
    tmp_path,
    monkeypatch,
):
    paths = [tmp_path / "first.bin", tmp_path / "second.bin"]
    for path in paths:
        path.write_bytes(b"same exact payload" * 4096)
    database = fs.FilesDB()
    database.connect(tmp_path / "hashes.db")
    database.select_query = "SELECT digest FROM missing_hash_cache_table"
    monkeypatch.setattr(fs, "filesdb", database)
    scanner = Scanner()
    scanner.scan_type = ScanType.CONTENTS
    try:
        groups = scanner.get_dupe_groups([fs.File(path) for path in paths])
    finally:
        database.close()

    assert groups == []
    assert not scanner.scan_receipt.complete
    assert not scanner.scan_receipt.allows_destructive_actions
    assert scanner.scan_receipt.failed == 2
    assert all(issue.code == "exact_hash_failed" for issue in scanner.scan_receipt.issues)


def test_extension_is_not_counted_in_filename_scan(fake_fileexists):
    s = Scanner()
    s.min_match_percentage = 100
    f = [no("foo.bar"), no("foo.bleh")]
    r = s.get_dupe_groups(f)
    eq_(len(r), 1)
    eq_(len(r[0]), 2)


def test_job(fake_fileexists):
    def do_progress(progress, desc=""):
        log.append(progress)
        return True

    s = Scanner()
    log = []
    f = [no("foo bar"), no("foo bar"), no("foo bleh")]
    s.get_dupe_groups(f, j=job.Job(1, do_progress))
    eq_(log[0], 0)
    eq_(log[-1], 100)


def test_mix_file_kind(fake_fileexists):
    s = Scanner()
    s.mix_file_kind = False
    f = [no("foo.1"), no("foo.2")]
    r = s.get_dupe_groups(f)
    eq_(len(r), 0)


def test_word_weighting(fake_fileexists):
    s = Scanner()
    s.min_match_percentage = 75
    s.word_weighting = True
    f = [no("foo bar"), no("foo bar bleh")]
    r = s.get_dupe_groups(f)
    eq_(len(r), 1)
    g = r[0]
    m = g.get_match_of(g.dupes[0])
    eq_(m.percentage, 75)  # 16 letters, 12 matching


def test_similar_words(fake_fileexists):
    s = Scanner()
    s.match_similar_words = True
    f = [
        no("The White Stripes"),
        no("The Whites Stripe"),
        no("Limp Bizkit"),
        no("Limp Bizkitt"),
    ]
    r = s.get_dupe_groups(f)
    eq_(len(r), 2)


def test_fields(fake_fileexists):
    s = Scanner()
    s.scan_type = ScanType.FIELDS
    f = [no("The White Stripes - Little Ghost"), no("The White Stripes - Little Acorn")]
    r = s.get_dupe_groups(f)
    eq_(len(r), 0)


def test_fields_no_order(fake_fileexists):
    s = Scanner()
    s.scan_type = ScanType.FIELDSNOORDER
    f = [no("The White Stripes - Little Ghost"), no("Little Ghost - The White Stripes")]
    r = s.get_dupe_groups(f)
    eq_(len(r), 1)


def test_tag_scan(fake_fileexists):
    s = Scanner()
    s.scan_type = ScanType.TAG
    o1 = no("foo")
    o2 = no("bar")
    o1.artist = "The White Stripes"
    o1.title = "The Air Near My Fingers"
    o2.artist = "The White Stripes"
    o2.title = "The Air Near My Fingers"
    r = s.get_dupe_groups([o1, o2])
    eq_(len(r), 1)


def test_tag_with_album_scan(fake_fileexists):
    s = Scanner()
    s.scan_type = ScanType.TAG
    s.scanned_tags = {"artist", "album", "title"}
    o1 = no("foo")
    o2 = no("bar")
    o3 = no("bleh")
    o1.artist = "The White Stripes"
    o1.title = "The Air Near My Fingers"
    o1.album = "Elephant"
    o2.artist = "The White Stripes"
    o2.title = "The Air Near My Fingers"
    o2.album = "Elephant"
    o3.artist = "The White Stripes"
    o3.title = "The Air Near My Fingers"
    o3.album = "foobar"
    r = s.get_dupe_groups([o1, o2, o3])
    eq_(len(r), 1)


def test_that_dash_in_tags_dont_create_new_fields(fake_fileexists):
    s = Scanner()
    s.scan_type = ScanType.TAG
    s.scanned_tags = {"artist", "album", "title"}
    s.min_match_percentage = 50
    o1 = no("foo")
    o2 = no("bar")
    o1.artist = "The White Stripes - a"
    o1.title = "The Air Near My Fingers - a"
    o1.album = "Elephant - a"
    o2.artist = "The White Stripes - b"
    o2.title = "The Air Near My Fingers - b"
    o2.album = "Elephant - b"
    r = s.get_dupe_groups([o1, o2])
    eq_(len(r), 1)


def test_tag_scan_with_different_scanned(fake_fileexists):
    s = Scanner()
    s.scan_type = ScanType.TAG
    s.scanned_tags = {"track", "year"}
    o1 = no("foo")
    o2 = no("bar")
    o1.artist = "The White Stripes"
    o1.title = "some title"
    o1.track = "foo"
    o1.year = "bar"
    o2.artist = "The White Stripes"
    o2.title = "another title"
    o2.track = "foo"
    o2.year = "bar"
    r = s.get_dupe_groups([o1, o2])
    eq_(len(r), 1)


def test_tag_scan_only_scans_existing_tags(fake_fileexists):
    s = Scanner()
    s.scan_type = ScanType.TAG
    s.scanned_tags = {"artist", "foo"}
    o1 = no("foo")
    o2 = no("bar")
    o1.artist = "The White Stripes"
    o1.foo = "foo"
    o2.artist = "The White Stripes"
    o2.foo = "bar"
    r = s.get_dupe_groups([o1, o2])
    eq_(len(r), 1)  # Because 'foo' is not scanned, they match


def test_tag_scan_converts_to_str(fake_fileexists):
    s = Scanner()
    s.scan_type = ScanType.TAG
    s.scanned_tags = {"track"}
    o1 = no("foo")
    o2 = no("bar")
    o1.track = 42
    o2.track = 42
    try:
        r = s.get_dupe_groups([o1, o2])
    except TypeError:
        raise AssertionError()
    eq_(len(r), 1)


def test_tag_scan_non_ascii(fake_fileexists):
    s = Scanner()
    s.scan_type = ScanType.TAG
    s.scanned_tags = {"title"}
    o1 = no("foo")
    o2 = no("bar")
    o1.title = "foobar\u00e9"
    o2.title = "foobar\u00e9"
    try:
        r = s.get_dupe_groups([o1, o2])
    except UnicodeEncodeError:
        raise AssertionError()
    eq_(len(r), 1)


def test_ignore_list(fake_fileexists):
    s = Scanner()
    f1 = no("foobar")
    f2 = no("foobar")
    f3 = no("foobar")
    f1.path = Path("dir1/foobar")
    f2.path = Path("dir2/foobar")
    f3.path = Path("dir3/foobar")
    ignore_list = IgnoreList()
    ignore_list.ignore(str(f1.path), str(f2.path))
    ignore_list.ignore(str(f1.path), str(f3.path))
    r = s.get_dupe_groups([f1, f2, f3], ignore_list=ignore_list)
    eq_(len(r), 1)
    g = r[0]
    eq_(len(g.dupes), 1)
    assert f1 not in g
    assert f2 in g
    assert f3 in g
    # Ignored matches are not counted as discarded
    eq_(s.discarded_file_count, 0)


def test_ignore_list_checks_for_unicode(fake_fileexists):
    # scanner was calling path_str for ignore list checks. Since the Path changes, it must
    # be unicode(path)
    s = Scanner()
    f1 = no("foobar")
    f2 = no("foobar")
    f3 = no("foobar")
    f1.path = Path("foo1\u00e9")
    f2.path = Path("foo2\u00e9")
    f3.path = Path("foo3\u00e9")
    ignore_list = IgnoreList()
    ignore_list.ignore(str(f1.path), str(f2.path))
    ignore_list.ignore(str(f1.path), str(f3.path))
    r = s.get_dupe_groups([f1, f2, f3], ignore_list=ignore_list)
    eq_(len(r), 1)
    g = r[0]
    eq_(len(g.dupes), 1)
    assert f1 not in g
    assert f2 in g
    assert f3 in g


def test_file_evaluates_to_false(fake_fileexists):
    # A very wrong way to use any() was added at some point, causing resulting group list
    # to be empty.
    class FalseNamedObject(NamedObject):
        def __bool__(self):
            return False

    s = Scanner()
    f1 = FalseNamedObject("foobar", path="p1")
    f2 = FalseNamedObject("foobar", path="p2")
    r = s.get_dupe_groups([f1, f2])
    eq_(len(r), 1)


def test_size_threshold(fake_fileexists):
    # Only file equal or higher than the size_threshold in size are scanned
    s = Scanner()
    f1 = no("foo", 1, path="p1")
    f2 = no("foo", 2, path="p2")
    f3 = no("foo", 3, path="p3")
    s.size_threshold = 2
    groups = s.get_dupe_groups([f1, f2, f3])
    eq_(len(groups), 1)
    [group] = groups
    eq_(len(group), 2)
    assert f1 not in group
    assert f2 in group
    assert f3 in group


def test_tie_breaker_path_deepness(fake_fileexists):
    # If there is a tie in prioritization, path deepness is used as a tie breaker
    s = Scanner()
    o1, o2 = no("foo"), no("foo")
    o1.path = Path("foo")
    o2.path = Path("foo/bar")
    [group] = s.get_dupe_groups([o1, o2])
    assert group.ref is o2


def test_tie_breaker_copy(fake_fileexists):
    # if copy is in the words used (even if it has a deeper path), it becomes a dupe
    s = Scanner()
    o1, o2 = no("foo bar Copy"), no("foo bar")
    o1.path = Path("deeper/path")
    o2.path = Path("foo")
    [group] = s.get_dupe_groups([o1, o2])
    assert group.ref is o2


def test_tie_breaker_same_name_plus_digit(fake_fileexists):
    # if ref has the same words as dupe, but has some just one extra word which is a digit, it
    # becomes a dupe
    s = Scanner()
    o1 = no("foo bar 42")
    o2 = no("foo bar [42]")
    o3 = no("foo bar (42)")
    o4 = no("foo bar {42}")
    o5 = no("foo bar")
    # all numbered names have deeper paths, so they'll end up ref if the digits aren't correctly
    # used as tie breakers
    o1.path = Path("deeper/path")
    o2.path = Path("deeper/path")
    o3.path = Path("deeper/path")
    o4.path = Path("deeper/path")
    o5.path = Path("foo")
    [group] = s.get_dupe_groups([o1, o2, o3, o4, o5])
    assert group.ref is o5


def test_partial_group_match(fake_fileexists):
    # Count the number of discarded matches (when a file doesn't match all other dupes of the
    # group) in Scanner.discarded_file_count
    s = Scanner()
    o1, o2, o3 = no("a b"), no("a"), no("b")
    s.min_match_percentage = 50
    [group] = s.get_dupe_groups([o1, o2, o3])
    eq_(len(group), 2)
    assert o1 in group
    # The file that will actually be counted as a dupe is undefined. The only thing we want to test
    # is that we don't have both
    if o2 in group:
        assert o3 not in group
    else:
        assert o3 in group
    eq_(s.discarded_file_count, 1)


def test_dont_group_files_that_dont_exist(tmpdir):
    # when creating groups, check that files exist first. It's possible that these files have
    # been moved during the scan by the user.
    # In this test, we have to delete one of the files between the get_matches() part and the
    # get_groups() part.
    s = Scanner()
    s.scan_type = ScanType.CONTENTS
    p = Path(str(tmpdir))
    with p.joinpath("file1").open("w") as fp:
        fp.write("foo")
    with p.joinpath("file2").open("w") as fp:
        fp.write("foo")
    file1, file2 = fs.get_files(p)

    def getmatches(*args, **kw):
        file2.path.unlink()
        return [Match(file1, file2, 100)]

    s._getmatches = getmatches

    assert not s.get_dupe_groups([file1, file2])


def test_folder_scan_exclude_subfolder_matches(fake_fileexists):
    # when doing a Folders scan type, don't include matches for folders whose parent folder already
    # match.
    s = Scanner()
    s.scan_type = ScanType.FOLDERS
    topf1 = no("top folder 1", size=42)
    topf1.digest = topf1.digest_partial = topf1.digest_samples = b"some_digest__1"
    topf1.path = Path("/topf1")
    topf2 = no("top folder 2", size=42)
    topf2.digest = topf2.digest_partial = topf2.digest_samples = b"some_digest__1"
    topf2.path = Path("/topf2")
    subf1 = no("sub folder 1", size=41)
    subf1.digest = subf1.digest_partial = subf1.digest_samples = b"some_digest__2"
    subf1.path = Path("/topf1/sub")
    subf2 = no("sub folder 2", size=41)
    subf2.digest = subf2.digest_partial = subf2.digest_samples = b"some_digest__2"
    subf2.path = Path("/topf2/sub")
    eq_(len(s.get_dupe_groups([topf1, topf2, subf1, subf2])), 1)  # only top folders
    # however, if another folder matches a subfolder, keep in in the matches
    otherf = no("other folder", size=41)
    otherf.digest = otherf.digest_partial = otherf.digest_samples = b"some_digest__2"
    otherf.path = Path("/otherfolder")
    eq_(len(s.get_dupe_groups([topf1, topf2, subf1, subf2, otherf])), 2)


def test_folder_scan_keeps_transitive_groups_in_linear_storage(fake_fileexists):
    scanner = Scanner()
    scanner.scan_type = ScanType.FOLDERS
    folders = [no("folder-{}".format(index), size=42) for index in range(2_000)]
    for folder in folders:
        folder.digest = b"same-recursive-manifest"

    [group] = scanner.get_dupe_groups(folders)

    assert len(group) == 2_000
    assert isinstance(group.matches, engine.ExactMatchesView)
    assert group.verification_kind is engine.VerificationKind.UNVERIFIED
    assert len(group.matches) == 1_999_000


def test_folder_scan_cross_pool_scope_requires_two_pools(fake_fileexists):
    scanner = Scanner()
    scanner.scan_type = ScanType.FOLDERS
    scanner.comparison_scope = "cross_pool"
    first, second = no("first", size=42), no("second", size=42)
    first.digest = second.digest = b"same-recursive-manifest"
    first.comparison_pool = second.comparison_pool = "incoming"

    assert scanner.get_dupe_groups([first, second]) == []

    second.comparison_pool = "protected"
    [group] = scanner.get_dupe_groups([first, second])
    assert set(group) == {first, second}


def test_ignore_files_with_same_path(fake_fileexists):
    # It's possible that the scanner is fed with two file instances pointing to the same path. One
    # of these files has to be ignored
    s = Scanner()
    f1 = no("foobar", path="path1/foobar")
    f2 = no("foobar", path="path1/foobar")
    eq_(s.get_dupe_groups([f1, f2]), [])


def test_dont_count_ref_files_as_discarded(fake_fileexists):
    # To speed up the scan, we don't bother comparing contents of files that are both ref files.
    # However, this causes problems in "discarded" counting and we make sure here that we don't
    # report discarded matches in exact duplicate scans.
    s = Scanner()
    s.scan_type = ScanType.CONTENTS
    o1 = no("foo", path="p1")
    o2 = no("foo", path="p2")
    o3 = no("foo", path="p3")
    o1.digest = o1.digest_partial = o1.digest_samples = "foobar"
    o2.digest = o2.digest_partial = o2.digest_samples = "foobar"
    o3.digest = o3.digest_partial = o3.digest_samples = "foobar"
    o1.is_ref = True
    o2.is_ref = True
    eq_(len(s.get_dupe_groups([o1, o2, o3])), 1)
    eq_(s.discarded_file_count, 0)


def test_prioritize_me(fake_fileexists):
    # in ScannerME, bitrate goes first (right after is_ref) in prioritization
    s = ScannerME()
    o1, o2 = no("foo", path="p1"), no("foo", path="p2")
    o1.bitrate = 1
    o2.bitrate = 2
    [group] = s.get_dupe_groups([o1, o2])
    assert group.ref is o2


def test_cross_pool_scope_ignores_duplicates_inside_one_pool(fake_fileexists):
    scanner = Scanner()
    scanner.comparison_scope = "cross_pool"
    first, second = no("same", path="first"), no("same", path="second")
    first.comparison_pool = "incoming"
    second.comparison_pool = "incoming"

    assert scanner.get_dupe_groups([first, second]) == []


def test_cross_pool_scope_keeps_incoming_to_protected_comparisons(fake_fileexists):
    scanner = Scanner()
    scanner.comparison_scope = "cross_pool"
    incoming, protected = no("same", path="incoming"), no("same", path="protected")
    incoming.comparison_pool = "incoming"
    protected.comparison_pool = "protected"
    protected.is_ref = True

    [group] = scanner.get_dupe_groups([incoming, protected])

    assert group.ref is protected
    assert group.dupes == [incoming]


def test_cross_pool_scope_applies_to_verified_exact_groups(fake_fileexists):
    scanner = Scanner()
    scanner.scan_type = ScanType.CONTENTS
    scanner.comparison_scope = "cross_pool"
    first, second = no("first"), no("second")
    first.digest_partial = second.digest_partial = b"partial"
    first.digest_samples = second.digest_samples = b"sample"
    first.digest = second.digest = b"full"
    first.comparison_pool = second.comparison_pool = "incoming"

    assert scanner.get_dupe_groups([first, second]) == []
