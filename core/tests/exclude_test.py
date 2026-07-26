# Copyright 2016 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import io
from xml.etree import ElementTree as ET

import pytest

from hscommon.testutil import eq_
from hscommon.plat import ISWINDOWS

import core.exclude as exclude_module
from core.tests.base import DupeGuru
from core.exclude import (
    ExcludeDict,
    ExcludeList,
    ExcludeListLimitError,
    ExcludeListLoadError,
    default_regexes,
)

from re import error

# Two slightly different implementations here, one around a list of lists,
# and another around a dictionary.


class TestCaseListXMLLoading:
    def setup_method(self, method):
        self.exclude_list = ExcludeList()

    def test_load_non_existant_file(self):
        # Loads the pre-defined regexes
        self.exclude_list.load_from_xml("non_existant.xml")
        eq_(len(default_regexes), len(self.exclude_list))
        # they should also be marked by default
        eq_(len(default_regexes), self.exclude_list.marked_count)

    def test_save_to_xml(self):
        f = io.BytesIO()
        self.exclude_list.save_to_xml(f)
        f.seek(0)
        doc = ET.parse(f)
        root = doc.getroot()
        eq_("exclude_list", root.tag)

    def test_save_and_load(self, tmpdir):
        e1 = ExcludeList()
        e2 = ExcludeList()
        eq_(len(e1), 0)
        e1.add(r"one")
        e1.mark(r"one")
        e1.add(r"two")
        tmpxml = str(tmpdir.join("exclude_testunit.xml"))
        e1.save_to_xml(tmpxml)
        e2.load_from_xml(tmpxml)
        # We should have the default regexes
        assert r"one" in e2
        assert r"two" in e2
        eq_(len(e2), 2)
        eq_(e2.marked_count, 1)

    def test_invalid_schema_is_rejected_without_partial_state(self):
        self.exclude_list.add("existing")
        self.exclude_list.mark("existing")
        before = list(self.exclude_list)
        root = ET.Element("foobar")
        exclude_node = ET.SubElement(root, "bogus")
        exclude_node.set("regex", "None")
        exclude_node.set("marked", "y")

        exclude_node = ET.SubElement(root, "exclude")
        exclude_node.set("regex", "one")
        # marked field invalid
        exclude_node.set("markedddd", "y")

        exclude_node = ET.SubElement(root, "exclude")
        exclude_node.set("regex", "two")
        # missing marked field

        exclude_node = ET.SubElement(root, "exclude")
        exclude_node.set("regex", "three")
        exclude_node.set("markedddd", "pazjbjepo")

        f = io.BytesIO()
        tree = ET.ElementTree(root)
        tree.write(f, encoding="utf-8")
        f.seek(0)
        failure = self.exclude_list.load_from_xml(f)

        assert isinstance(failure, ExcludeListLoadError)
        assert list(self.exclude_list) == before

    @pytest.mark.parametrize("regex", [r".*", r"one))"])
    def test_dangerous_or_invalid_regex_is_typed_failure_and_preserves_state(self, regex):
        self.exclude_list.add("existing")
        self.exclude_list.mark("existing")
        before = list(self.exclude_list)
        payload = (
            '<?xml version="1.0" encoding="utf-8"?>' '<exclude_list><exclude regex="{}" marked="y"/></exclude_list>'
        ).format(regex)

        failure = self.exclude_list.load_from_xml(io.BytesIO(payload.encode("utf-8")))

        assert isinstance(failure, ExcludeListLoadError)
        assert list(self.exclude_list) == before

    def test_loader_builds_entries_without_quadratic_add_or_mark_calls(self, monkeypatch):
        def unexpected_call(*_args, **_kwargs):
            raise AssertionError("transactional loader must bulk-build its state")

        monkeypatch.setattr(self.exclude_list, "add", unexpected_call)
        monkeypatch.setattr(self.exclude_list, "mark", unexpected_call)
        payload = b'<exclude_list><exclude regex="one" marked="y"/>' b'<exclude regex="two" marked="n"/></exclude_list>'

        assert self.exclude_list.load_from_xml(io.BytesIO(payload)) is None
        assert list(self.exclude_list) == [(False, "two"), (True, "one")]

    def test_caller_specific_item_limit_is_enforced_transactionally(self, monkeypatch):
        self.exclude_list.add("existing")
        before = list(self.exclude_list)
        monkeypatch.setattr(exclude_module, "EXCLUDE_XML_MAX_ITEMS", 2)
        payload = (
            b'<exclude_list><exclude regex="one" marked="n"/>'
            b'<exclude regex="two" marked="n"/>'
            b'<exclude regex="three" marked="n"/></exclude_list>'
        )

        failure = self.exclude_list.load_from_xml(io.BytesIO(payload))

        assert isinstance(failure, ExcludeListLoadError)
        assert list(self.exclude_list) == before

    def test_individually_valid_but_uncombinable_union_is_rejected(self):
        if not self.exclude_list._use_union:
            pytest.skip("separate-regex mode does not combine expressions")
        payload = (
            b'<exclude_list><exclude regex="(?P&lt;name&gt;one)" marked="y"/>'
            b'<exclude regex="(?P&lt;name&gt;two)" marked="y"/></exclude_list>'
        )

        failure = self.exclude_list.load_from_xml(io.BytesIO(payload))

        assert isinstance(failure, ExcludeListLoadError)
        assert len(self.exclude_list) == 0

    def test_caller_specific_regex_length_limit_preserves_state(self, monkeypatch):
        self.exclude_list.add("old")
        before = list(self.exclude_list)
        monkeypatch.setattr(exclude_module, "EXCLUDE_XML_MAX_REGEX_CHARS", 3)

        failure = self.exclude_list.load_from_xml(
            io.BytesIO(b'<exclude_list><exclude regex="four" marked="n"/></exclude_list>')
        )

        assert isinstance(failure, ExcludeListLoadError)
        assert list(self.exclude_list) == before


class TestCaseDictXMLLoading(TestCaseListXMLLoading):
    def setup_method(self, method):
        self.exclude_list = ExcludeDict()


@pytest.mark.parametrize("exclude_class", [ExcludeList, ExcludeDict])
class TestRuntimeBoundedExclusionList:
    def test_add_item_limit_is_atomic(self, exclude_class, monkeypatch):
        exclude_list = exclude_class()
        exclude_list.add("kept")
        before = list(exclude_list)
        revision = exclude_list.revision
        monkeypatch.setattr(exclude_module, "EXCLUDE_XML_MAX_ITEMS", 1)

        with pytest.raises(ExcludeListLimitError):
            exclude_list.add("rejected")

        assert list(exclude_list) == before
        assert exclude_list.revision == revision

    def test_add_regex_and_total_limits_are_atomic(
        self,
        exclude_class,
        monkeypatch,
    ):
        exclude_list = exclude_class()
        exclude_list.add("one")
        before = list(exclude_list)
        revision = exclude_list.revision
        monkeypatch.setattr(exclude_module, "EXCLUDE_XML_MAX_REGEX_CHARS", 3)
        monkeypatch.setattr(exclude_module, "EXCLUDE_XML_MAX_TOTAL_REGEX_CHARS", 5)

        with pytest.raises(ExcludeListLimitError):
            exclude_list.add("four")
        with pytest.raises(ExcludeListLimitError):
            exclude_list.add("two")

        assert list(exclude_list) == before
        assert exclude_list.revision == revision

    def test_rename_limit_and_duplicate_are_atomic(
        self,
        exclude_class,
        monkeypatch,
    ):
        exclude_list = exclude_class()
        exclude_list.add("one")
        exclude_list.mark("one")
        exclude_list.add("two")
        before = list(exclude_list)
        revision = exclude_list.revision

        with pytest.raises(ExcludeListLimitError):
            exclude_list.rename("one", "two")
        monkeypatch.setattr(exclude_module, "EXCLUDE_XML_MAX_REGEX_CHARS", 3)
        with pytest.raises(ExcludeListLimitError):
            exclude_list.rename("one", "long")

        assert list(exclude_list) == before
        assert exclude_list.revision == revision

    def test_xml_byte_and_character_limits_are_atomic(
        self,
        exclude_class,
        monkeypatch,
    ):
        exclude_list = exclude_class()
        before = list(exclude_list)
        revision = exclude_list.revision
        original_byte_limit = exclude_module.EXCLUDE_XML_MAX_BYTES
        empty_payload_size = len(
            ET.tostring(
                ET.Element("exclude_list"),
                encoding="utf-8",
                xml_declaration=True,
            )
        )
        monkeypatch.setattr(
            exclude_module,
            "EXCLUDE_XML_MAX_BYTES",
            empty_payload_size,
        )

        with pytest.raises(ExcludeListLimitError):
            exclude_list.add("one")
        monkeypatch.setattr(
            exclude_module,
            "EXCLUDE_XML_MAX_BYTES",
            original_byte_limit,
        )
        with pytest.raises(ExcludeListLimitError):
            exclude_list.add("control\x01character")

        assert list(exclude_list) == before
        assert exclude_list.revision == revision

    def test_save_revalidates_and_preserves_existing_destination(
        self,
        exclude_class,
        monkeypatch,
        tmp_path,
    ):
        exclude_list = exclude_class()
        exclude_list.add("one")
        destination = tmp_path / "exclude_list.xml"
        original = b"existing file must survive failed validation"
        destination.write_bytes(original)
        monkeypatch.setattr(exclude_module, "EXCLUDE_XML_MAX_ITEMS", 1)
        # Simulate a state created by older/unbounded code without using the now
        # bounded public mutation path.
        if isinstance(exclude_list._excluded, dict):
            exclude_list._excluded["two"] = {
                "index": 0,
                "compilable": True,
                "error": None,
                "compiled": exclude_list.compile_re("two")[2],
            }
            exclude_list._excluded["one"]["index"] = 1
        else:
            exclude_list._excluded.insert(
                0,
                ["two", True, None, exclude_list.compile_re("two")[2]],
            )

        with pytest.raises(ExcludeListLimitError):
            exclude_list.save_to_xml(destination)

        assert destination.read_bytes() == original

    def test_union_mark_failure_is_atomic(self, exclude_class):
        exclude_list = exclude_class(union_regex=True)
        first = r"(?P<same>one)"
        second = r"(?P<same>two)"
        exclude_list.add(first)
        exclude_list.mark(first)
        exclude_list.add(second)
        before = list(exclude_list)
        revision = exclude_list.revision

        with pytest.raises(ExcludeListLimitError):
            exclude_list.mark(second)

        assert list(exclude_list) == before
        assert exclude_list.revision == revision

    def test_all_public_mark_operations_update_revision(self, exclude_class):
        exclude_list = exclude_class()
        exclude_list.add("one")
        exclude_list.add("two")

        revision = exclude_list.revision
        exclude_list.mark_all()
        assert exclude_list.revision > revision
        assert list(exclude_list) == [(True, "two"), (True, "one")]

        revision = exclude_list.revision
        exclude_list.mark_invert()
        assert exclude_list.revision > revision
        assert list(exclude_list) == [(False, "two"), (False, "one")]

        revision = exclude_list.revision
        exclude_list.mark_toggle_multiple(["one", "two"])
        assert exclude_list.revision > revision
        assert list(exclude_list) == [(True, "two"), (True, "one")]

        revision = exclude_list.revision
        exclude_list.mark_none()
        assert exclude_list.revision > revision
        assert list(exclude_list) == [(False, "two"), (False, "one")]


class TestCaseListEmpty:
    def setup_method(self, method):
        self.app = DupeGuru()
        self.app.exclude_list = ExcludeList(union_regex=False)
        self.exclude_list = self.app.exclude_list

    def test_add_mark_and_remove_regex(self):
        regex1 = r"one"
        regex2 = r"two"
        self.exclude_list.add(regex1)
        assert regex1 in self.exclude_list
        self.exclude_list.add(regex2)
        self.exclude_list.mark(regex1)
        self.exclude_list.mark(regex2)
        eq_(len(self.exclude_list), 2)
        eq_(len(self.exclude_list.compiled), 2)
        compiled_files = [x for x in self.exclude_list.compiled_files]
        eq_(len(compiled_files), 2)
        self.exclude_list.remove(regex2)
        assert regex2 not in self.exclude_list
        eq_(len(self.exclude_list), 1)

    def test_add_duplicate(self):
        self.exclude_list.add(r"one")
        eq_(1, len(self.exclude_list))
        try:
            self.exclude_list.add(r"one")
        except Exception:
            pass
        eq_(1, len(self.exclude_list))

    def test_add_not_compilable(self):
        # Trying to add a non-valid regex should not work and raise exception
        regex = r"one))"
        try:
            self.exclude_list.add(regex)
        except Exception as e:
            # Make sure we raise a re.error so that the interface can process it
            eq_(type(e), error)
        added = self.exclude_list.mark(regex)
        eq_(added, False)
        eq_(len(self.exclude_list), 0)
        eq_(len(self.exclude_list.compiled), 0)
        compiled_files = [x for x in self.exclude_list.compiled_files]
        eq_(len(compiled_files), 0)

    def test_force_add_not_compilable(self):
        """Forced invalid entries can no longer create an unloadable document."""
        regex = r"one))"
        with pytest.raises(ExcludeListLimitError):
            self.exclude_list.add(regex, forced=True)
        eq_(len(self.exclude_list), 0)
        eq_(len(self.exclude_list.compiled), 0)
        compiled_files = [x for x in self.exclude_list.compiled_files]
        eq_(len(compiled_files), 0)
        with pytest.raises(ExcludeListLimitError):
            self.exclude_list.add(regex, forced=True)
        eq_(len(self.exclude_list), 0)
        eq_(len(self.exclude_list.compiled), 0)

    def test_rename_regex(self):
        regex = r"one"
        self.exclude_list.add(regex)
        self.exclude_list.mark(regex)
        regex_renamed = r"one))"
        # An invalid rename is rejected without changing the original entry.
        with pytest.raises(ExcludeListLimitError):
            self.exclude_list.rename(regex, regex_renamed)
        assert regex in self.exclude_list
        assert regex_renamed not in self.exclude_list
        assert self.exclude_list.is_marked(regex)
        regex_renamed_compilable = r"two"
        self.exclude_list.rename(regex, regex_renamed_compilable)
        assert regex_renamed_compilable in self.exclude_list
        eq_(self.exclude_list.is_marked(regex_renamed), False)
        eq_(self.exclude_list.is_marked(regex_renamed_compilable), True)
        eq_(len(self.exclude_list), 1)
        # Should still be marked after rename
        regex_compilable = r"three"
        self.exclude_list.rename(regex_renamed_compilable, regex_compilable)
        eq_(self.exclude_list.is_marked(regex_compilable), True)

    def test_rename_regex_file_to_path(self):
        regex = r".*/one.*"
        if ISWINDOWS:
            regex = r".*\\one.*"
        regex2 = r".*one.*"
        self.exclude_list.add(regex)
        self.exclude_list.mark(regex)
        compiled_re = [x.pattern for x in self.exclude_list._excluded_compiled]
        files_re = [x.pattern for x in self.exclude_list.compiled_files]
        paths_re = [x.pattern for x in self.exclude_list.compiled_paths]
        assert regex in compiled_re
        assert regex not in files_re
        assert regex in paths_re
        self.exclude_list.rename(regex, regex2)
        compiled_re = [x.pattern for x in self.exclude_list._excluded_compiled]
        files_re = [x.pattern for x in self.exclude_list.compiled_files]
        paths_re = [x.pattern for x in self.exclude_list.compiled_paths]
        assert regex not in compiled_re
        assert regex2 in compiled_re
        assert regex2 in files_re
        assert regex2 not in paths_re

    def test_restore_default(self):
        """Only unmark previously added regexes and mark the pre-defined ones"""
        regex = r"one"
        self.exclude_list.add(regex)
        self.exclude_list.mark(regex)
        self.exclude_list.restore_defaults()
        eq_(len(default_regexes), self.exclude_list.marked_count)
        # added regex shouldn't be marked
        eq_(self.exclude_list.is_marked(regex), False)
        # added regex shouldn't be in compiled list either
        compiled = [x for x in self.exclude_list.compiled]
        assert regex not in compiled
        # Only default regexes marked and in compiled list
        for re in default_regexes:
            assert self.exclude_list.is_marked(re)
            found = False
            for compiled_re in compiled:
                if compiled_re.pattern == re:
                    found = True
            if not found:
                raise (Exception(f"Default RE {re} not found in compiled list."))
        eq_(len(default_regexes), len(self.exclude_list.compiled))


class TestCaseListEmptyUnion(TestCaseListEmpty):
    """Same but with union regex"""

    def setup_method(self, method):
        self.app = DupeGuru()
        self.app.exclude_list = ExcludeList(union_regex=True)
        self.exclude_list = self.app.exclude_list

    def test_add_mark_and_remove_regex(self):
        regex1 = r"one"
        regex2 = r"two"
        self.exclude_list.add(regex1)
        assert regex1 in self.exclude_list
        self.exclude_list.add(regex2)
        self.exclude_list.mark(regex1)
        self.exclude_list.mark(regex2)
        eq_(len(self.exclude_list), 2)
        eq_(len(self.exclude_list.compiled), 1)
        compiled_files = [x for x in self.exclude_list.compiled_files]
        eq_(len(compiled_files), 1)  # Two patterns joined together into one
        assert "|" in compiled_files[0].pattern
        self.exclude_list.remove(regex2)
        assert regex2 not in self.exclude_list
        eq_(len(self.exclude_list), 1)

    def test_rename_regex_file_to_path(self):
        regex = r".*/one.*"
        if ISWINDOWS:
            regex = r".*\\one.*"
        regex2 = r".*one.*"
        self.exclude_list.add(regex)
        self.exclude_list.mark(regex)
        eq_(len([x for x in self.exclude_list]), 1)
        compiled_re = [x.pattern for x in self.exclude_list.compiled]
        files_re = [x.pattern for x in self.exclude_list.compiled_files]
        paths_re = [x.pattern for x in self.exclude_list.compiled_paths]
        assert regex in compiled_re
        assert regex not in files_re
        assert regex in paths_re
        self.exclude_list.rename(regex, regex2)
        eq_(len([x for x in self.exclude_list]), 1)
        compiled_re = [x.pattern for x in self.exclude_list.compiled]
        files_re = [x.pattern for x in self.exclude_list.compiled_files]
        paths_re = [x.pattern for x in self.exclude_list.compiled_paths]
        assert regex not in compiled_re
        assert regex2 in compiled_re
        assert regex2 in files_re
        assert regex2 not in paths_re

    def test_restore_default(self):
        """Only unmark previously added regexes and mark the pre-defined ones"""
        regex = r"one"
        self.exclude_list.add(regex)
        self.exclude_list.mark(regex)
        self.exclude_list.restore_defaults()
        eq_(len(default_regexes), self.exclude_list.marked_count)
        # added regex shouldn't be marked
        eq_(self.exclude_list.is_marked(regex), False)
        # added regex shouldn't be in compiled list either
        compiled = [x for x in self.exclude_list.compiled]
        assert regex not in compiled
        # Need to escape both to get the same strings after compilation
        compiled_escaped = {x.encode("unicode-escape").decode() for x in compiled[0].pattern.split("|")}
        default_escaped = {x.encode("unicode-escape").decode() for x in default_regexes}
        assert compiled_escaped == default_escaped
        eq_(len(default_regexes), len(compiled[0].pattern.split("|")))


class TestCaseDictEmpty(TestCaseListEmpty):
    """Same, but with dictionary implementation"""

    def setup_method(self, method):
        self.app = DupeGuru()
        self.app.exclude_list = ExcludeDict(union_regex=False)
        self.exclude_list = self.app.exclude_list


class TestCaseDictEmptyUnion(TestCaseDictEmpty):
    """Same, but with union regex"""

    def setup_method(self, method):
        self.app = DupeGuru()
        self.app.exclude_list = ExcludeDict(union_regex=True)
        self.exclude_list = self.app.exclude_list

    def test_add_mark_and_remove_regex(self):
        regex1 = r"one"
        regex2 = r"two"
        self.exclude_list.add(regex1)
        assert regex1 in self.exclude_list
        self.exclude_list.add(regex2)
        self.exclude_list.mark(regex1)
        self.exclude_list.mark(regex2)
        eq_(len(self.exclude_list), 2)
        eq_(len(self.exclude_list.compiled), 1)
        compiled_files = [x for x in self.exclude_list.compiled_files]
        # two patterns joined into one
        eq_(len(compiled_files), 1)
        self.exclude_list.remove(regex2)
        assert regex2 not in self.exclude_list
        eq_(len(self.exclude_list), 1)

    def test_rename_regex_file_to_path(self):
        regex = r".*/one.*"
        if ISWINDOWS:
            regex = r".*\\one.*"
        regex2 = r".*one.*"
        self.exclude_list.add(regex)
        self.exclude_list.mark(regex)
        marked_re = [x for marked, x in self.exclude_list if marked]
        eq_(len(marked_re), 1)
        compiled_re = [x.pattern for x in self.exclude_list.compiled]
        files_re = [x.pattern for x in self.exclude_list.compiled_files]
        paths_re = [x.pattern for x in self.exclude_list.compiled_paths]
        assert regex in compiled_re
        assert regex not in files_re
        assert regex in paths_re
        self.exclude_list.rename(regex, regex2)
        compiled_re = [x.pattern for x in self.exclude_list.compiled]
        files_re = [x.pattern for x in self.exclude_list.compiled_files]
        paths_re = [x.pattern for x in self.exclude_list.compiled_paths]
        assert regex not in compiled_re
        assert regex2 in compiled_re
        assert regex2 in files_re
        assert regex2 not in paths_re

    def test_restore_default(self):
        """Only unmark previously added regexes and mark the pre-defined ones"""
        regex = r"one"
        self.exclude_list.add(regex)
        self.exclude_list.mark(regex)
        self.exclude_list.restore_defaults()
        eq_(len(default_regexes), self.exclude_list.marked_count)
        # added regex shouldn't be marked
        eq_(self.exclude_list.is_marked(regex), False)
        # added regex shouldn't be in compiled list either
        compiled = [x for x in self.exclude_list.compiled]
        assert regex not in compiled
        # Need to escape both to get the same strings after compilation
        compiled_escaped = {x.encode("unicode-escape").decode() for x in compiled[0].pattern.split("|")}
        default_escaped = {x.encode("unicode-escape").decode() for x in default_regexes}
        assert compiled_escaped == default_escaped
        eq_(len(default_regexes), len(compiled[0].pattern.split("|")))


def split_union(pattern_object):
    """Returns list of strings for each union pattern"""
    return [x for x in pattern_object.pattern.split("|")]


class TestCaseCompiledList:
    """Test consistency between union or and separate versions."""

    def setup_method(self, method):
        self.e_separate = ExcludeList(union_regex=False)
        self.e_separate.restore_defaults()
        self.e_union = ExcludeList(union_regex=True)
        self.e_union.restore_defaults()

    def test_same_number_of_expressions(self):
        # We only get one union Pattern item in a tuple, which is made of however many parts
        eq_(len(split_union(self.e_union.compiled[0])), len(default_regexes))
        # We get as many as there are marked items
        eq_(len(self.e_separate.compiled), len(default_regexes))
        exprs = split_union(self.e_union.compiled[0])
        # We should have the same number and the same expressions
        eq_(len(exprs), len(self.e_separate.compiled))
        for expr in self.e_separate.compiled:
            assert expr.pattern in exprs

    def test_compiled_files(self):
        # is path separator checked properly to yield the output
        if ISWINDOWS:
            regex1 = r"test\\one\\sub"
        else:
            regex1 = r"test/one/sub"
        self.e_separate.add(regex1)
        self.e_separate.mark(regex1)
        self.e_union.add(regex1)
        self.e_union.mark(regex1)
        separate_compiled_dirs = self.e_separate.compiled
        separate_compiled_files = [x for x in self.e_separate.compiled_files]
        # HACK we need to call compiled property FIRST to generate the cache
        union_compiled_dirs = self.e_union.compiled
        # print(f"type: {type(self.e_union.compiled_files[0])}")
        # A generator returning only one item... ugh
        union_compiled_files = [x for x in self.e_union.compiled_files][0]
        print(f"compiled files: {union_compiled_files}")
        # Separate should give several plus the one added
        eq_(len(separate_compiled_dirs), len(default_regexes) + 1)
        # regex1 shouldn't be in the "files" version
        eq_(len(separate_compiled_files), len(default_regexes))
        # Only one Pattern returned, which when split should be however many + 1
        eq_(len(split_union(union_compiled_dirs[0])), len(default_regexes) + 1)
        # regex1 shouldn't be here either
        eq_(len(split_union(union_compiled_files)), len(default_regexes))


class TestCaseCompiledDict(TestCaseCompiledList):
    """Test the dictionary version"""

    def setup_method(self, method):
        self.e_separate = ExcludeDict(union_regex=False)
        self.e_separate.restore_defaults()
        self.e_union = ExcludeDict(union_regex=True)
        self.e_union.restore_defaults()
