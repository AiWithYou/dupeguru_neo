# Copyright 2016 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import io
from xml.etree import ElementTree as ET

from pytest import raises
from hscommon.testutil import eq_

import core.ignore as ignore_module
from core.ignore import IgnoreList, IgnoreListLimitError, IgnoreListLoadError


def test_empty():
    il = IgnoreList()
    eq_(0, len(il))
    assert not il.are_ignored("foo", "bar")


def test_simple():
    il = IgnoreList()
    il.ignore("foo", "bar")
    assert il.are_ignored("foo", "bar")
    assert il.are_ignored("bar", "foo")
    assert not il.are_ignored("foo", "bleh")
    assert not il.are_ignored("bleh", "bar")
    eq_(1, len(il))


def test_multiple():
    il = IgnoreList()
    il.ignore("foo", "bar")
    il.ignore("foo", "bleh")
    il.ignore("bleh", "bar")
    il.ignore("aybabtu", "bleh")
    assert il.are_ignored("foo", "bar")
    assert il.are_ignored("bar", "foo")
    assert il.are_ignored("foo", "bleh")
    assert il.are_ignored("bleh", "bar")
    assert not il.are_ignored("aybabtu", "bar")
    eq_(4, len(il))


def test_ignored_neighbors_exposes_both_stored_edge_directions():
    il = IgnoreList()
    il.ignore("foo", "bar")
    il.ignore("bar", "baz")

    assert il.ignored_neighbors("bar") == {"foo", "baz"}
    assert il.ignored_neighbors("missing") == set()


def test_clear():
    il = IgnoreList()
    il.ignore("foo", "bar")
    il.clear()
    assert not il.are_ignored("foo", "bar")
    assert not il.are_ignored("bar", "foo")
    eq_(0, len(il))


def test_add_same_twice():
    il = IgnoreList()
    il.ignore("foo", "bar")
    il.ignore("bar", "foo")
    eq_(1, len(il))


def test_save_to_xml():
    il = IgnoreList()
    il.ignore("foo", "bar")
    il.ignore("foo", "bleh")
    il.ignore("bleh", "bar")
    f = io.BytesIO()
    il.save_to_xml(f)
    f.seek(0)
    doc = ET.parse(f)
    root = doc.getroot()
    eq_(root.tag, "ignore_list")
    eq_(len(root), 2)
    eq_(len([c for c in root if c.tag == "file"]), 2)
    f1, f2 = root[:]
    subchildren = [c for c in f1 if c.tag == "file"] + [c for c in f2 if c.tag == "file"]
    eq_(len(subchildren), 3)


def test_save_then_load():
    il = IgnoreList()
    il.ignore("foo", "bar")
    il.ignore("foo", "bleh")
    il.ignore("bleh", "bar")
    il.ignore("\u00e9", "bar")
    f = io.BytesIO()
    il.save_to_xml(f)
    f.seek(0)
    il = IgnoreList()
    il.load_from_xml(f)
    eq_(4, len(il))
    assert il.are_ignored("\u00e9", "bar")


def test_attribute_whitespace_round_trips_and_byte_projection_covers_xml():
    il = IgnoreList()
    first = "line-one\nline-two\tcolumn"
    second = "carriage\rreturn"
    il.ignore(first, second)
    output = io.BytesIO()

    il.save_to_xml(output)
    payload = output.getvalue()

    assert b"&#10;" in payload
    assert b"&#09;" in payload
    assert b"&#13;" in payload
    assert il._projected_bytes >= len(payload)
    output.seek(0)
    restored = IgnoreList()
    assert restored.load_from_xml(output) is None
    assert restored.are_ignored(first, second)


def test_xml_unrepresentable_runtime_path_is_rejected_transactionally():
    il = IgnoreList()
    il.ignore("existing-a", "existing-b")
    revision_before = il.revision

    for invalid in ("control-\x01", "surrogate-\udcff"):
        with raises(IgnoreListLimitError, match="XML 1.0"):
            il.ignore(invalid, "valid")

    assert list(il) == [("existing-a", "existing-b")]
    assert il.revision == revision_before


def test_load_xml_with_empty_file_tags():
    f = io.BytesIO()
    f.write(b'<?xml version="1.0" encoding="utf-8"?><ignore_list><file><file/></file></ignore_list>')
    f.seek(0)
    il = IgnoreList()
    il.load_from_xml(f)
    eq_(0, len(il))


def test_invalid_schema_is_typed_failure_and_preserves_existing_state():
    il = IgnoreList()
    il.ignore("existing-a", "existing-b")
    payload = (
        b'<ignore_list><file path="valid"><file path="other"/></file>'
        b'<file path="broken" unexpected="yes"><file path="third"/></file></ignore_list>'
    )

    failure = il.load_from_xml(io.BytesIO(payload))

    assert isinstance(failure, IgnoreListLoadError)
    assert list(il) == [("existing-a", "existing-b")]


def test_nested_ignore_child_is_rejected_instead_of_silently_accepted():
    il = IgnoreList()
    payload = b'<ignore_list><file path="a"><file path="b">' b'<file path="c"/></file></file></ignore_list>'

    failure = il.load_from_xml(io.BytesIO(payload))

    assert isinstance(failure, IgnoreListLoadError)
    assert len(il) == 0


def test_empty_ignore_parent_is_rejected_transactionally():
    il = IgnoreList()
    il.ignore("existing-a", "existing-b")

    failure = il.load_from_xml(io.BytesIO(b'<ignore_list><file path="orphan"/></ignore_list>'))

    assert isinstance(failure, IgnoreListLoadError)
    assert list(il) == [("existing-a", "existing-b")]


def test_ignore_loader_bulk_builds_without_per_edge_ignore_calls(monkeypatch):
    il = IgnoreList()

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("transactional loader must bulk-build its state")

    monkeypatch.setattr(il, "ignore", unexpected_call)
    payload = b'<ignore_list><file path="a"><file path="b"/>' b'<file path="c"/></file></ignore_list>'

    assert il.load_from_xml(io.BytesIO(payload)) is None
    assert il.are_ignored("a", "b")
    assert il.are_ignored("c", "a")
    assert len(il) == 2


def test_ignore_caller_specific_edge_limit_is_transactional(monkeypatch):
    il = IgnoreList()
    il.ignore("existing-a", "existing-b")
    monkeypatch.setattr(ignore_module, "IGNORE_XML_MAX_EDGES", 1)
    payload = b'<ignore_list><file path="a"><file path="b"/>' b'<file path="c"/></file></ignore_list>'

    failure = il.load_from_xml(io.BytesIO(payload))

    assert isinstance(failure, IgnoreListLoadError)
    assert list(il) == [("existing-a", "existing-b")]


def test_runtime_edge_limit_rejects_before_mutating_state(monkeypatch):
    il = IgnoreList()
    il.ignore("existing-a", "existing-b")
    monkeypatch.setattr(ignore_module, "IGNORE_XML_MAX_EDGES", 1)

    with raises(IgnoreListLimitError):
        il.ignore("new-a", "new-b")

    assert list(il) == [("existing-a", "existing-b")]


def test_ignore_many_is_transactional_when_batch_exceeds_limit(monkeypatch):
    il = IgnoreList()
    il.ignore("existing-a", "existing-b")
    revision_before = il.revision
    monkeypatch.setattr(ignore_module, "IGNORE_XML_MAX_EDGES", 2)

    with raises(IgnoreListLimitError):
        il.ignore_many(
            (
                ("first", "second"),
                ("third", "fourth"),
            )
        )

    assert list(il) == [("existing-a", "existing-b")]
    assert il.revision == revision_before


def test_runtime_character_limit_matches_loader_contract(monkeypatch):
    il = IgnoreList()
    monkeypatch.setattr(
        ignore_module,
        "IGNORE_XML_MAX_TOTAL_CHARS",
        len("ignore_list") + 2 * (len("file") + len("path")) + 2,
    )

    with raises(IgnoreListLimitError):
        il.ignore("aa", "bb")

    assert len(il) == 0


def test_ignore_caller_specific_path_limit_is_transactional(monkeypatch):
    il = IgnoreList()
    il.ignore("existing-a", "existing-b")
    monkeypatch.setattr(ignore_module, "IGNORE_XML_MAX_PATH_CHARS", 2)

    failure = il.load_from_xml(io.BytesIO(b'<ignore_list><file path="abc"><file path="de"/></file></ignore_list>'))

    assert isinstance(failure, IgnoreListLoadError)
    assert list(il) == [("existing-a", "existing-b")]


def test_are_ignore_works_when_a_child_is_a_key_somewhere_else():
    il = IgnoreList()
    il.ignore("foo", "bar")
    il.ignore("bar", "baz")
    assert il.are_ignored("bar", "foo")


def test_no_dupes_when_a_child_is_a_key_somewhere_else():
    il = IgnoreList()
    il.ignore("foo", "bar")
    il.ignore("bar", "baz")
    il.ignore("bar", "foo")
    eq_(2, len(il))


def test_iterate():
    # It must be possible to iterate through ignore list
    il = IgnoreList()
    expected = [("foo", "bar"), ("bar", "baz"), ("foo", "baz")]
    for i in expected:
        il.ignore(i[0], i[1])
    for i in il:
        expected.remove(i)  # No exception should be raised
    assert not expected  # expected should be empty


def test_filter():
    il = IgnoreList()
    il.ignore("foo", "bar")
    il.ignore("bar", "baz")
    il.ignore("foo", "baz")
    il.filter(lambda f, s: f == "bar")
    eq_(1, len(il))
    assert not il.are_ignored("foo", "bar")
    assert il.are_ignored("bar", "baz")


def test_save_with_non_ascii_items():
    il = IgnoreList()
    il.ignore("\xac", "\xbf")
    f = io.BytesIO()
    try:
        il.save_to_xml(f)
    except Exception as e:
        raise AssertionError(str(e))


def test_len():
    il = IgnoreList()
    eq_(0, len(il))
    il.ignore("foo", "bar")
    eq_(1, len(il))


def test_nonzero():
    il = IgnoreList()
    assert not il
    il.ignore("foo", "bar")
    assert il


def test_remove():
    il = IgnoreList()
    il.ignore("foo", "bar")
    il.ignore("foo", "baz")
    il.remove("bar", "foo")
    eq_(len(il), 1)
    assert not il.are_ignored("foo", "bar")


def test_remove_non_existant():
    il = IgnoreList()
    il.ignore("foo", "bar")
    il.ignore("foo", "baz")
    with raises(ValueError):
        il.remove("foo", "bleh")
