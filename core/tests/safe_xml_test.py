import io
from xml.etree import ElementTree as ET

import pytest

from core import safe_xml as safe_xml_module
from core.safe_xml import iter_xml_events, parse_xml, write_xml


def test_parse_rejects_entity_documents():
    payload = io.BytesIO(b'<!DOCTYPE root [<!ENTITY x "expanded">]><root>&x;</root>')

    with pytest.raises(ValueError, match="DTD and entity"):
        parse_xml(payload)


def test_parse_enforces_a_byte_limit():
    payload = b"<root>123</root>"

    assert parse_xml(io.BytesIO(payload), max_bytes=len(payload)).tag == "root"
    with pytest.raises(ValueError, match="size limit"):
        parse_xml(io.BytesIO(payload), max_bytes=len(payload) - 1)


@pytest.mark.parametrize(
    ("payload", "limit_name", "boundary", "message"),
    [
        (b"<r><a/><b/></r>", "max_elements", 3, "element count"),
        (b"<r><a/></r>", "max_depth", 2, "element depth"),
        (
            b'<r first="1" second="2"/>',
            "max_attributes_per_element",
            2,
            "element attribute count",
        ),
        (
            b'<r first="1"><a second="2"/></r>',
            "max_attributes",
            2,
            "attribute count",
        ),
        (b"<root/>", "max_name_chars", 4, "element name"),
        (
            b'<r value="abcd"/>',
            "max_attribute_chars",
            4,
            "attribute string",
        ),
        (b"<r>abcd</r>", "max_text_chars", 4, "element text"),
        (b"<r><a/>abcd</r>", "max_tail_chars", 4, "element tail"),
    ],
)
def test_parse_accepts_each_shape_boundary_and_rejects_one_less(
    payload,
    limit_name,
    boundary,
    message,
):
    assert parse_xml(io.BytesIO(payload), **{limit_name: boundary}).tag in {
        "r",
        "root",
    }

    with pytest.raises(ValueError, match=message):
        parse_xml(io.BytesIO(payload), **{limit_name: boundary - 1})


def test_parse_enforces_total_string_content():
    payload = b'<r value="abcd">efgh</r>'

    assert parse_xml(io.BytesIO(payload), max_total_chars=14).tag == "r"
    with pytest.raises(ValueError, match="string content"):
        parse_xml(io.BytesIO(payload), max_total_chars=13)


def test_event_iterator_delays_end_until_tail_is_finalized(monkeypatch):
    monkeypatch.setattr(safe_xml_module, "_XML_READ_CHUNK_BYTES", 1)
    payload = io.BytesIO(b"<root><first/> \r\n\t <second/></root>")

    events = list(iter_xml_events(payload))

    ended = {element.tag: element for event, element in events if event == "end"}
    assert ended["first"].tail == " \n\t "


def test_parse_limits_unclosed_growing_text_before_eof(monkeypatch):
    monkeypatch.setattr(safe_xml_module, "_XML_READ_CHUNK_BYTES", 1)

    with pytest.raises(ValueError, match="element text"):
        parse_xml(
            io.BytesIO(b"<root>12345<child/>"),
            max_text_chars=4,
        )


def test_parse_limits_tail_with_one_byte_chunks(monkeypatch):
    monkeypatch.setattr(safe_xml_module, "_XML_READ_CHUNK_BYTES", 1)

    with pytest.raises(ValueError, match="element tail"):
        parse_xml(
            io.BytesIO(b"<root><first/>12345</root>"),
            max_tail_chars=4,
        )


def test_parse_rejects_utf16_entity_declarations():
    payload = (
        '<?xml version="1.0" encoding="utf-16"?>' '<!DOCTYPE root [<!ENTITY x "expanded">]><root>&x;</root>'
    ).encode("utf-16")

    with pytest.raises(ValueError, match="DTD and entity"):
        parse_xml(io.BytesIO(payload))


def test_parse_rejects_nonpositive_limits_without_reading_source():
    class Unreadable:
        def read(self, size):
            raise AssertionError("invalid limits must fail before reading")

    with pytest.raises(ValueError, match="max_depth"):
        parse_xml(Unreadable(), max_depth=0)


def test_path_write_is_atomic_and_parseable(tmp_path):
    destination = tmp_path / "state.xml"
    destination.write_bytes(b"<old/>")
    root = ET.Element("new")

    write_xml(ET.ElementTree(root), destination)

    assert parse_xml(destination).tag == "new"
    assert not list(tmp_path.glob(".state.xml.*.tmp"))


def test_failed_serialization_does_not_replace_existing_path(tmp_path, monkeypatch):
    destination = tmp_path / "state.xml"
    destination.write_bytes(b"<old/>")
    tree = ET.ElementTree(ET.Element("new"))

    def fail_write(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(tree, "write", fail_write)
    with pytest.raises(OSError):
        write_xml(tree, destination)

    assert destination.read_bytes() == b"<old/>"
