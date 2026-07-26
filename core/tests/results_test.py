# Copyright 2016 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import io
import os.path as op

import pytest
from xml.etree import ElementTree as ET

from pytest import raises
from hscommon.testutil import eq_
from hscommon.util import first
from core import engine
from core import results as results_module
from core import safe_xml as safe_xml_module
from core.destructive_eligibility import EligibilityCode, evaluate_duplicate
from core.tests.base import NamedObject, GetTestGroups, DupeGuru
from core.results import Results


def _saved_results_root():
    return ET.Element("results", schema_version="2", saved_at_ns="1")


def _add_saved_group(
    root,
    file_count,
    *,
    matches=(),
    relation="similar",
):
    attributes = {"relation": relation}
    if relation in {"verified_exact", "reported_exact"}:
        attributes.update(
            {
                "algorithm": "sha256",
                "digest": "01" * 32,
                "size": "1",
            }
        )
    group = ET.SubElement(root, "group", attributes)
    for index in range(file_count):
        ET.SubElement(
            group,
            "file",
            {
                "path": f"file-{len(root)}-{index}.bin",
                "words": f"file,{index}",
                "is_ref": "y" if index == 0 else "n",
                "marked": "y",
            },
        )
    for first_index, second_index, percentage in matches:
        ET.SubElement(
            group,
            "match",
            {
                "first": str(first_index),
                "second": str(second_index),
                "percentage": str(percentage),
            },
        )
    return group


def _saved_xml(root):
    return io.BytesIO(ET.tostring(root, encoding="utf-8"))


def _unique_file_resolver():
    files = {}

    def get_file(path):
        return files.setdefault(path, NamedObject(path, size=1))

    return get_file, files


class TestCaseResultsEmpty:
    def setup_method(self, method):
        self.app = DupeGuru()
        self.results = self.app.results

    def test_apply_invalid_filter(self):
        # If the applied filter is an invalid regexp, just ignore the filter.
        self.results.apply_filter("[")  # invalid
        self.test_stat_line()  # make sure that the stats line isn't saying we applied a '[' filter

    def test_stat_line(self):
        eq_("0 / 0 (0.00 B / 0.00 B) duplicates marked.", self.results.stat_line)

    def test_groups(self):
        eq_(0, len(self.results.groups))

    def test_get_group_of_duplicate(self):
        assert self.results.get_group_of_duplicate("foo") is None

    def test_save_to_xml(self):
        f = io.BytesIO()
        self.results.save_to_xml(f)
        f.seek(0)
        doc = ET.parse(f)
        root = doc.getroot()
        eq_("results", root.tag)

    def test_is_modified(self):
        assert not self.results.is_modified

    def test_is_modified_after_setting_empty_group(self):
        # Don't consider results as modified if they're empty
        self.results.groups = []
        assert not self.results.is_modified

    def test_save_to_same_name_as_folder(self, tmpdir):
        # Issue #149
        # When saving to a filename that already exists, the file is overwritten. However, when
        # the name exists but that it's a folder, then there used to be a crash. The proper fix
        # would have been some kind of feedback to the user, but the work involved for something
        # that simply never happens (I never received a report of this crash, I experienced it
        # while fooling around) is too much. Instead, use standard name conflict resolution.
        folderpath = tmpdir.join("foo")
        folderpath.mkdir()
        self.results.save_to_xml(str(folderpath))  # no crash
        assert tmpdir.join("[000] foo").check()


class TestCaseResultsWithSomeGroups:
    def setup_method(self, method):
        self.app = DupeGuru()
        self.results = self.app.results
        self.objects, self.matches, self.groups = GetTestGroups()
        self.results.groups = self.groups

    def test_stat_line(self):
        eq_("0 / 3 (0.00 B / 1.01 KB) duplicates marked.", self.results.stat_line)

    def test_groups(self):
        eq_(2, len(self.results.groups))

    def test_get_group_of_duplicate(self):
        for o in self.objects:
            g = self.results.get_group_of_duplicate(o)
            assert isinstance(g, engine.Group)
            assert o in g
        assert self.results.get_group_of_duplicate(self.groups[0]) is None

    def test_remove_duplicates(self):
        g1, g2 = self.results.groups
        self.results.remove_duplicates([g1.dupes[0]])
        eq_(2, len(g1))
        assert g1 in self.results.groups
        self.results.remove_duplicates([g1.ref])
        eq_(2, len(g1))
        assert g1 in self.results.groups
        self.results.remove_duplicates([g1.dupes[0]])
        eq_(0, len(g1))
        assert g1 not in self.results.groups
        self.results.remove_duplicates([g2.dupes[0]])
        eq_(0, len(g2))
        assert g2 not in self.results.groups
        eq_(0, len(self.results.groups))

    def test_remove_duplicates_with_ref_files(self):
        g1, g2 = self.results.groups
        self.objects[0].is_ref = True
        self.objects[1].is_ref = True
        self.results.remove_duplicates([self.objects[2]])
        eq_(0, len(g1))
        assert g1 not in self.results.groups

    def test_make_ref(self):
        g = self.results.groups[0]
        d = g.dupes[0]
        self.results.make_ref(d)
        assert d is g.ref

    def test_sort_groups(self):
        self.results.make_ref(self.objects[1])  # We want to make the 1024 sized object to go ref.
        g1, g2 = self.groups
        self.results.sort_groups("size")
        assert self.results.groups[0] is g2
        assert self.results.groups[1] is g1
        self.results.sort_groups("size", False)
        assert self.results.groups[0] is g1
        assert self.results.groups[1] is g2

    def test_set_groups_when_sorted(self):
        self.results.make_ref(self.objects[1])  # We want to make the 1024 sized object to go ref.
        self.results.sort_groups("size")
        objects, matches, groups = GetTestGroups()
        g1, g2 = groups
        g1.switch_ref(objects[1])
        self.results.groups = groups
        assert self.results.groups[0] is g2
        assert self.results.groups[1] is g1

    def test_get_dupe_list(self):
        eq_([self.objects[1], self.objects[2], self.objects[4]], self.results.dupes)

    def test_dupe_list_is_cached(self):
        assert self.results.dupes is self.results.dupes

    def test_dupe_list_cache_is_invalidated_when_needed(self):
        o1, o2, o3, o4, o5 = self.objects
        eq_([o2, o3, o5], self.results.dupes)
        self.results.make_ref(o2)
        eq_([o1, o3, o5], self.results.dupes)
        objects, matches, groups = GetTestGroups()
        o1, o2, o3, o4, o5 = objects
        self.results.groups = groups
        eq_([o2, o3, o5], self.results.dupes)

    def test_dupe_list_sort(self):
        o1, o2, o3, o4, o5 = self.objects
        o1.size = 5
        o2.size = 4
        o3.size = 3
        o4.size = 2
        o5.size = 1
        self.results.sort_dupes("size")
        eq_([o5, o3, o2], self.results.dupes)
        self.results.sort_dupes("size", False)
        eq_([o2, o3, o5], self.results.dupes)

    def test_dupe_list_remember_sort(self):
        o1, o2, o3, o4, o5 = self.objects
        o1.size = 5
        o2.size = 4
        o3.size = 3
        o4.size = 2
        o5.size = 1
        self.results.sort_dupes("size")
        self.results.make_ref(o2)
        eq_([o5, o3, o1], self.results.dupes)

    def test_dupe_list_sort_delta_values(self):
        o1, o2, o3, o4, o5 = self.objects
        o1.size = 10
        o2.size = 2  # -8
        o3.size = 3  # -7
        o4.size = 20
        o5.size = 1  # -19
        self.results.sort_dupes("size", delta=True)
        eq_([o5, o2, o3], self.results.dupes)

    def test_sort_empty_list(self):
        # There was an infinite loop when sorting an empty list.
        app = DupeGuru()
        r = app.results
        r.sort_dupes("name")
        eq_([], r.dupes)

    def test_dupe_list_update_on_remove_duplicates(self):
        o1, o2, o3, o4, o5 = self.objects
        eq_(3, len(self.results.dupes))
        self.results.remove_duplicates([o2])
        eq_(2, len(self.results.dupes))

    def test_exact_group_batch_removal_rebuilds_each_container_once_at_100k_scale(
        self,
        monkeypatch,
    ):
        class SyntheticFile:
            __slots__ = ("is_ref", "size")

            def __init__(self):
                self.size = 1
                self.is_ref = False

        class CountingOrdered(list):
            def __init__(self, values):
                super().__init__(values)
                self.iterations = 0

            def __iter__(self):
                self.iterations += 1
                return super().__iter__()

            def remove(self, _item):
                raise AssertionError("batch removal must not call ordered.remove")

        class CountingUnordered(set):
            def __init__(self, values):
                super().__init__(values)
                self.difference_updates = 0

            def difference_update(self, *others):
                self.difference_updates += 1
                return super().difference_update(*others)

            def remove(self, _item):
                raise AssertionError("batch removal must not call unordered.remove")

        files = [SyntheticFile() for _ in range(100_000)]
        evidence = engine.ExactEvidence(
            kind=engine.VerificationKind.VERIFIED_EXACT,
            algorithm="test",
            digest=b"digest",
            size=1,
        )
        group = engine.Group.from_exact_files(files, evidence)
        results = Results(object())
        results.groups = [group]
        results.mark_all()
        tracked_ordered = CountingOrdered(group.ordered)
        tracked_unordered = CountingUnordered(group.unordered)
        group.ordered = tracked_ordered
        group.unordered = tracked_unordered

        def fail_single_remove(*_args, **_kwargs):
            raise AssertionError("Results must use the group batch-removal API")

        monkeypatch.setattr(engine.Group, "remove_dupe", fail_single_remove)

        results.remove_duplicates(files[1:])

        assert tracked_ordered.iterations == 1
        assert tracked_unordered.difference_updates == 1
        assert len(group) == 0
        assert results.groups == []
        assert results.dupes == []
        assert results.get_group_of_duplicate(files[0]) is None
        assert results.get_group_of_duplicate(files[-1]) is None
        assert results.mark_count == 0
        assert results.stat_line == "0 / 0 (0.00 B / 0.00 B) duplicates marked."

    def test_is_modified(self):
        # Changing the groups sets the modified flag
        assert self.results.is_modified

    def test_is_modified_after_save_and_load(self):
        # Saving/Loading a file sets the modified flag back to False
        def get_file(path):
            return [f for f in self.objects if str(f.path) == path][0]

        f = io.BytesIO()
        self.results.save_to_xml(f)
        assert not self.results.is_modified
        self.results.groups = self.groups  # sets the flag back
        f.seek(0)
        self.results.load_from_xml(f, get_file)
        assert not self.results.is_modified

    def test_is_modified_after_removing_all_results(self):
        # Removing all results sets the is_modified flag to false.
        self.results.mark_all()
        self.results.perform_on_marked(lambda x: None, True)
        assert not self.results.is_modified

    def test_group_of_duplicate_after_removal(self):
        # removing a duplicate also removes it from the dupe:group map.
        dupe = self.results.groups[1].dupes[0]
        ref = self.results.groups[1].ref
        self.results.remove_duplicates([dupe])
        assert self.results.get_group_of_duplicate(dupe) is None
        # also remove group ref
        assert self.results.get_group_of_duplicate(ref) is None

    def test_dupe_list_sort_delta_values_nonnumeric(self):
        # When sorting dupes in delta mode on a non-numeric column, our first sort criteria is if
        # the string is the same as its ref.
        g1r, g1d1, g1d2, g2r, g2d1 = self.objects
        # "aaa" makes our dupe go first in alphabetical order, but since we have the same value as
        # ref, we're going last.
        g2r.name = g2d1.name = "aaa"
        self.results.sort_dupes("name", delta=True)
        eq_("aaa", self.results.dupes[2].name)

    def test_dupe_list_sort_delta_values_nonnumeric_case_insensitive(self):
        # Non-numeric delta sorting comparison is case insensitive
        g1r, g1d1, g1d2, g2r, g2d1 = self.objects
        g2r.name = "AaA"
        g2d1.name = "aAa"
        self.results.sort_dupes("name", delta=True)
        eq_("aAa", self.results.dupes[2].name)


class TestCaseResultsWithSavedResults:
    def setup_method(self, method):
        self.app = DupeGuru()
        self.results = self.app.results
        self.objects, self.matches, self.groups = GetTestGroups()
        self.results.groups = self.groups
        self.f = io.BytesIO()
        self.results.save_to_xml(self.f)
        self.f.seek(0)

    def test_is_modified(self):
        # Saving a file sets the modified flag back to False
        assert not self.results.is_modified

    def test_is_modified_after_load(self):
        # Loading a file sets the modified flag back to False
        def get_file(path):
            return [f for f in self.objects if str(f.path) == path][0]

        self.results.groups = self.groups  # sets the flag back
        self.results.load_from_xml(self.f, get_file)
        assert not self.results.is_modified

    def test_is_modified_after_remove(self):
        # Removing dupes sets the modified flag
        self.results.remove_duplicates([self.results.groups[0].dupes[0]])
        assert self.results.is_modified

    def test_is_modified_after_make_ref(self):
        # Making a dupe ref sets the modified flag
        self.results.make_ref(self.results.groups[0].dupes[0])
        assert self.results.is_modified


class TestCaseResultsMarkings:
    def setup_method(self, method):
        self.app = DupeGuru()
        self.results = self.app.results
        self.objects, self.matches, self.groups = GetTestGroups()
        self.results.groups = self.groups

    def test_stat_line(self):
        eq_("0 / 3 (0.00 B / 1.01 KB) duplicates marked.", self.results.stat_line)
        self.results.mark(self.objects[1])
        eq_("1 / 3 (1.00 KB / 1.01 KB) duplicates marked.", self.results.stat_line)
        self.results.mark_invert()
        eq_("2 / 3 (2.00 B / 1.01 KB) duplicates marked.", self.results.stat_line)
        self.results.mark_invert()
        self.results.unmark(self.objects[1])
        self.results.mark(self.objects[2])
        self.results.mark(self.objects[4])
        eq_("2 / 3 (2.00 B / 1.01 KB) duplicates marked.", self.results.stat_line)
        self.results.mark(self.objects[0])  # this is a ref, it can't be counted
        eq_("2 / 3 (2.00 B / 1.01 KB) duplicates marked.", self.results.stat_line)
        self.results.groups = self.groups
        eq_("0 / 3 (0.00 B / 1.01 KB) duplicates marked.", self.results.stat_line)

    def test_with_ref_duplicate(self):
        self.objects[1].is_ref = True
        self.results.groups = self.groups
        assert not self.results.mark(self.objects[1])
        self.results.mark(self.objects[2])
        eq_("1 / 2 (1.00 B / 2.00 B) duplicates marked.", self.results.stat_line)

    def test_perform_on_marked(self):
        def log_object(o):
            log.append(o)
            return True

        log = []
        self.results.mark_all()
        self.results.perform_on_marked(log_object, False)
        assert self.objects[1] in log
        assert self.objects[2] in log
        assert self.objects[4] in log
        eq_(3, len(log))
        log = []
        self.results.mark_none()
        self.results.mark(self.objects[4])
        self.results.perform_on_marked(log_object, True)
        eq_(1, len(log))
        assert self.objects[4] in log
        eq_(1, len(self.results.groups))

    def test_perform_on_marked_with_problems(self):
        def log_object(o):
            log.append(o)
            if o is self.objects[1]:
                raise OSError("foobar")

        log = []
        self.results.mark_all()
        assert self.results.is_marked(self.objects[1])
        self.results.perform_on_marked(log_object, True)
        eq_(len(log), 3)
        eq_(len(self.results.groups), 1)
        eq_(len(self.results.groups[0]), 2)
        assert self.objects[1] in self.results.groups[0]
        assert not self.results.is_marked(self.objects[2])
        assert self.results.is_marked(self.objects[1])
        eq_(len(self.results.problems), 1)
        dupe, msg = self.results.problems[0]
        assert dupe is self.objects[1]
        eq_(msg, "foobar")

    def test_perform_on_marked_with_ref(self):
        def log_object(o):
            log.append(o)
            return True

        log = []
        self.objects[0].is_ref = True
        self.objects[1].is_ref = True
        self.results.mark_all()
        self.results.perform_on_marked(log_object, True)
        assert self.objects[1] not in log
        assert self.objects[2] in log
        assert self.objects[4] in log
        eq_(2, len(log))
        eq_(0, len(self.results.groups))

    def test_perform_on_marked_remove_objects_only_at_the_end(self):
        def check_groups(o):
            eq_(3, len(g1))
            eq_(2, len(g2))
            return True

        g1, g2 = self.results.groups
        self.results.mark_all()
        self.results.perform_on_marked(check_groups, True)
        eq_(0, len(g1))
        eq_(0, len(g2))
        eq_(0, len(self.results.groups))

    def test_remove_duplicates(self):
        g1 = self.results.groups[0]
        self.results.mark(g1.dupes[0])
        eq_("1 / 3 (1.00 KB / 1.01 KB) duplicates marked.", self.results.stat_line)
        self.results.remove_duplicates([g1.dupes[1]])
        eq_("1 / 2 (1.00 KB / 1.01 KB) duplicates marked.", self.results.stat_line)
        self.results.remove_duplicates([g1.dupes[0]])
        eq_("0 / 1 (0.00 B / 1.00 B) duplicates marked.", self.results.stat_line)

    def test_make_ref(self):
        g = self.results.groups[0]
        d = g.dupes[0]
        self.results.mark(d)
        eq_("1 / 3 (1.00 KB / 1.01 KB) duplicates marked.", self.results.stat_line)
        self.results.make_ref(d)
        eq_("0 / 3 (0.00 B / 3.00 B) duplicates marked.", self.results.stat_line)
        self.results.make_ref(d)
        eq_("0 / 3 (0.00 B / 3.00 B) duplicates marked.", self.results.stat_line)

    def test_save_xml(self):
        self.results.mark(self.objects[1])
        self.results.mark_invert()
        f = io.BytesIO()
        self.results.save_to_xml(f)
        f.seek(0)
        doc = ET.parse(f)
        root = doc.getroot()
        g1, g2 = root.iter("group")
        d1, d2, d3 = g1.iter("file")
        eq_("n", d1.get("marked"))
        eq_("n", d2.get("marked"))
        eq_("y", d3.get("marked"))
        d1, d2 = g2.iter("file")
        eq_("n", d1.get("marked"))
        eq_("y", d2.get("marked"))

    def test_load_xml(self):
        def get_file(path):
            return [f for f in self.objects if str(f.path) == path][0]

        self.objects[4].name = "ibabtu 2"  # we can't have 2 files with the same path
        self.results.mark(self.objects[1])
        self.results.mark_invert()
        f = io.BytesIO()
        self.results.save_to_xml(f)
        f.seek(0)
        app = DupeGuru()
        r = Results(app)
        r.load_from_xml(f, get_file)
        assert not r.is_marked(self.objects[0])
        assert not r.is_marked(self.objects[1])
        assert not r.is_marked(self.objects[2])
        assert not r.is_marked(self.objects[3])
        assert not r.is_marked(self.objects[4])
        assert r.loaded_report
        assert r.loaded_schema_version == 3


class TestCaseResultsXML:
    def setup_method(self, method):
        self.app = DupeGuru()
        self.results = self.app.results
        self.objects, self.matches, self.groups = GetTestGroups()
        self.results.groups = self.groups

    def get_file(self, path):  # use this as a callback for load_from_xml
        return [o for o in self.objects if str(o.path) == path][0]

    def test_save_to_xml(self):
        self.objects[0].is_ref = True
        self.objects[0].words = [["foo", "bar"]]
        f = io.BytesIO()
        self.results.save_to_xml(f)
        f.seek(0)
        doc = ET.parse(f)
        root = doc.getroot()
        eq_("results", root.tag)
        eq_(2, len(root))
        eq_(2, len([c for c in root if c.tag == "group"]))
        g1, g2 = root
        eq_(6, len(g1))
        eq_(3, len([c for c in g1 if c.tag == "file"]))
        eq_(3, len([c for c in g1 if c.tag == "match"]))
        d1, d2, d3 = (c for c in g1 if c.tag == "file")
        eq_(op.join("basepath", "foo bar"), d1.get("path"))
        eq_(op.join("basepath", "bar bleh"), d2.get("path"))
        eq_(op.join("basepath", "foo bleh"), d3.get("path"))
        eq_("y", d1.get("is_ref"))
        eq_("n", d2.get("is_ref"))
        eq_("n", d3.get("is_ref"))
        eq_("foo,bar", d1.get("words"))
        eq_("bar,bleh", d2.get("words"))
        eq_("foo,bleh", d3.get("words"))
        eq_(3, len(g2))
        eq_(2, len([c for c in g2 if c.tag == "file"]))
        eq_(1, len([c for c in g2 if c.tag == "match"]))
        d1, d2 = (c for c in g2 if c.tag == "file")
        eq_(op.join("basepath", "ibabtu"), d1.get("path"))
        eq_(op.join("basepath", "ibabtu"), d2.get("path"))
        eq_("n", d1.get("is_ref"))
        eq_("n", d2.get("is_ref"))
        eq_("ibabtu", d1.get("words"))
        eq_("ibabtu", d2.get("words"))

    def test_save_streams_without_materializing_an_element_tree(
        self,
        monkeypatch,
    ):
        def forbidden_sub_element(*_args, **_kwargs):
            raise AssertionError("Results save must not construct an ElementTree")

        monkeypatch.setattr(ET, "SubElement", forbidden_sub_element)
        output = io.BytesIO()

        self.results.save_to_xml(output)

        assert output.getvalue().startswith(b"<?xml version='1.0'")
        assert not self.results.is_modified

    def test_save_roundtrip_is_byte_exact_and_preserves_xml_attributes(
        self,
        monkeypatch,
    ):
        first = NamedObject(' first\tline\nreturn\r&<>"\u00e9 ', size=1)
        second = NamedObject("second-\u65e5\u672c\u8a9e", size=1)
        first.words = [' word\tline\nreturn\r&<>"\u00e9 ']
        second.words = ["\u65e5\u672c\u8a9e"]
        group = engine.Group()
        group.add_match(engine.Match(first, second, 91))
        saved = Results(DupeGuru())
        saved.groups = [group]
        monkeypatch.setattr(results_module.time, "time_ns", lambda: 123456789)
        first_output = io.BytesIO()

        saved.save_to_xml(first_output)

        payload = first_output.getvalue()
        assert b"&#09;" in payload
        assert b"&#10;" in payload
        assert b"&#13;" in payload
        assert b"&amp;" in payload
        assert b"&lt;" in payload
        assert b"&gt;" in payload
        assert b"&quot;" in payload
        assert "\u65e5\u672c\u8a9e".encode() in payload
        root = ET.fromstring(payload)
        file_elements = list(root.iter("file"))
        assert file_elements[0].get("path") == str(first.path)
        assert file_elements[0].get("words") == first.words[0]

        by_path = {str(item.path): item for item in (first, second)}
        loaded = Results(DupeGuru())
        loaded.load_from_xml(io.BytesIO(payload), by_path.get)
        second_output = io.BytesIO()
        loaded.save_to_xml(second_output)

        assert second_output.getvalue() == payload

    def test_compact_exact_save_uses_only_linear_membership_work(self):
        class HashCountingObject(NamedObject):
            hash_calls = 0

            def __hash__(self):
                type(self).hash_calls += 1
                return object.__hash__(self)

        file_count = 2_000
        objects = [HashCountingObject("exact-{}.bin".format(index), size=1) for index in range(file_count)]
        evidence = engine.ExactEvidence(
            kind=engine.VerificationKind.VERIFIED_EXACT,
            algorithm="sha256",
            digest=b"\x09" * 32,
            size=1,
        )
        results = Results(DupeGuru())
        results.groups = [engine.Group.from_exact_files(objects, evidence)]
        HashCountingObject.hash_calls = 0

        results.save_to_xml(io.BytesIO())

        # Preflight and serialization each use one uniqueness set, and mark
        # lookup hashes each non-reference file at most twice.
        assert HashCountingObject.hash_calls <= 4 * file_count

    def test_advertised_compact_exact_group_streams_250k_files(
        self,
        monkeypatch,
    ):
        class VirtualFile:
            __slots__ = ("is_ref", "path")

            def __init__(self, index):
                self.path = "file-{}.bin".format(index)
                self.is_ref = index == 0

        class VirtualCompactGroup:
            _is_exact = True
            compact_relation = engine.VerificationKind.VERIFIED_EXACT.value
            verification_kind = engine.VerificationKind.VERIFIED_EXACT
            evidence = engine.ExactEvidence(
                kind=engine.VerificationKind.VERIFIED_EXACT,
                algorithm="sha256",
                digest=b"\x08" * 32,
                size=1,
            )

            def __init__(self, count):
                self.count = count

            def __len__(self):
                return self.count

            def __iter__(self):
                return (VirtualFile(index) for index in range(self.count))

        class CountingStream:
            def __init__(self):
                self.bytes_written = 0

            def write(self, payload):
                self.bytes_written += len(payload)
                return len(payload)

        def forbidden_sub_element(*_args, **_kwargs):
            raise AssertionError("Results save must not construct ElementTree nodes")

        monkeypatch.setattr(ET, "SubElement", forbidden_sub_element)
        group = VirtualCompactGroup(results_module.MAX_RESULTS_FILES_PER_GROUP)
        groups = [group]
        root_attributes = {
            "schema_version": str(results_module.RESULTS_SCHEMA_VERSION),
            "saved_at_ns": "1",
            "destructive_proof": "requires_live_reverification",
        }

        expected_bytes = results_module._validate_results_save_contract(
            groups,
            root_attributes,
        )
        output = CountingStream()
        results_module._write_results_xml(
            output,
            groups,
            root_attributes,
            lambda _file: False,
            expected_bytes,
        )

        assert output.bytes_written == expected_bytes

    def test_invalid_surrogate_does_not_touch_existing_path_destination(
        self,
        tmp_path,
    ):
        self.objects[0].name = "invalid-\udcff"
        destination = tmp_path / "results.dupeguru"
        destination.write_bytes(b"existing")

        with raises(ValueError, match="XML 1.0"):
            self.results.save_to_xml(destination)

        assert destination.read_bytes() == b"existing"
        assert not list(tmp_path.glob(".results.dupeguru.*.tmp"))
        assert self.results.is_modified

    def test_mid_stream_failure_preserves_path_and_removes_temporary_file(
        self,
        monkeypatch,
        tmp_path,
    ):
        destination = tmp_path / "results.dupeguru"
        destination.write_bytes(b"existing")
        original_serializer = results_module._xml_element_bytes
        calls = 0

        def fail_after_root(tag, attributes, *, empty):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated streaming failure")
            return original_serializer(tag, attributes, empty=empty)

        monkeypatch.setattr(
            results_module,
            "_xml_element_bytes",
            fail_after_root,
        )
        with raises(OSError, match="simulated streaming failure"):
            self.results.save_to_xml(destination)

        assert destination.read_bytes() == b"existing"
        assert not list(tmp_path.glob(".results.dupeguru.*.tmp"))
        assert self.results.is_modified

    def test_file_like_destination_is_direct_and_may_remain_partial_on_failure(
        self,
        monkeypatch,
    ):
        original_serializer = results_module._xml_element_bytes
        calls = 0

        def fail_after_root(tag, attributes, *, empty):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated direct-stream failure")
            return original_serializer(tag, attributes, empty=empty)

        monkeypatch.setattr(
            results_module,
            "_xml_element_bytes",
            fail_after_root,
        )
        output = io.BytesIO()
        with raises(OSError, match="simulated direct-stream failure"):
            self.results.save_to_xml(output)

        assert output.getvalue().startswith(b"<?xml version='1.0' encoding='utf-8'?>\n<results")
        assert not output.getvalue().endswith(b"</results>")
        assert self.results.is_modified

    def test_memory_error_during_preflight_is_translated_without_writing(
        self,
        monkeypatch,
    ):
        def fail_preflight(*_args, **_kwargs):
            raise MemoryError

        monkeypatch.setattr(
            results_module,
            "_validate_results_save_contract",
            fail_preflight,
        )
        output = io.BytesIO()

        with raises(ValueError, match="memory"):
            self.results.save_to_xml(output)

        assert output.getvalue() == b""
        assert self.results.is_modified

    def test_memory_error_during_serialization_is_translated_and_atomic(
        self,
        monkeypatch,
        tmp_path,
    ):
        destination = tmp_path / "results.dupeguru"
        destination.write_bytes(b"existing")

        def fail_serialization(*_args, **_kwargs):
            raise MemoryError

        monkeypatch.setattr(
            results_module,
            "_write_results_xml",
            fail_serialization,
        )
        with raises(ValueError, match="memory"):
            self.results.save_to_xml(destination)

        assert destination.read_bytes() == b"existing"
        assert not list(tmp_path.glob(".results.dupeguru.*.tmp"))
        assert self.results.is_modified

    def test_load_xml(self):
        def get_file(path):
            return [f for f in self.objects if str(f.path) == path][0]

        self.objects[0].is_ref = True
        self.objects[4].name = "ibabtu 2"  # we can't have 2 files with the same path
        f = io.BytesIO()
        self.results.save_to_xml(f)
        f.seek(0)
        app = DupeGuru()
        r = Results(app)
        r.load_from_xml(f, get_file)
        eq_(2, len(r.groups))
        g1, g2 = r.groups
        eq_(3, len(g1))
        assert g1[0].is_ref
        assert not g1[1].is_ref
        assert not g1[2].is_ref
        assert g1[0] is self.objects[0]
        assert g1[1] is self.objects[1]
        assert g1[2] is self.objects[2]
        eq_(["foo", "bar"], g1[0].words)
        eq_(["bar", "bleh"], g1[1].words)
        eq_(["foo", "bleh"], g1[2].words)
        eq_(2, len(g2))
        assert not g2[0].is_ref
        assert not g2[1].is_ref
        assert g2[0] is self.objects[3]
        assert g2[1] is self.objects[4]
        eq_(["ibabtu"], g2[0].words)
        eq_(["ibabtu"], g2[1].words)

    def test_load_xml_with_filename(self, tmpdir):
        def get_file(path):
            return [f for f in self.objects if str(f.path) == path][0]

        filename = str(tmpdir.join("dupeguru_results.xml"))
        self.objects[4].name = "ibabtu 2"  # we can't have 2 files with the same path
        self.results.save_to_xml(filename)
        app = DupeGuru()
        r = Results(app)
        r.load_from_xml(filename, get_file)
        eq_(2, len(r.groups))

    def test_load_xml_with_some_files_that_dont_exist_anymore(self):
        def get_file(path):
            if path.endswith("ibabtu 2"):
                return None
            return [f for f in self.objects if str(f.path) == path][0]

        self.objects[4].name = "ibabtu 2"  # we can't have 2 files with the same path
        f = io.BytesIO()
        self.results.save_to_xml(f)
        f.seek(0)
        app = DupeGuru()
        r = Results(app)
        r.load_from_xml(f, get_file)
        eq_(1, len(r.groups))
        eq_(3, len(r.groups[0]))

    def test_streaming_parser_detaches_files_with_one_byte_input_chunks(
        self,
        monkeypatch,
    ):
        file_count = 5_000
        payload = (
            b'<results schema_version="3" saved_at_ns="1">'
            b'<group relation="reported_exact" algorithm="sha256" digest="'
            + b"01" * 32
            + b'" size="1">'
            + b"".join(b'<file path="file-%d.bin" />' % index for index in range(file_count))
            + b"</group></results>"
        )
        original_release = results_module._release_results_xml_element
        released_files = 0
        max_group_children = 0

        def track_release(parent, element):
            nonlocal max_group_children, released_files
            if parent is not None and parent.tag == "group":
                max_group_children = max(max_group_children, len(parent))
            if element.tag == "file":
                released_files += 1
            original_release(parent, element)

        monkeypatch.setattr(safe_xml_module, "_XML_READ_CHUNK_BYTES", 1)
        monkeypatch.setattr(
            results_module,
            "_release_results_xml_element",
            track_release,
        )

        document = results_module._parse_saved_results(io.BytesIO(payload))

        assert len(document.groups) == 1
        assert len(document.groups[0].files) == file_count
        assert released_files == file_count
        # The prior file end is retained only until the next parser event so
        # its tail is final. It and the next file are the only siblings alive.
        assert max_group_children <= 2

    @pytest.mark.parametrize("tail", [" \r\n\t ", "not-whitespace"])
    def test_streaming_parser_finalizes_tail_across_one_byte_chunks(
        self,
        monkeypatch,
        tail,
    ):
        payload = (
            '<results schema_version="3" saved_at_ns="1">'
            '<group relation="reported_exact" algorithm="sha256" '
            'digest="{}" size="1">'
            '<file path="one"/>{}<file path="two"/>'
            "</group></results>"
        ).format("01" * 32, tail)
        monkeypatch.setattr(safe_xml_module, "_XML_READ_CHUNK_BYTES", 1)

        if tail.isspace():
            document = results_module._parse_saved_results(io.BytesIO(payload.encode()))
            assert len(document.groups[0].files) == 2
        else:
            with raises(ValueError, match="tail text"):
                results_module._parse_saved_results(io.BytesIO(payload.encode()))

    @pytest.mark.parametrize(
        "payload",
        [
            b"<foobar/>",
            b"<results><wrapper><group/></wrapper></results>",
            (b"<results><group><file path='one'/><file path='two'/>" b"<wrapper/></group></results>"),
            (b"<results><group><file path='one'><file path='nested'/>" b"</file><file path='two'/></group></results>"),
        ],
    )
    def test_load_rejects_noncanonical_or_nested_structure(self, payload):
        calls = []

        with raises(ValueError):
            Results(DupeGuru()).load_from_xml(
                io.BytesIO(payload),
                lambda path: calls.append(path),
            )

        assert calls == []

    def test_xml_non_ascii(self):
        def get_file(path):
            if path == op.join("basepath", "\xe9foo bar"):
                return objects[0]
            if path == op.join("basepath", "bar bleh"):
                return objects[1]

        objects = [NamedObject("\xe9foo bar", True), NamedObject("bar bleh", True)]
        matches = engine.getmatches(objects)  # we should have 5 matches
        groups = engine.get_groups(matches)  # We should have 2 groups
        for g in groups:
            g.prioritize(lambda x: objects.index(x))  # We want the dupes to be in the same order as the list is
        app = DupeGuru()
        results = Results(app)
        results.groups = groups
        f = io.BytesIO()
        results.save_to_xml(f)
        f.seek(0)
        app = DupeGuru()
        r = Results(app)
        r.load_from_xml(f, get_file)
        g = r.groups[0]
        eq_("\xe9foo bar", g[0].name)
        eq_(["efoo", "bar"], g[0].words)

    def test_load_invalid_xml(self):
        f = io.BytesIO()
        f.write(b"<this is invalid")
        f.seek(0)
        app = DupeGuru()
        r = Results(app)
        with raises(ET.ParseError):
            r.load_from_xml(f, None)
        eq_(0, len(r.groups))

    def test_load_non_existant_xml(self):
        app = DupeGuru()
        r = Results(app)
        with raises(IOError):
            r.load_from_xml("does_not_exist.xml", None)
        eq_(0, len(r.groups))

    def test_saved_exact_group_is_compact_and_requires_live_reverification(self):
        objects = [
            NamedObject("one.bin", size=4),
            NamedObject("two.bin", size=4),
            NamedObject("three.bin", size=4),
        ]
        evidence = engine.ExactEvidence(
            kind=engine.VerificationKind.VERIFIED_EXACT,
            algorithm="sha256",
            digest=b"\x01" * 32,
            size=4,
        )
        group = engine.Group.from_exact_files(objects, evidence)
        results = Results(DupeGuru())
        results.groups = [group]
        output = io.BytesIO()

        results.save_to_xml(output)
        output.seek(0)
        root = ET.parse(output).getroot()
        group_node = next(root.iter("group"))
        eq_("3", root.get("schema_version"))
        eq_("verified_exact", group_node.get("relation"))
        eq_(0, len(list(group_node.iter("match"))))

        output.seek(0)
        loaded = Results(DupeGuru())
        loaded.load_from_xml(
            output,
            lambda path: next(item for item in objects if str(item.path) == path),
        )
        eq_(1, len(loaded.groups))
        eq_(3, len(loaded.groups[0]))
        assert loaded.groups[0]._is_exact
        assert loaded.groups[0].verification_kind is engine.VerificationKind.UNVERIFIED
        assert loaded.groups[0].evidence.kind is engine.VerificationKind.UNVERIFIED
        assert loaded.loaded_report

    def test_save_rejects_per_group_match_overflow_before_writing(self, monkeypatch):
        objects = [NamedObject("same") for _ in range(3)]
        group = engine.get_groups(engine.getmatches(objects))[0]
        saved = Results(DupeGuru())
        saved.groups = [group]
        output = io.BytesIO()
        monkeypatch.setattr(results_module, "MAX_RESULTS_MATCHES_PER_GROUP", 2)

        with raises(ValueError, match="per-group match count"):
            saved.save_to_xml(output)

        assert output.getvalue() == b""

    def test_save_rejects_per_group_file_overflow_before_writing(self, monkeypatch):
        objects = [NamedObject("same-{}".format(index), size=4) for index in range(3)]
        evidence = engine.ExactEvidence(
            kind=engine.VerificationKind.VERIFIED_EXACT,
            algorithm="sha256",
            digest=b"\x02" * 32,
            size=4,
        )
        saved = Results(DupeGuru())
        saved.groups = [engine.Group.from_exact_files(objects, evidence)]
        output = io.BytesIO()
        monkeypatch.setattr(results_module, "MAX_RESULTS_FILES_PER_GROUP", 2)

        with raises(ValueError, match="per-group file count"):
            saved.save_to_xml(output)

        assert output.getvalue() == b""

    def test_save_rejects_total_match_overflow_before_writing(self, monkeypatch):
        objects = [NamedObject("same-{}".format(index)) for index in range(4)]
        first = engine.Group()
        first.add_match(engine.Match(objects[0], objects[1], 100))
        second = engine.Group()
        second.add_match(engine.Match(objects[2], objects[3], 100))
        saved = Results(DupeGuru())
        saved.groups = [first, second]
        output = io.BytesIO()
        monkeypatch.setattr(results_module, "MAX_RESULTS_TOTAL_MATCHES", 1)

        with raises(ValueError, match="total match count"):
            saved.save_to_xml(output)

        assert output.getvalue() == b""

    def test_save_rejects_byte_overflow_before_writing(self, monkeypatch):
        objects = [NamedObject("same-{}".format(index), size=4) for index in range(2)]
        evidence = engine.ExactEvidence(
            kind=engine.VerificationKind.VERIFIED_EXACT,
            algorithm="sha256",
            digest=b"\x03" * 32,
            size=4,
        )
        saved = Results(DupeGuru())
        saved.groups = [engine.Group.from_exact_files(objects, evidence)]
        output = io.BytesIO()
        monkeypatch.setattr(results_module, "MAX_RESULTS_XML_BYTES", 64)

        with raises(ValueError, match="byte save limit"):
            saved.save_to_xml(output)

        assert output.getvalue() == b""

    def test_save_rejects_xml_unrepresentable_path_before_writing(self):
        first = NamedObject("valid", size=4)
        second = NamedObject("invalid-\udcff", size=4)
        evidence = engine.ExactEvidence(
            kind=engine.VerificationKind.VERIFIED_EXACT,
            algorithm="sha256",
            digest=b"\x04" * 32,
            size=4,
        )
        saved = Results(DupeGuru())
        saved.groups = [engine.Group.from_exact_files([first, second], evidence)]
        output = io.BytesIO()

        with raises(ValueError, match="XML 1.0"):
            saved.save_to_xml(output)

        assert output.getvalue() == b""

    def test_failed_save_preserves_active_filter(self, monkeypatch):
        objects = [NamedObject("same-{}".format(index), size=4) for index in range(2)]
        evidence = engine.ExactEvidence(
            kind=engine.VerificationKind.VERIFIED_EXACT,
            algorithm="sha256",
            digest=b"\x05" * 32,
            size=4,
        )
        saved = Results(DupeGuru())
        saved.groups = [engine.Group.from_exact_files(objects, evidence)]
        saved.apply_filter("same-0")
        filtered_before = list(saved.groups)
        monkeypatch.setattr(results_module, "MAX_RESULTS_XML_BYTES", 64)

        with raises(ValueError, match="byte save limit"):
            saved.save_to_xml(io.BytesIO())

        assert list(saved.groups) == filtered_before
        assert "filter: same-0" in saved.stat_line

    def test_saved_folder_manifest_group_is_compact_and_review_only(
        self,
        monkeypatch,
    ):
        objects = [NamedObject("folder-{}".format(index), size=4) for index in range(5_000)]
        group = engine.Group.from_unverified_transitive_files(
            objects,
            relation="folder_manifest",
        )
        results = Results(DupeGuru())
        results.groups = [group]
        output = io.BytesIO()

        results.save_to_xml(output)
        output.seek(0)
        root = ET.parse(output).getroot()
        group_node = next(root.iter("group"))
        assert root.get("schema_version") == "3"
        assert group_node.get("relation") == "folder_manifest"
        assert group_node.get("algorithm") is None
        assert group_node.get("digest") is None
        assert group_node.get("size") is None
        assert not list(group_node.iter("match"))

        def forbidden_match(*_args):
            raise AssertionError("Compact folder-manifest load must not construct pair matches")

        monkeypatch.setattr(results_module.engine, "Match", forbidden_match)
        output.seek(0)
        loaded = Results(DupeGuru())
        by_path = {str(item.path): item for item in objects}
        loaded.load_from_xml(
            output,
            by_path.get,
        )

        [loaded_group] = loaded.groups
        assert len(loaded_group) == 5_000
        assert loaded_group.compact_relation == "folder_manifest"
        assert loaded_group.verification_kind is engine.VerificationKind.UNVERIFIED
        assert loaded_group.evidence is None
        assert loaded.loaded_report

    def test_load_rejects_unknown_results_schema(self):
        payload = io.BytesIO(b'<results schema_version="999"/>')
        with raises(ValueError, match="Unsupported results schema version"):
            Results(DupeGuru()).load_from_xml(payload, lambda path: None)

    def test_load_rejects_dtd_and_entities(self):
        payload = io.BytesIO(b'<!DOCTYPE results [<!ENTITY secret "expanded">]><results>&secret;</results>')
        with raises(ValueError, match="DTD and entity"):
            Results(DupeGuru()).load_from_xml(payload, lambda path: None)

    def test_group_count_boundary_and_overflow_precede_file_resolution(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(results_module, "MAX_RESULTS_GROUPS", 2)
        resolver, _ = _unique_file_resolver()
        boundary = _saved_results_root()
        _add_saved_group(boundary, 2, relation="reported_exact")
        _add_saved_group(boundary, 2, relation="reported_exact")

        loaded = Results(DupeGuru())
        loaded.load_from_xml(_saved_xml(boundary), resolver)
        eq_(2, len(loaded.groups))

        overflow = _saved_results_root()
        for _ in range(3):
            _add_saved_group(overflow, 2, relation="reported_exact")
        calls = []
        with raises(ValueError, match="group count"):
            Results(DupeGuru()).load_from_xml(
                _saved_xml(overflow),
                lambda path: calls.append(path),
            )
        assert calls == []

    def test_per_group_file_boundary_and_overflow_precede_file_resolution(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(results_module, "MAX_RESULTS_FILES_PER_GROUP", 3)
        resolver, _ = _unique_file_resolver()
        boundary = _saved_results_root()
        _add_saved_group(boundary, 3, relation="reported_exact")

        loaded = Results(DupeGuru())
        loaded.load_from_xml(_saved_xml(boundary), resolver)
        eq_(3, len(loaded.groups[0]))

        overflow = _saved_results_root()
        _add_saved_group(overflow, 4, relation="reported_exact")
        calls = []
        with raises(ValueError, match="per-group file count"):
            Results(DupeGuru()).load_from_xml(
                _saved_xml(overflow),
                lambda path: calls.append(path),
            )
        assert calls == []

    def test_total_file_boundary_and_overflow_precede_file_resolution(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(results_module, "MAX_RESULTS_TOTAL_FILES", 4)
        resolver, _ = _unique_file_resolver()
        boundary = _saved_results_root()
        _add_saved_group(boundary, 2, relation="reported_exact")
        _add_saved_group(boundary, 2, relation="reported_exact")

        Results(DupeGuru()).load_from_xml(_saved_xml(boundary), resolver)

        overflow = _saved_results_root()
        _add_saved_group(overflow, 2, relation="reported_exact")
        _add_saved_group(overflow, 3, relation="reported_exact")
        calls = []
        with raises(ValueError, match="total file count"):
            Results(DupeGuru()).load_from_xml(
                _saved_xml(overflow),
                lambda path: calls.append(path),
            )
        assert calls == []

    def test_per_group_match_boundary_and_overflow_precede_file_resolution(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(results_module, "MAX_RESULTS_MATCHES_PER_GROUP", 3)
        resolver, _ = _unique_file_resolver()
        boundary = _saved_results_root()
        _add_saved_group(
            boundary,
            3,
            matches=((0, 1, 90), (0, 2, 80), (1, 2, 70)),
        )

        Results(DupeGuru()).load_from_xml(_saved_xml(boundary), resolver)

        overflow = _saved_results_root()
        _add_saved_group(
            overflow,
            3,
            matches=((0, 1, 90), (0, 2, 80), (1, 2, 70), (1, 0, 60)),
        )
        calls = []
        with raises(ValueError, match="per-group match count"):
            Results(DupeGuru()).load_from_xml(
                _saved_xml(overflow),
                lambda path: calls.append(path),
            )
        assert calls == []

    def test_total_match_boundary_and_overflow_precede_file_resolution(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(results_module, "MAX_RESULTS_TOTAL_MATCHES", 2)
        resolver, _ = _unique_file_resolver()
        boundary = _saved_results_root()
        _add_saved_group(boundary, 2, matches=((0, 1, 90),))
        _add_saved_group(boundary, 2, matches=((0, 1, 90),))

        Results(DupeGuru()).load_from_xml(_saved_xml(boundary), resolver)

        overflow = _saved_results_root()
        for _ in range(3):
            _add_saved_group(overflow, 2, matches=((0, 1, 90),))
        calls = []
        with raises(ValueError, match="total match count"):
            Results(DupeGuru()).load_from_xml(
                _saved_xml(overflow),
                lambda path: calls.append(path),
            )
        assert calls == []

    def test_oversized_attribute_is_rejected_before_file_resolution(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(results_module, "MAX_RESULTS_XML_ATTRIBUTE_CHARS", 32)
        root = _saved_results_root()
        group = ET.SubElement(root, "group", relation="similar")
        ET.SubElement(group, "file", path="a" * 33)
        ET.SubElement(group, "file", path="two")
        calls = []

        with raises(ValueError, match="attribute string"):
            Results(DupeGuru()).load_from_xml(
                _saved_xml(root),
                lambda path: calls.append(path),
            )

        assert calls == []

    def test_excessive_saved_words_are_rejected_before_file_resolution(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(results_module, "MAX_RESULTS_WORDS_PER_FILE", 3)
        root = _saved_results_root()
        group = ET.SubElement(root, "group", relation="similar")
        ET.SubElement(group, "file", path="one", words="a,b,c,d")
        ET.SubElement(group, "file", path="two", words="a")
        calls = []

        with raises(ValueError, match="word count"):
            Results(DupeGuru()).load_from_xml(
                _saved_xml(root),
                lambda path: calls.append(path),
            )

        assert calls == []

    def test_malformed_match_is_rejected_before_file_resolution(self):
        root = _saved_results_root()
        group = _add_saved_group(root, 2, matches=((0, 1, 90),))
        group[-1].attrib.pop("percentage")
        calls = []

        with raises(ValueError, match="percentage"):
            Results(DupeGuru()).load_from_xml(
                _saved_xml(root),
                lambda path: calls.append(path),
            )

        assert calls == []

    def test_duplicate_saved_match_pair_is_rejected(self):
        root = _saved_results_root()
        _add_saved_group(
            root,
            3,
            matches=((0, 1, 90), (1, 0, 80), (1, 2, 70)),
        )

        with raises(ValueError, match="duplicate match pair"):
            results_module._parse_saved_results(_saved_xml(root))

    def test_incomplete_saved_match_graph_is_rejected(self):
        root = _saved_results_root()
        _add_saved_group(
            root,
            3,
            matches=((0, 1, 90), (0, 2, 80)),
        )

        with raises(ValueError, match="complete match graph"):
            results_module._parse_saved_results(_saved_xml(root))

    def test_large_matchless_legacy_group_fails_before_quadratic_work(
        self,
        monkeypatch,
    ):
        root = _saved_results_root()
        _add_saved_group(root, 2_000, relation="similar")
        file_calls = []
        match_calls = []

        def forbidden_match(*args):
            match_calls.append(args)
            raise AssertionError("Match construction must not start")

        monkeypatch.setattr(results_module.engine, "Match", forbidden_match)
        with raises(ValueError, match="pair limit"):
            Results(DupeGuru()).load_from_xml(
                _saved_xml(root),
                lambda path: file_calls.append(path),
            )

        assert file_calls == []
        assert match_calls == []

    def test_legacy_pair_budget_accepts_256_files_and_rejects_257_before_resolution(
        self,
    ):
        boundary = _saved_results_root()
        _add_saved_group(boundary, 256, relation="similar")

        document = results_module._parse_saved_results(_saved_xml(boundary))

        eq_(256, len(document.groups[0].files))
        overflow = _saved_results_root()
        _add_saved_group(overflow, 257, relation="similar")
        calls = []
        with raises(ValueError, match="pair limit"):
            Results(DupeGuru()).load_from_xml(
                _saved_xml(overflow),
                lambda path: calls.append(path),
            )
        assert calls == []

    def test_legacy_work_budget_boundary_is_checked_during_parse(
        self,
        monkeypatch,
    ):
        root = _saved_results_root()
        group = ET.SubElement(root, "group", relation="similar")
        ET.SubElement(group, "file", path="one", words="abc")
        ET.SubElement(group, "file", path="two", words="x")
        monkeypatch.setattr(
            results_module,
            "MAX_LEGACY_RECONSTRUCTION_WORK",
            13,
        )

        document = results_module._parse_saved_results(_saved_xml(root))

        assert len(document.groups) == 1
        monkeypatch.setattr(
            results_module,
            "MAX_LEGACY_RECONSTRUCTION_WORK",
            12,
        )
        with raises(ValueError, match="work limit"):
            results_module._parse_saved_results(_saved_xml(root))

    def test_legacy_work_amplification_fails_before_resolution_or_compare(
        self,
        monkeypatch,
    ):
        root = _saved_results_root()
        group = ET.SubElement(root, "group", relation="similar")
        words = ",".join(["x"] * results_module.MAX_RESULTS_WORDS_PER_FILE)
        ET.SubElement(group, "file", path="one", words=words)
        ET.SubElement(group, "file", path="two", words=words)
        resolver_calls = []

        def forbidden_compare(*_args, **_kwargs):
            raise AssertionError("legacy comparison must not start")

        monkeypatch.setattr(results_module.engine, "compare", forbidden_compare)
        with raises(ValueError, match="work limit"):
            Results(DupeGuru()).load_from_xml(
                _saved_xml(root),
                lambda path: resolver_calls.append(path),
            )

        assert resolver_calls == []

    def test_legacy_work_budget_is_cumulative_across_document(
        self,
        monkeypatch,
    ):
        root = _saved_results_root()
        for suffix in ("a", "b"):
            group = ET.SubElement(root, "group", relation="similar")
            ET.SubElement(
                group,
                "file",
                path=f"one-{suffix}",
                words="abc",
            )
            ET.SubElement(
                group,
                "file",
                path=f"two-{suffix}",
                words="x",
            )
        monkeypatch.setattr(
            results_module,
            "MAX_LEGACY_RECONSTRUCTION_WORK",
            20,
        )
        resolver_calls = []

        with raises(ValueError, match="work limit"):
            Results(DupeGuru()).load_from_xml(
                _saved_xml(root),
                lambda path: resolver_calls.append(path),
            )

        assert resolver_calls == []

    def test_small_matchless_legacy_group_is_rebuilt_iteratively(self):
        root = _saved_results_root()
        _add_saved_group(root, 32, relation="similar")
        resolver, _ = _unique_file_resolver()
        loaded = Results(DupeGuru())

        loaded.load_from_xml(_saved_xml(root), resolver)

        eq_(1, len(loaded.groups))
        eq_(32, len(loaded.groups[0]))
        eq_(32 * 31 // 2, len(loaded.groups[0].matches))

    def test_dense_saved_similarity_group_uses_bulk_constructor(self, monkeypatch):
        root = _saved_results_root()
        file_count = 100
        matches = tuple((first, second, 90) for first in range(file_count) for second in range(first + 1, file_count))
        _add_saved_group(
            root,
            file_count,
            relation="similar",
            matches=matches,
        )
        resolver, _ = _unique_file_resolver()

        def forbidden_incremental_add(*_args, **_kwargs):
            raise AssertionError("dense saved groups must not replay add_match")

        monkeypatch.setattr(engine.Group, "add_match", forbidden_incremental_add)
        loaded = Results(DupeGuru())
        loaded.load_from_xml(_saved_xml(root), resolver)

        [group] = loaded.groups
        assert len(group) == file_count
        assert len(group.matches) == file_count * (file_count - 1) // 2

    def test_large_compact_exact_group_remains_linear_and_review_only(
        self,
        monkeypatch,
    ):
        root = _saved_results_root()
        _add_saved_group(root, 5_000, relation="reported_exact")
        resolver, _ = _unique_file_resolver()

        def forbidden_match(*args):
            raise AssertionError("Compact exact load must not construct pair matches")

        monkeypatch.setattr(results_module.engine, "Match", forbidden_match)
        loaded = Results(DupeGuru())
        loaded.load_from_xml(_saved_xml(root), resolver)

        group = loaded.groups[0]
        eq_(5_000, len(group))
        assert group._is_exact
        assert group.verification_kind is engine.VerificationKind.UNVERIFIED
        assert loaded.loaded_report
        assert loaded.scan_receipt is None
        assert loaded.mark_count == 0
        assert evaluate_duplicate(loaded, group[1]).code is EligibilityCode.SAVED_REPORT

    def test_failed_load_preserves_input_path_and_existing_results_state(
        self,
        tmp_path,
    ):
        results = self.results
        results.mark(self.objects[1])
        results.apply_filter("foo")
        receipt = object()
        results.scan_receipt = receipt
        before_groups = list(results.groups)
        before_dupes = list(results.dupes)
        before_marks = [results.is_marked(item) for item in self.objects]
        before_stat_line = results.stat_line
        before_modified = results.is_modified
        before_words = getattr(self.objects[0], "words", None)
        before_is_ref = self.objects[0].is_ref
        payload = b"<results><group><group/></group></results>"
        source = tmp_path / "malformed.dupeguru"
        source.write_bytes(payload)

        with raises(ValueError):
            results.load_from_xml(
                source,
                lambda path: (_ for _ in ()).throw(AssertionError("malformed XML must not resolve files")),
            )

        assert source.read_bytes() == payload
        assert list(results.groups) == before_groups
        assert list(results.dupes) == before_dupes
        assert [results.is_marked(item) for item in self.objects] == before_marks
        assert results.stat_line == before_stat_line
        assert results.is_modified is before_modified
        assert results.scan_receipt is receipt
        assert getattr(self.objects[0], "words", None) == before_words
        assert self.objects[0].is_ref is before_is_ref

    def test_byte_limit_failure_preserves_input_bytes_and_existing_filter(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(results_module, "MAX_RESULTS_XML_BYTES", 32)
        results = self.results
        results.apply_filter("foo")
        before_groups = list(results.groups)
        payload = io.BytesIO(b"<results>" + b" " * 64 + b"</results>")
        original = payload.getvalue()

        with raises(ValueError, match="size limit"):
            results.load_from_xml(payload, lambda path: None)

        assert payload.getvalue() == original
        assert list(results.groups) == before_groups

    def test_commit_failure_rolls_back_results_and_resolved_file_attributes(
        self,
        monkeypatch,
    ):
        results = self.results
        before_groups = list(results.groups)
        first = NamedObject("first.bin", size=1)
        second = NamedObject("second.bin", size=1)
        first.words = ["old", "first"]
        second.words = ["old", "second"]
        before_attributes = [
            (list(first.words), first.is_ref),
            (list(second.words), second.is_ref),
        ]
        root = _saved_results_root()
        group = _add_saved_group(root, 2, relation="reported_exact")
        paths = [element.get("path") for element in group if element.tag == "file"]
        files = dict(zip(paths, (first, second), strict=True))

        def fail_commit(filter_string):
            raise RuntimeError("simulated commit failure")

        monkeypatch.setattr(results, "apply_filter", fail_commit)
        with raises(RuntimeError, match="simulated commit failure"):
            results.load_from_xml(_saved_xml(root), files.get)

        assert list(results.groups) == before_groups
        assert (first.words, first.is_ref) == before_attributes[0]
        assert (second.words, second.is_ref) == before_attributes[1]

    def test_remember_match_percentage(self):
        group = self.groups[0]
        d1, d2, d3 = group
        fake_matches = set()
        fake_matches.add(engine.Match(d1, d2, 42))
        fake_matches.add(engine.Match(d1, d3, 43))
        fake_matches.add(engine.Match(d2, d3, 46))
        group.matches = fake_matches
        f = io.BytesIO()
        results = self.results
        results.save_to_xml(f)
        f.seek(0)
        app = DupeGuru()
        results = Results(app)
        results.load_from_xml(f, self.get_file)
        group = results.groups[0]
        d1, d2, d3 = group
        match = group.get_match_of(d2)  # d1 - d2
        eq_(42, match[2])
        match = group.get_match_of(d3)  # d1 - d3
        eq_(43, match[2])
        group.switch_ref(d2)
        match = group.get_match_of(d3)  # d2 - d3
        eq_(46, match[2])

    def test_save_and_load(self):
        # previously, when reloading matches, they wouldn't be reloaded as namedtuples
        f = io.BytesIO()
        self.results.save_to_xml(f)
        f.seek(0)
        self.results.load_from_xml(f, self.get_file)
        first(self.results.groups[0].matches).percentage

    def test_apply_filter_works_on_paths(self):
        # apply_filter() searches on the whole path, not just on the filename.
        self.results.apply_filter("basepath")
        eq_(len(self.results.groups), 2)

    def test_save_xml_with_invalid_characters(self):
        # Refuse to publish a document which ElementTree cannot load again.
        self.objects[0].name = "foo\x19"
        output = io.BytesIO()

        with raises(ValueError, match="XML 1.0"):
            self.results.save_to_xml(output)

        assert output.getvalue() == b""


class TestCaseResultsFilter:
    def setup_method(self, method):
        self.app = DupeGuru()
        self.results = self.app.results
        self.objects, self.matches, self.groups = GetTestGroups()
        self.results.groups = self.groups
        self.results.apply_filter(r"foo")

    def test_groups(self):
        eq_(1, len(self.results.groups))
        assert self.results.groups[0] is self.groups[0]

    def test_dupes(self):
        # There are 2 objects matching. The first one is ref. Only the 3rd one is supposed to be in dupes.
        eq_(1, len(self.results.dupes))
        assert self.results.dupes[0] is self.objects[2]

    def test_cancel_filter(self):
        self.results.apply_filter(None)
        eq_(3, len(self.results.dupes))
        eq_(2, len(self.results.groups))

    def test_dupes_reconstructed_filtered(self):
        # make_ref resets self.__dupes to None. When it's reconstructed, we want it filtered
        dupe = self.results.dupes[0]  # 3rd object
        self.results.make_ref(dupe)
        eq_(1, len(self.results.dupes))
        assert self.results.dupes[0] is self.objects[0]

    def test_include_ref_dupes_in_filter(self):
        # When only the ref of a group match the filter, include it in the group
        self.results.apply_filter(None)
        self.results.apply_filter(r"foo bar")
        eq_(1, len(self.results.groups))
        eq_(0, len(self.results.dupes))

    def test_filters_build_on_one_another(self):
        self.results.apply_filter(r"bar")
        eq_(1, len(self.results.groups))
        eq_(0, len(self.results.dupes))

    def test_stat_line(self):
        expected = "0 / 1 (0.00 B / 1.00 B) duplicates marked. filter: foo"
        eq_(expected, self.results.stat_line)
        self.results.apply_filter(r"bar")
        expected = "0 / 0 (0.00 B / 0.00 B) duplicates marked. filter: foo --> bar"
        eq_(expected, self.results.stat_line)
        self.results.apply_filter(None)
        expected = "0 / 3 (0.00 B / 1.01 KB) duplicates marked."
        eq_(expected, self.results.stat_line)

    def test_mark_count_is_filtered_as_well(self):
        self.results.apply_filter(None)
        # We don't want to perform mark_all() because we want the mark list to contain objects
        for dupe in self.results.dupes:
            self.results.mark(dupe)
        self.results.apply_filter(r"foo")
        expected = "1 / 1 (1.00 B / 1.00 B) duplicates marked. filter: foo"
        eq_(expected, self.results.stat_line)

    def test_mark_all_only_affects_filtered_items(self):
        # When performing actions like mark_all() and mark_none in a filtered environment, only mark
        # items that are actually in the filter.
        self.results.mark_all()
        self.results.apply_filter(None)
        eq_(self.results.mark_count, 1)

    def test_sort_groups(self):
        self.results.apply_filter(None)
        self.results.make_ref(self.objects[1])  # to have the 1024 b obkect as ref
        g1, g2 = self.groups
        self.results.apply_filter("a")  # Matches both group
        self.results.sort_groups("size")
        assert self.results.groups[0] is g2
        assert self.results.groups[1] is g1
        self.results.apply_filter(None)
        assert self.results.groups[0] is g2
        assert self.results.groups[1] is g1
        self.results.sort_groups("size", False)
        self.results.apply_filter("a")
        assert self.results.groups[1] is g2
        assert self.results.groups[0] is g1

    def test_set_group(self):
        # We want the new group to be filtered
        self.objects, self.matches, self.groups = GetTestGroups()
        self.results.groups = self.groups
        eq_(1, len(self.results.groups))
        assert self.results.groups[0] is self.groups[0]

    def test_load_cancels_filter(self, tmpdir):
        def get_file(path):
            return [f for f in self.objects if str(f.path) == path][0]

        filename = str(tmpdir.join("dupeguru_results.xml"))
        self.objects[4].name = "ibabtu 2"  # we can't have 2 files with the same path
        self.results.save_to_xml(filename)
        app = DupeGuru()
        r = Results(app)
        r.apply_filter("foo")
        r.load_from_xml(filename, get_file)
        eq_(2, len(r.groups))

    def test_remove_dupe(self):
        self.results.remove_duplicates([self.results.dupes[0]])
        self.results.apply_filter(None)
        eq_(2, len(self.results.groups))
        eq_(2, len(self.results.dupes))
        self.results.apply_filter("ibabtu")
        self.results.remove_duplicates([self.results.dupes[0]])
        self.results.apply_filter(None)
        eq_(1, len(self.results.groups))
        eq_(1, len(self.results.dupes))

    def test_filter_is_case_insensitive(self):
        self.results.apply_filter(None)
        self.results.apply_filter("FOO")
        eq_(1, len(self.results.dupes))

    def test_make_ref_on_filtered_out_doesnt_mess_stats(self):
        # When filtered, a group containing filtered out dupes will display them as being reference.
        # When calling make_ref on such a dupe, the total size and dupecount stats gets messed up
        # because they are *not* counted in the stats in the first place.
        g1, g2 = self.groups
        bar_bleh = g1[1]  # The "bar bleh" dupe is filtered out
        self.results.make_ref(bar_bleh)
        # Now the stats should display *2* markable dupes (instead of 1)
        expected = "0 / 2 (0.00 B / 2.00 B) duplicates marked. filter: foo"
        eq_(expected, self.results.stat_line)
        self.results.apply_filter(None)  # Now let's make sure our unfiltered results aren't fucked up
        expected = "0 / 3 (0.00 B / 3.00 B) duplicates marked."
        eq_(expected, self.results.stat_line)


class TestCaseResultsRefFile:
    def setup_method(self, method):
        self.app = DupeGuru()
        self.results = self.app.results
        self.objects, self.matches, self.groups = GetTestGroups()
        self.objects[0].is_ref = True
        self.objects[1].is_ref = True
        self.results.groups = self.groups

    def test_stat_line(self):
        expected = "0 / 2 (0.00 B / 2.00 B) duplicates marked."
        eq_(expected, self.results.stat_line)
