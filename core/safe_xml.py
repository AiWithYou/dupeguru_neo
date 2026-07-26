# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Bounded, entity-free XML I/O with atomic path replacement."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

# These defaults cover large preference/result documents while independently
# bounding every XML allocation multiplier. Callers with a narrower schema
# should pass tighter limits.
DEFAULT_MAX_XML_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_XML_ELEMENTS = 4_000_000
DEFAULT_MAX_XML_DEPTH = 64
DEFAULT_MAX_XML_ATTRIBUTES_PER_ELEMENT = 32
DEFAULT_MAX_XML_ATTRIBUTES = 12_000_000
DEFAULT_MAX_XML_NAME_CHARS = 256
DEFAULT_MAX_XML_ATTRIBUTE_CHARS = 1024 * 1024
DEFAULT_MAX_XML_TEXT_CHARS = 4 * 1024 * 1024
DEFAULT_MAX_XML_TAIL_CHARS = 4 * 1024 * 1024
DEFAULT_MAX_XML_TOTAL_CHARS = 128 * 1024 * 1024
_XML_READ_CHUNK_BYTES = 64 * 1024
_FORBIDDEN_DECLARATIONS = (b"<!doctype", b"<!entity")


def _positive_limit(name, value):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def iter_xml_events(
    source,
    *,
    max_bytes=DEFAULT_MAX_XML_BYTES,
    max_elements=DEFAULT_MAX_XML_ELEMENTS,
    max_depth=DEFAULT_MAX_XML_DEPTH,
    max_attributes_per_element=DEFAULT_MAX_XML_ATTRIBUTES_PER_ELEMENT,
    max_attributes=DEFAULT_MAX_XML_ATTRIBUTES,
    max_name_chars=DEFAULT_MAX_XML_NAME_CHARS,
    max_attribute_chars=DEFAULT_MAX_XML_ATTRIBUTE_CHARS,
    max_text_chars=DEFAULT_MAX_XML_TEXT_CHARS,
    max_tail_chars=DEFAULT_MAX_XML_TAIL_CHARS,
    max_total_chars=DEFAULT_MAX_XML_TOTAL_CHARS,
):
    """Yield bounded ``XMLPullParser`` events without retaining a full tree.

    ``end`` events are delayed until the next parser event (or EOF), because
    ElementTree may not finalize an element's tail at the instant its raw end
    event is emitted. A consumer may therefore detach and clear an element as
    soon as this iterator yields its ``end`` event.
    """

    max_bytes = _positive_limit("max_bytes", max_bytes)
    max_elements = _positive_limit("max_elements", max_elements)
    max_depth = _positive_limit("max_depth", max_depth)
    max_attributes_per_element = _positive_limit(
        "max_attributes_per_element",
        max_attributes_per_element,
    )
    max_attributes = _positive_limit("max_attributes", max_attributes)
    max_name_chars = _positive_limit("max_name_chars", max_name_chars)
    max_attribute_chars = _positive_limit(
        "max_attribute_chars",
        max_attribute_chars,
    )
    max_text_chars = _positive_limit("max_text_chars", max_text_chars)
    max_tail_chars = _positive_limit("max_tail_chars", max_tail_chars)
    max_total_chars = _positive_limit("max_total_chars", max_total_chars)
    read_chunk_bytes = _positive_limit(
        "_XML_READ_CHUNK_BYTES",
        _XML_READ_CHUNK_BYTES,
    )

    if hasattr(source, "read"):
        stream = source
        should_close = False
    else:
        stream = open(source, "rb")
        should_close = True

    parser = ET.XMLPullParser(events=("start", "end"))
    total_bytes = 0
    total_elements = 0
    total_attributes = 0
    total_chars = 0
    depth = 0
    saw_root = False
    declaration_probe = b""
    pending_end = None
    live_elements = []

    def add_characters(count):
        nonlocal total_chars
        total_chars += count
        if total_chars > max_total_chars:
            raise ValueError("XML string content exceeds the supported total limit")

    def finalize_pending_tail():
        nonlocal pending_end
        if pending_end is None:
            return None
        tail = pending_end.tail or ""
        if len(tail) > max_tail_chars:
            raise ValueError("XML element tail exceeds the supported limit")
        add_characters(len(tail))
        completed = pending_end
        pending_end = None
        return "end", completed

    def inspect_incomplete_content():
        provisional_chars = total_chars
        for element in live_elements:
            text = element.text or ""
            if len(text) > max_text_chars:
                raise ValueError("XML element text exceeds the supported limit")
            provisional_chars += len(text)
        if pending_end is not None:
            tail = pending_end.tail or ""
            if len(tail) > max_tail_chars:
                raise ValueError("XML element tail exceeds the supported limit")
            provisional_chars += len(tail)
        if provisional_chars > max_total_chars:
            raise ValueError("XML string content exceeds the supported total limit")

    def inspect_events():
        nonlocal depth, pending_end, saw_root, total_attributes, total_elements
        for event, element in parser.read_events():
            completed = finalize_pending_tail()
            if completed is not None:
                yield completed
            if event == "start":
                depth += 1
                if depth > max_depth:
                    raise ValueError("XML element depth exceeds the supported limit")
                total_elements += 1
                if total_elements > max_elements:
                    raise ValueError("XML element count exceeds the supported limit")
                saw_root = True
                live_elements.append(element)
                tag = element.tag
                if not isinstance(tag, str) or len(tag) > max_name_chars:
                    raise ValueError("XML element name exceeds the supported limit")
                attribute_count = len(element.attrib)
                if attribute_count > max_attributes_per_element:
                    raise ValueError("XML element attribute count exceeds the supported limit")
                total_attributes += attribute_count
                if total_attributes > max_attributes:
                    raise ValueError("XML attribute count exceeds the supported limit")
                add_characters(len(tag))
                for name, value in element.attrib.items():
                    if (
                        not isinstance(name, str)
                        or not isinstance(value, str)
                        or len(name) > max_name_chars
                        or len(value) > max_attribute_chars
                    ):
                        raise ValueError("XML attribute string exceeds the supported limit")
                    add_characters(len(name) + len(value))
                yield event, element
                continue

            text = element.text or ""
            if len(text) > max_text_chars:
                raise ValueError("XML element text exceeds the supported limit")
            add_characters(len(text))
            if not live_elements or live_elements[-1] is not element:
                raise ET.ParseError("unbalanced XML elements")
            live_elements.pop()
            depth -= 1
            pending_end = element

    try:
        while True:
            remaining = max_bytes - total_bytes
            chunk = stream.read(min(read_chunk_bytes, remaining + 1))
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            if not isinstance(chunk, bytes):
                raise TypeError("XML source must return bytes or text")
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise ValueError("XML document exceeds the supported size limit")
            normalized = chunk.replace(b"\0", b"").lower()
            combined_probe = declaration_probe + normalized
            if any(token in combined_probe for token in _FORBIDDEN_DECLARATIONS):
                raise ValueError("DTD and entity declarations are not allowed")
            declaration_probe = combined_probe[-64:]
            parser.feed(chunk)
            yield from inspect_events()
            inspect_incomplete_content()
        parser.close()
        yield from inspect_events()
        inspect_incomplete_content()
        completed = finalize_pending_tail()
        if completed is not None:
            yield completed
    finally:
        if should_close:
            stream.close()

    if not saw_root:
        raise ET.ParseError("no element found")
    if depth != 0:
        raise ET.ParseError("unbalanced XML elements")


def parse_xml(
    source,
    *,
    max_bytes=DEFAULT_MAX_XML_BYTES,
    max_elements=DEFAULT_MAX_XML_ELEMENTS,
    max_depth=DEFAULT_MAX_XML_DEPTH,
    max_attributes_per_element=DEFAULT_MAX_XML_ATTRIBUTES_PER_ELEMENT,
    max_attributes=DEFAULT_MAX_XML_ATTRIBUTES,
    max_name_chars=DEFAULT_MAX_XML_NAME_CHARS,
    max_attribute_chars=DEFAULT_MAX_XML_ATTRIBUTE_CHARS,
    max_text_chars=DEFAULT_MAX_XML_TEXT_CHARS,
    max_tail_chars=DEFAULT_MAX_XML_TAIL_CHARS,
    max_total_chars=DEFAULT_MAX_XML_TOTAL_CHARS,
):
    """Parse XML while enforcing independent byte, shape, and string limits."""

    root = None
    for event, element in iter_xml_events(
        source,
        max_bytes=max_bytes,
        max_elements=max_elements,
        max_depth=max_depth,
        max_attributes_per_element=max_attributes_per_element,
        max_attributes=max_attributes,
        max_name_chars=max_name_chars,
        max_attribute_chars=max_attribute_chars,
        max_text_chars=max_text_chars,
        max_tail_chars=max_tail_chars,
        max_total_chars=max_total_chars,
    ):
        if event == "start" and root is None:
            root = element
    if root is None:
        raise ET.ParseError("no element found")
    return root


def _fsync_directory(path):
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(os.fspath(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_xml_stream(write_document, destination):
    """Write an XML document from ``write_document``.

    ``write_document`` receives a binary stream. Path destinations are written
    to a sibling temporary file, flushed to stable storage, and atomically
    replaced. File-like destinations are intentionally written directly; a
    caller that needs rollback must provide a path destination.
    """

    if hasattr(destination, "write"):
        write_document(destination)
        return
    destination = Path(destination).absolute()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(destination.name),
        suffix=".tmp",
        dir=os.fspath(destination.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            write_document(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(os.fspath(temporary), os.fspath(destination))
        _fsync_directory(destination.parent)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def write_xml(tree, destination, *, xml_declaration=True):
    write_xml_stream(
        lambda stream: tree.write(
            stream,
            encoding="utf-8",
            xml_declaration=xml_declaration,
        ),
        destination,
    )
