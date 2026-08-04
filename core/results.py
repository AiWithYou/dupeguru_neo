# Created By: Virgil Dupras
# Created On: 2006/02/23
# Copyright 2015 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import logging
import re
import os
import os.path as op
import time
from dataclasses import dataclass
from errno import EISDIR, EACCES

from hscommon.jobprogress.job import nulljob
from hscommon.conflict import get_conflicted_name
from hscommon.util import flatten, nonone, format_size
from hscommon.trans import tr

from core import engine
from core.markable import Markable
from core.safe_xml import iter_xml_events, write_xml_stream

RESULTS_SCHEMA_VERSION = 3
MAX_RESULTS_XML_BYTES = 256 * 1024 * 1024
# Count caps are independent of the byte cap so very short XML records cannot
# trigger unbounded Python object creation. Compact exact groups remain O(k)
# and intentionally receive a much larger per-group file allowance.
MAX_RESULTS_GROUPS = 100_000
MAX_RESULTS_TOTAL_FILES = 1_000_000
MAX_RESULTS_FILES_PER_GROUP = 250_000
MAX_RESULTS_TOTAL_MATCHES = 2_000_000
MAX_RESULTS_MATCHES_PER_GROUP = engine.MAX_SIMILAR_MATCHES_PER_GROUP
MAX_RESULTS_XML_ELEMENTS = 1 + MAX_RESULTS_GROUPS + MAX_RESULTS_TOTAL_FILES + MAX_RESULTS_TOTAL_MATCHES
MAX_RESULTS_XML_ATTRIBUTES_PER_ELEMENT = 16
MAX_RESULTS_XML_ATTRIBUTES = 12_000_000
MAX_RESULTS_XML_DEPTH = 3
MAX_RESULTS_XML_NAME_CHARS = 64
MAX_RESULTS_XML_ATTRIBUTE_CHARS = 64 * 1024
MAX_RESULTS_XML_TEXT_CHARS = 1024 * 1024
MAX_RESULTS_XML_TAIL_CHARS = 1024 * 1024
MAX_RESULTS_XML_TOTAL_CHARS = 128 * 1024 * 1024
MAX_RESULTS_WORDS_PER_FILE = 4_096
# Legacy reports without serialized matches require all-pairs reconstruction.
# 256 files produce 32,640 pairs; the next file would exceed this small,
# explicit compatibility budget.
MAX_LEGACY_RECONSTRUCTED_MATCHES = 32_768
# Legacy schema versions omitted match percentages and require replaying the
# old list-based word comparator. Bound its conservative worst-case character
# comparison work independently of the pair count.
MAX_LEGACY_RECONSTRUCTION_WORK = 10_000_000

_RESULTS_ROOT_ATTRIBUTES = frozenset(
    {
        "schema_version",
        "saved_at_ns",
        "destructive_proof",
        "scan_id",
        "scan_status",
        "scan_complete",
    }
)
_RESULTS_GROUP_ATTRIBUTES = frozenset(
    {
        "relation",
        "algorithm",
        "digest",
        "size",
    }
)
_RESULTS_FILE_ATTRIBUTES = frozenset({"path", "words", "is_ref", "marked"})
_RESULTS_MATCH_ATTRIBUTES = frozenset({"first", "second", "percentage"})
_EXACT_REPORT_RELATIONS = frozenset(
    {
        engine.VerificationKind.VERIFIED_EXACT.value,
        "reported_exact",
    }
)
_NONEXACT_REPORT_RELATIONS = frozenset(
    {
        "",
        engine.VerificationKind.UNVERIFIED.value,
        engine.VerificationKind.SIMILAR.value,
    }
)
_COMPACT_TRANSITIVE_RELATIONS = frozenset({"folder_manifest"})
_MISSING_ATTRIBUTE = object()
_XML_DECLARATION = b"<?xml version='1.0' encoding='utf-8'?>\n"
_XML_DECLARATION_BYTES = len(_XML_DECLARATION)
_MATCH_PERCENTAGE_SENTINEL = 255
_EMPTY_SAVED_WORDS = ("",)


@dataclass(frozen=True, slots=True)
class _SavedFileRecord:
    path: str
    words: tuple[str, ...]
    is_ref: bool


@dataclass(frozen=True, slots=True)
class _SavedMatchRecord:
    first: int
    second: int
    percentage: int


@dataclass(frozen=True, slots=True)
class _SavedGroupRecord:
    files: tuple[_SavedFileRecord, ...]
    matches: tuple[_SavedMatchRecord, ...]
    exact_evidence: object = None
    relation: str = ""


@dataclass(frozen=True, slots=True)
class _SavedResultsDocument:
    schema_version: int
    groups: tuple[_SavedGroupRecord, ...]
    metadata: dict


@dataclass(slots=True)
class _SavedGroupBuilder:
    relation: str
    exact_evidence: object
    files: list
    matches: list
    reached_matches: bool = False
    match_pair_bits: bytearray | None = None


def _read_results_xml(source):
    return iter_xml_events(
        source,
        max_bytes=MAX_RESULTS_XML_BYTES,
        max_elements=MAX_RESULTS_XML_ELEMENTS,
        max_depth=MAX_RESULTS_XML_DEPTH,
        max_attributes_per_element=MAX_RESULTS_XML_ATTRIBUTES_PER_ELEMENT,
        max_attributes=MAX_RESULTS_XML_ATTRIBUTES,
        max_name_chars=MAX_RESULTS_XML_NAME_CHARS,
        max_attribute_chars=MAX_RESULTS_XML_ATTRIBUTE_CHARS,
        max_text_chars=MAX_RESULTS_XML_TEXT_CHARS,
        max_tail_chars=MAX_RESULTS_XML_TAIL_CHARS,
        max_total_chars=MAX_RESULTS_XML_TOTAL_CHARS,
    )


def _require_known_attributes(element, allowed):
    unknown = sorted(set(element.attrib) - allowed)
    if unknown:
        raise ValueError(
            "Unsupported {} attribute(s): {}".format(
                element.tag,
                ", ".join(unknown),
            )
        )


def _require_whitespace_content(element):
    if element.text and not element.text.isspace():
        raise ValueError(f"Results XML element {element.tag!r} cannot contain text")
    if element.tail and not element.tail.isspace():
        raise ValueError(f"Results XML element {element.tag!r} cannot contain tail text")


def _parse_nonnegative_integer(value, label, *, default=None):
    if value is None:
        if default is not None:
            return default
        raise ValueError(f"Missing {label}")
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError(f"Invalid {label}") from error
    if result < 0:
        raise ValueError(f"Invalid {label}")
    return result


def _validate_legacy_reconstruction_work(file_records):
    work = 0
    prefix_characters = 0
    prefix_file_count = 0
    for record in file_records:
        word_count = max(len(record.words), 1)
        character_count = sum(max(len(word), 1) for word in record.words)
        # engine.compare(first, second) performs list membership, an optional
        # front comparison, and list removal for every first-side word. The
        # factor of three bounds those scans; the second term covers copying
        # and other linear work for both lists.
        work += 3 * prefix_characters * word_count
        work += prefix_characters + prefix_file_count * character_count
        if work > MAX_LEGACY_RECONSTRUCTION_WORK:
            raise ValueError("Legacy match reconstruction exceeds the supported work limit")
        prefix_characters += character_count
        prefix_file_count += 1
    return work


def _parse_saved_results(source):
    stack = []
    group_records = []
    current_group = None
    schema_version = None
    saved_at_ns = 0
    metadata = None
    total_files = 0
    total_matches = 0
    legacy_reconstruction_work = 0

    for event, element in _read_results_xml(source):
        if event == "start":
            parent = stack[-1] if stack else None
            if parent is None:
                if element.tag != "results":
                    raise ValueError("Results XML root must be 'results'")
                _require_known_attributes(element, _RESULTS_ROOT_ATTRIBUTES)
                schema_version = _parse_nonnegative_integer(
                    element.get("schema_version"),
                    "results schema version",
                    default=1,
                )
                if schema_version < 1 or schema_version > RESULTS_SCHEMA_VERSION:
                    raise ValueError("Unsupported results schema version: {}".format(schema_version))
                if element.get("scan_complete") not in {None, "y", "n"}:
                    raise ValueError("Invalid saved scan_complete value")
                saved_at_ns = _parse_nonnegative_integer(
                    element.get("saved_at_ns"),
                    "saved_at_ns",
                    default=0,
                )
                metadata = {
                    "scan_id": element.get("scan_id", ""),
                    "scan_status": element.get("scan_status", ""),
                    "scan_complete": element.get("scan_complete", "n") == "y",
                }
            elif len(stack) == 1:
                if parent.tag != "results" or element.tag != "group":
                    raise ValueError("Results XML root may contain only direct group elements")
                if len(group_records) >= MAX_RESULTS_GROUPS:
                    raise ValueError("Results XML group count exceeds the supported limit")
                _require_known_attributes(element, _RESULTS_GROUP_ATTRIBUTES)
                relation = element.get("relation", "")
                if relation not in (
                    _EXACT_REPORT_RELATIONS | _NONEXACT_REPORT_RELATIONS | _COMPACT_TRANSITIVE_RELATIONS
                ):
                    raise ValueError("Unsupported saved group relation")
                if relation in _COMPACT_TRANSITIVE_RELATIONS and schema_version < 3:
                    raise ValueError("Compact folder-manifest groups require results schema version 3")
                exact_evidence = None
                if relation in _EXACT_REPORT_RELATIONS:
                    algorithm = element.get("algorithm")
                    digest_text = element.get("digest")
                    if not algorithm or not digest_text:
                        raise ValueError("Compact exact result metadata is incomplete")
                    try:
                        digest = bytes.fromhex(digest_text)
                    except ValueError as error:
                        raise ValueError("Invalid compact exact digest") from error
                    if not digest or len(digest) > 64:
                        raise ValueError("Invalid compact exact digest")
                    exact_evidence = engine.ReportedExactEvidence(
                        algorithm=algorithm,
                        digest=digest,
                        size=_parse_nonnegative_integer(
                            element.get("size"),
                            "compact exact size",
                        ),
                        saved_at_ns=saved_at_ns,
                    )
                elif relation in _COMPACT_TRANSITIVE_RELATIONS and any(
                    element.get(name) is not None for name in ("algorithm", "digest", "size")
                ):
                    raise ValueError("Compact folder-manifest groups cannot claim file-content evidence")
                current_group = _SavedGroupBuilder(
                    relation=relation,
                    exact_evidence=exact_evidence,
                    files=[],
                    matches=[],
                )
            elif len(stack) == 2:
                if parent.tag != "group" or element.tag not in {"file", "match"}:
                    raise ValueError("Results XML groups may contain only direct file and match elements")
                if current_group is None:
                    raise ValueError("Results XML group state is invalid")
                if element.tag == "file":
                    if current_group.reached_matches:
                        raise ValueError("Results XML file elements must precede match elements")
                    _require_known_attributes(element, _RESULTS_FILE_ATTRIBUTES)
                else:
                    current_group.reached_matches = True
                    _require_known_attributes(element, _RESULTS_MATCH_ATTRIBUTES)
            else:
                raise ValueError("Results XML file and match elements cannot have children")
            stack.append(element)
            continue

        if not stack or stack[-1] is not element:
            raise ValueError("Results XML element nesting is invalid")
        _require_whitespace_content(element)
        parent = stack[-2] if len(stack) > 1 else None

        if element.tag == "file":
            if current_group is None:
                raise ValueError("Results XML group state is invalid")
            path = element.get("path")
            if not path:
                raise ValueError("Saved result file path is required")
            is_ref = element.get("is_ref", "n")
            marked = element.get("marked", "n")
            if is_ref not in {"y", "n"} or marked not in {"y", "n"}:
                raise ValueError("Invalid saved file boolean attribute")
            words_text = element.get("words", "")
            if words_text.count(",") + 1 > MAX_RESULTS_WORDS_PER_FILE:
                raise ValueError("Saved result word count exceeds the supported limit")
            if len(current_group.files) >= MAX_RESULTS_FILES_PER_GROUP:
                raise ValueError("Results XML per-group file count exceeds the supported limit")
            total_files += 1
            if total_files > MAX_RESULTS_TOTAL_FILES:
                raise ValueError("Results XML total file count exceeds the supported limit")
            current_group.files.append(
                _SavedFileRecord(
                    path=path,
                    words=(_EMPTY_SAVED_WORDS if not words_text else tuple(words_text.split(","))),
                    is_ref=is_ref == "y",
                )
            )
        elif element.tag == "match":
            if current_group is None:
                raise ValueError("Results XML group state is invalid")
            if len(current_group.matches) >= MAX_RESULTS_MATCHES_PER_GROUP:
                raise ValueError("Results XML per-group match count exceeds the supported limit")
            total_matches += 1
            if total_matches > MAX_RESULTS_TOTAL_MATCHES:
                raise ValueError("Results XML total match count exceeds the supported limit")
            if current_group.relation in _NONEXACT_REPORT_RELATIONS:
                file_count = len(current_group.files)
                first_index = _parse_nonnegative_integer(
                    element.get("first"),
                    "saved match first index",
                )
                second_index = _parse_nonnegative_integer(
                    element.get("second"),
                    "saved match second index",
                )
                percentage = _parse_nonnegative_integer(
                    element.get("percentage"),
                    "saved match percentage",
                )
                if (
                    first_index >= file_count
                    or second_index >= file_count
                    or first_index == second_index
                    or percentage > 100
                ):
                    raise ValueError("Saved match values are outside the supported range")
                if second_index < first_index:
                    first_index, second_index = second_index, first_index
                pair_count = file_count * (file_count - 1) // 2
                if pair_count > MAX_RESULTS_MATCHES_PER_GROUP:
                    raise ValueError("Saved similarity group does not contain a complete match graph")
                if current_group.match_pair_bits is None:
                    current_group.match_pair_bits = bytearray((pair_count + 7) // 8)
                pair_offset = _pair_offset(first_index, second_index, file_count)
                byte_offset, bit_offset = divmod(pair_offset, 8)
                pair_mask = 1 << bit_offset
                if current_group.match_pair_bits[byte_offset] & pair_mask:
                    raise ValueError("Saved similarity group contains a duplicate match pair")
                current_group.match_pair_bits[byte_offset] |= pair_mask
                current_group.matches.append(
                    _SavedMatchRecord(
                        first=first_index,
                        second=second_index,
                        percentage=percentage,
                    )
                )
            else:
                # Exact and folder-manifest relations reject matches at group
                # finalization, after the same count limits as legacy parsing.
                current_group.matches.append(None)
        elif element.tag == "group":
            if current_group is None:
                raise ValueError("Results XML group state is invalid")
            file_count = len(current_group.files)
            if file_count < 2:
                raise ValueError("A saved results group requires at least two files")
            if current_group.relation in _EXACT_REPORT_RELATIONS:
                if current_group.matches:
                    raise ValueError("Compact exact result groups cannot contain match elements")
            elif current_group.relation in _COMPACT_TRANSITIVE_RELATIONS:
                if current_group.matches:
                    raise ValueError("Compact folder-manifest groups cannot contain match elements")
            elif current_group.matches:
                if len(current_group.matches) != file_count * (file_count - 1) // 2:
                    raise ValueError("Saved similarity group does not contain a complete match graph")
            else:
                pair_count = file_count * (file_count - 1) // 2
                if pair_count > MAX_LEGACY_RECONSTRUCTED_MATCHES:
                    raise ValueError("Legacy match reconstruction exceeds the supported pair limit")
                legacy_reconstruction_work += _validate_legacy_reconstruction_work(current_group.files)
                if legacy_reconstruction_work > MAX_LEGACY_RECONSTRUCTION_WORK:
                    raise ValueError("Legacy match reconstruction exceeds the supported work limit")
            group_records.append(
                _SavedGroupRecord(
                    files=tuple(current_group.files),
                    matches=tuple(current_group.matches),
                    exact_evidence=current_group.exact_evidence,
                    relation=current_group.relation,
                )
            )
            current_group = None
        elif element.tag == "results":
            if current_group is not None:
                raise ValueError("Results XML group state is invalid")

        stack.pop()
        _release_results_xml_element(parent, element)

    if stack or schema_version is None or metadata is None:
        raise ValueError("Results XML document is incomplete")
    return _SavedResultsDocument(
        schema_version=schema_version,
        groups=tuple(group_records),
        metadata=metadata,
    )


def _release_results_xml_element(parent, element):
    if parent is not None:
        parent.remove(element)
    element.clear()


def _is_xml_10_text(value):
    return all(
        character in "\t\n\r"
        or "\x20" <= character <= "\ud7ff"
        or "\ue000" <= character <= "\ufffd"
        or "\U00010000" <= character <= "\U0010ffff"
        for character in value
    )


def _escaped_xml_attribute(value):
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\r", "&#13;")
        .replace("\n", "&#10;")
        .replace("\t", "&#09;")
    )


def _group_xml_attributes(group):
    verification_kind = getattr(
        group,
        "verification_kind",
        engine.VerificationKind.UNVERIFIED,
    )
    if verification_kind is engine.VerificationKind.VERIFIED_EXACT:
        relation = verification_kind.value
    elif getattr(group, "compact_relation", None) in _COMPACT_TRANSITIVE_RELATIONS:
        relation = group.compact_relation
    elif getattr(group, "_is_exact", False):
        relation = "reported_exact"
    else:
        relation = verification_kind.value
    attributes = {"relation": relation}
    evidence = getattr(group, "evidence", None)
    if evidence is not None and getattr(group, "_is_exact", False):
        algorithm = str(getattr(evidence, "algorithm", ""))
        digest = getattr(evidence, "digest", b"")
        size = getattr(evidence, "size", None)
        if (
            not algorithm
            or not isinstance(digest, bytes)
            or not digest
            or len(digest) > 64
            or type(size) is not int
            or size < 0
        ):
            raise ValueError("Compact exact result metadata is incomplete")
        attributes.update(
            {
                "algorithm": algorithm,
                "digest": digest.hex(),
                "size": str(size),
            }
        )
    if relation in _EXACT_REPORT_RELATIONS and not {
        "algorithm",
        "digest",
        "size",
    }.issubset(attributes):
        raise ValueError("Compact exact result metadata is incomplete")
    return attributes


def _file_xml_attributes(file, *, marked):
    try:
        words = engine.unpack_fields(file.words)
    except AttributeError:
        words = ()
    words_text = ",".join(words)
    if words_text.count(",") + 1 > MAX_RESULTS_WORDS_PER_FILE:
        raise ValueError("Results word count exceeds the supported save limit")
    path = str(file.path)
    if not path:
        raise ValueError("Saved result file path is required")
    return {
        "path": path,
        "words": words_text,
        "is_ref": "y" if file.is_ref else "n",
        "marked": "y" if marked else "n",
    }


def _compact_group_files_are_unique(group, file_count):
    """Validate compact membership with one transient O(k) identity set."""

    if len(set(group)) != file_count:
        raise ValueError("A saved results group cannot contain the same file twice")


def _pair_offset(first_index, second_index, file_count):
    return first_index * (2 * file_count - first_index - 1) // 2 + second_index - first_index - 1


def _indexed_match_percentages(group, file_indices, file_count):
    """Return canonical clique percentages in a compact byte array."""

    match_count = len(group.matches)
    expected_match_count = file_count * (file_count - 1) // 2
    if match_count != expected_match_count:
        raise ValueError("A saved similarity group does not contain a complete match graph")
    percentages = bytearray([_MATCH_PERCENTAGE_SENTINEL]) * expected_match_count
    for match in group.matches:
        try:
            first_index = file_indices[match.first]
            second_index = file_indices[match.second]
        except (AttributeError, KeyError) as error:
            raise ValueError("A saved match references a file outside its group") from error
        percentage = match.percentage
        if first_index == second_index or type(percentage) is not int or not 0 <= percentage <= 100:
            raise ValueError("A saved match has values outside the supported range")
        if second_index < first_index:
            first_index, second_index = second_index, first_index
        offset = _pair_offset(first_index, second_index, file_count)
        if percentages[offset] != _MATCH_PERCENTAGE_SENTINEL:
            raise ValueError("A saved similarity group contains a duplicate match pair")
        percentages[offset] = percentage
    return percentages


def _iter_indexed_match_percentages(percentages, file_count):
    offset = 0
    for first_index in range(file_count - 1):
        for second_index in range(first_index + 1, file_count):
            yield first_index, second_index, percentages[offset]
            offset += 1


def _validate_results_save_contract(groups, root_attributes):
    """Preflight the exact shape accepted by :func:`_parse_saved_results`."""

    if len(groups) > MAX_RESULTS_GROUPS:
        raise ValueError("Results group count exceeds the supported save limit")

    element_count = 0
    attribute_count = 0
    total_characters = 0
    projected_bytes = _XML_DECLARATION_BYTES
    total_files = 0
    total_matches = 0

    def add_element(tag, attributes, *, container):
        nonlocal attribute_count, element_count, projected_bytes, total_characters
        element_count += 1
        if element_count > MAX_RESULTS_XML_ELEMENTS:
            raise ValueError("Results element count exceeds the supported save limit")
        if len(attributes) > MAX_RESULTS_XML_ATTRIBUTES_PER_ELEMENT:
            raise ValueError("Results element attribute count exceeds the supported save limit")
        attribute_count += len(attributes)
        if attribute_count > MAX_RESULTS_XML_ATTRIBUTES:
            raise ValueError("Results attribute count exceeds the supported save limit")
        total_characters += len(tag)
        projected_bytes += (2 * len(tag) + 5) if container else (len(tag) + 4)
        for name, raw_value in attributes.items():
            value = str(raw_value)
            if len(name) > MAX_RESULTS_XML_NAME_CHARS or len(value) > MAX_RESULTS_XML_ATTRIBUTE_CHARS:
                raise ValueError("Results attribute string exceeds the supported save limit")
            if not _is_xml_10_text(name) or not _is_xml_10_text(value):
                raise ValueError("Results contain a character XML 1.0 cannot represent")
            total_characters += len(name) + len(value)
            projected_bytes += len(name.encode("utf-8")) + 4 + len(_escaped_xml_attribute(value).encode("utf-8"))
        if total_characters > MAX_RESULTS_XML_TOTAL_CHARS:
            raise ValueError("Results string content exceeds the supported total save limit")
        if projected_bytes > MAX_RESULTS_XML_BYTES:
            raise ValueError("Results XML exceeds the supported byte save limit")

    add_element("results", root_attributes, container=True)
    for group in groups:
        group_attributes = _group_xml_attributes(group)
        add_element("group", group_attributes, container=True)

        file_count = len(group)
        if file_count < 2:
            raise ValueError("A saved results group requires at least two files")
        if file_count > MAX_RESULTS_FILES_PER_GROUP:
            raise ValueError("Results per-group file count exceeds the supported save limit")
        total_files += file_count
        if total_files > MAX_RESULTS_TOTAL_FILES:
            raise ValueError("Results total file count exceeds the supported save limit")

        is_compact = bool(getattr(group, "_is_exact", False))
        file_indices = None if is_compact else {}
        if is_compact:
            _compact_group_files_are_unique(group, file_count)
        for index, file in enumerate(group):
            if file_indices is not None:
                if file in file_indices:
                    raise ValueError("A saved results group cannot contain the same file twice")
                file_indices[file] = index
            add_element(
                "file",
                _file_xml_attributes(file, marked=False),
                container=False,
            )

        if is_compact:
            continue
        match_count = len(group.matches)
        if match_count > MAX_RESULTS_MATCHES_PER_GROUP:
            raise ValueError("Results per-group match count exceeds the supported save limit")
        total_matches += match_count
        if total_matches > MAX_RESULTS_TOTAL_MATCHES:
            raise ValueError("Results total match count exceeds the supported save limit")
        percentages = _indexed_match_percentages(
            group,
            file_indices,
            file_count,
        )
        for first_index, second_index, percentage in _iter_indexed_match_percentages(percentages, file_count):
            add_element(
                "match",
                {
                    "first": str(first_index),
                    "second": str(second_index),
                    "percentage": str(percentage),
                },
                container=False,
            )
    return projected_bytes


def _xml_element_bytes(tag, attributes, *, empty):
    payload = bytearray(b"<")
    payload.extend(tag.encode("utf-8"))
    for name, raw_value in attributes.items():
        value = str(raw_value)
        if not _is_xml_10_text(name) or not _is_xml_10_text(value):
            raise ValueError("Results contain a character XML 1.0 cannot represent")
        payload.extend(b" ")
        payload.extend(name.encode("utf-8"))
        payload.extend(b'="')
        payload.extend(_escaped_xml_attribute(value).encode("utf-8"))
        payload.extend(b'"')
    payload.extend(b" />" if empty else b">")
    return bytes(payload)


class _BoundedResultsWriter:
    def __init__(self, stream):
        self.stream = stream
        self.bytes_written = 0

    def write(self, payload):
        projected = self.bytes_written + len(payload)
        if projected > MAX_RESULTS_XML_BYTES:
            raise ValueError("Results XML exceeds the supported byte save limit")
        written = self.stream.write(payload)
        if written is not None and written != len(payload):
            raise OSError("Could not write the complete Results XML document")
        self.bytes_written = projected


def _write_results_xml(stream, groups, root_attributes, is_marked, expected_bytes):
    output = _BoundedResultsWriter(stream)
    output.write(_XML_DECLARATION)
    output.write(_xml_element_bytes("results", root_attributes, empty=False))
    for group in groups:
        output.write(
            _xml_element_bytes(
                "group",
                _group_xml_attributes(group),
                empty=False,
            )
        )
        file_count = len(group)
        is_compact = bool(getattr(group, "_is_exact", False))
        file_indices = None if is_compact else {}
        if is_compact:
            _compact_group_files_are_unique(group, file_count)
        actual_file_count = 0
        for index, file in enumerate(group):
            actual_file_count += 1
            if file_indices is not None:
                if file in file_indices:
                    raise ValueError("A saved results group cannot contain the same file twice")
                file_indices[file] = index
            output.write(
                _xml_element_bytes(
                    "file",
                    _file_xml_attributes(file, marked=is_marked(file)),
                    empty=True,
                )
            )
        if actual_file_count != file_count:
            raise ValueError("Results changed while the XML document was being saved")
        if not is_compact:
            percentages = _indexed_match_percentages(
                group,
                file_indices,
                file_count,
            )
            for first_index, second_index, percentage in _iter_indexed_match_percentages(percentages, file_count):
                output.write(
                    _xml_element_bytes(
                        "match",
                        {
                            "first": str(first_index),
                            "second": str(second_index),
                            "percentage": str(percentage),
                        },
                        empty=True,
                    )
                )
        output.write(b"</group>")
    output.write(b"</results>")
    if output.bytes_written != expected_bytes:
        raise ValueError("Results changed while the XML document was being saved")


class Results(Markable):
    """Manages a collection of duplicate :class:`~core.engine.Group`.

    This class takes care or marking, sorting and filtering duplicate groups.

    .. attribute:: groups

        The list of :class:`~core.engine.Group` contained managed by this instance.

    .. attribute:: dupes

        A list of all duplicates (:class:`~core.fs.File` instances), without ref, contained in the
        currently managed :attr:`groups`.
    """

    # ---Override
    def __init__(self, app):
        Markable.__init__(self)
        self.__groups = []
        self.__group_of_duplicate = {}
        self.__groups_sort_descriptor = None  # This is a tuple (key, asc)
        self.__dupes = None
        self.__dupes_sort_descriptor = None  # This is a tuple (key, asc, delta)
        self.__filters = None
        self.__filtered_dupes = None
        self.__filtered_groups = None
        self.__recalculate_stats()
        self.__marked_size = 0
        self.app = app
        self.problems = []  # (dupe, error_msg)
        self.is_modified = False
        self.refresh_required = False
        self.loaded_report = False
        self.loaded_schema_version = None
        self.loaded_report_metadata = {}
        self.scan_receipt = None

    def _did_mark(self, dupe):
        self.__marked_size += dupe.size

    def _did_unmark(self, dupe):
        self.__marked_size -= dupe.size

    def _get_markable_count(self):
        return self.__total_count

    def _is_markable(self, dupe):
        if dupe.is_ref:
            return False
        g = self.get_group_of_duplicate(dupe)
        if not g:
            return False
        if dupe is g.ref:
            return False
        if self.__filtered_dupes is not None and dupe not in self.__filtered_dupes:
            return False
        return True

    def mark_all(self):
        if self.__filters:
            self.mark_multiple(self.__filtered_dupes)
        else:
            Markable.mark_all(self)

    def mark_invert(self):
        if self.__filters:
            self.mark_toggle_multiple(self.__filtered_dupes)
        else:
            Markable.mark_invert(self)

    def mark_none(self):
        if self.__filters:
            self.unmark_multiple(self.__filtered_dupes)
        else:
            Markable.mark_none(self)

    # ---Private
    def __get_dupe_list(self):
        if self.__dupes is None or self.refresh_required:
            self.__dupes = flatten(group.dupes for group in self.groups)
            self.refresh_required = False
            if None in self.__dupes:
                # This is debug logging to try to figure out #44
                logging.warning(
                    "There is a None value in the Results' dupe list. dupes: %r groups: %r",
                    self.__dupes,
                    self.groups,
                )
            if self.__filtered_dupes is not None:
                self.__dupes = [dupe for dupe in self.__dupes if dupe in self.__filtered_dupes]
            sd = self.__dupes_sort_descriptor
            if sd:
                self.sort_dupes(sd[0], sd[1], sd[2])
        return self.__dupes

    def __get_groups(self):
        if self.__filtered_groups is None:
            return self.__groups
        else:
            return self.__filtered_groups

    def __get_stat_line(self):
        if self.__filtered_dupes is None:
            mark_count = self.mark_count
            marked_size = self.__marked_size
            total_count = self.__total_count
            total_size = self.__total_size
        else:
            mark_count = len([dupe for dupe in self.__filtered_dupes if self.is_marked(dupe)])
            marked_size = sum(dupe.size for dupe in self.__filtered_dupes if self.is_marked(dupe))
            total_count = len([dupe for dupe in self.__filtered_dupes if self.is_markable(dupe)])
            total_size = sum(dupe.size for dupe in self.__filtered_dupes if self.is_markable(dupe))
        if self.mark_inverted:
            marked_size = self.__total_size - marked_size
        result = tr("%d / %d (%s / %s) duplicates marked.") % (
            mark_count,
            total_count,
            format_size(marked_size, 2),
            format_size(total_size, 2),
        )
        if self.__filters:
            result += tr(" filter: %s") % " --> ".join(self.__filters)
        return result

    def __recalculate_stats(self):
        self.__total_size = 0
        self.__total_count = 0
        for group in self.groups:
            markable = [dupe for dupe in group.dupes if self._is_markable(dupe)]
            self.__total_count += len(markable)
            self.__total_size += sum(dupe.size for dupe in markable)

    def __set_groups(self, new_groups):
        self.mark_none()
        self.__groups = new_groups
        self.loaded_report = False
        self.loaded_schema_version = None
        self.loaded_report_metadata = {}
        self.scan_receipt = None
        self.__group_of_duplicate = {}
        for g in self.__groups:
            for dupe in g:
                self.__group_of_duplicate[dupe] = g
                if not hasattr(dupe, "is_ref"):
                    dupe.is_ref = False
        self.is_modified = bool(self.__groups)
        old_filters = nonone(self.__filters, [])
        self.apply_filter(None)
        for filter_str in old_filters:
            self.apply_filter(filter_str)

    # ---Public
    def apply_filter(self, filter_str):
        """Applies a filter ``filter_str`` to :attr:`groups`

        When you apply the filter, only  dupes with the filename matching ``filter_str`` will be in
        in the results. To cancel the filter, just call apply_filter with ``filter_str`` to None,
        and the results will go back to normal.

        If call apply_filter on a filtered results, the filter will be applied
        *on the filtered results*.

        :param str filter_str: a string containing a regexp to filter dupes with.
        """
        if not filter_str:
            self.__filtered_dupes = None
            self.__filtered_groups = None
            self.__filters = None
        else:
            if not self.__filters:
                self.__filters = []
            try:
                filter_re = re.compile(filter_str, re.IGNORECASE)
            except re.error:
                return  # don't apply this filter.
            self.__filters.append(filter_str)
            if self.__filtered_dupes is None:
                self.__filtered_dupes = flatten(g[:] for g in self.groups)
            self.__filtered_dupes = {dupe for dupe in self.__filtered_dupes if filter_re.search(str(dupe.path))}
            filtered_groups = set()
            for dupe in self.__filtered_dupes:
                filtered_groups.add(self.get_group_of_duplicate(dupe))
            self.__filtered_groups = list(filtered_groups)
        self.__recalculate_stats()
        sd = self.__groups_sort_descriptor
        if sd:
            self.sort_groups(sd[0], sd[1])
        self.__dupes = None

    def get_group_of_duplicate(self, dupe):
        """Returns :class:`~core.engine.Group` in which ``dupe`` belongs."""
        try:
            return self.__group_of_duplicate[dupe]
        except (TypeError, KeyError):
            return None

    is_markable = _is_markable

    def load_from_xml(self, infile, get_file, j=nulljob):
        """Load results from ``infile``.

        :param infile: a file or path pointing to an XML file created with :meth:`save_to_xml`.
        :param get_file: a function f(path) returning a :class:`~core.fs.File` wrapping the path.
        :param j: A :ref:`job progress instance <jobs>`.
        """
        document = _parse_saved_results(infile)
        groups = []
        pending_file_updates = []
        for saved_group in j.iter_with_progress(document.groups, every=100):
            resolved = []
            for saved_file in saved_group.files:
                file = get_file(saved_file.path)
                resolved.append(file)
                if file is not None:
                    pending_file_updates.append(
                        (
                            file,
                            list(saved_file.words),
                            saved_file.is_ref,
                        )
                    )
            present = [
                (index, file, saved_group.files[index]) for index, file in enumerate(resolved) if file is not None
            ]
            if len(present) < 2:
                j.add_progress()
                continue
            # A resolver may collapse two saved paths to one object (for
            # example an old synthetic report or an aliasing filesystem
            # wrapper).  Such a record cannot form a valid duplicate group;
            # treat it like a missing member instead of constructing self
            # matches or aborting the otherwise review-only report.
            if len({file for _, file, _ in present}) != len(present):
                j.add_progress()
                continue

            if saved_group.exact_evidence is not None:
                group = engine.Group.from_unverified_exact_report(
                    [file for _, file, _ in present],
                    saved_group.exact_evidence,
                )
            elif saved_group.relation in _COMPACT_TRANSITIVE_RELATIONS:
                group = engine.Group.from_unverified_transitive_files(
                    [file for _, file, _ in present],
                    relation=saved_group.relation,
                )
            else:
                present_files = [file for _, file, _ in present]
                if saved_group.matches:
                    present_matches = []
                    for saved_match in saved_group.matches:
                        first_file = resolved[saved_match.first]
                        second_file = resolved[saved_match.second]
                        if first_file is None or second_file is None:
                            continue
                        present_matches.append(
                            engine.Match(
                                first_file,
                                second_file,
                                saved_match.percentage,
                            )
                        )
                    group = engine.Group.from_saved_matches(
                        present_files,
                        present_matches,
                    )
                else:
                    reconstructed_matches = []
                    for first_offset, (_, first_file, first_record) in enumerate(present[:-1]):
                        for _, second_file, second_record in present[first_offset + 1 :]:
                            percentage = engine.compare(
                                list(first_record.words),
                                list(second_record.words),
                            )
                            reconstructed_matches.append(
                                engine.Match(
                                    first_file,
                                    second_file,
                                    percentage,
                                )
                            )
                    group = engine.Group.from_saved_matches(
                        present_files,
                        reconstructed_matches,
                    )
            if len(group) >= 2:
                references = {id(file): saved_file.is_ref for _, file, saved_file in present}
                ordered_members = []
                seen_members = set()
                for _, file, _ in present:
                    identity = id(file)
                    if identity in seen_members or file not in group.unordered:
                        continue
                    seen_members.add(identity)
                    ordered_members.append(file)
                group.ordered = [file for file in ordered_members if references[id(file)]] + [
                    file for file in ordered_members if not references[id(file)]
                ]
                group.layout_revision += 1
                groups.append(group)
            j.add_progress()

        previous_state = self.__dict__.copy()
        attribute_snapshots = []
        try:
            for file, words, is_ref in pending_file_updates:
                attribute_snapshots.append(
                    (
                        file,
                        getattr(file, "words", _MISSING_ATTRIBUTE),
                        getattr(file, "is_ref", _MISSING_ATTRIBUTE),
                    )
                )
                file.words = words
                file.is_ref = is_ref
            self.groups = groups
            self.apply_filter(None)
            # A persisted report is historical data, never a live deletion
            # proof. Saved marks are intentionally not restored.
            self.loaded_report = True
            self.loaded_schema_version = document.schema_version
            self.loaded_report_metadata = dict(document.metadata)
            self.is_modified = False
        except BaseException:
            for file, old_words, old_is_ref in reversed(attribute_snapshots):
                if old_words is _MISSING_ATTRIBUTE:
                    try:
                        del file.words
                    except AttributeError:
                        pass
                else:
                    file.words = old_words
                if old_is_ref is _MISSING_ATTRIBUTE:
                    try:
                        del file.is_ref
                    except AttributeError:
                        pass
                else:
                    file.is_ref = old_is_ref
            self.__dict__.clear()
            self.__dict__.update(previous_state)
            raise

    def make_ref(self, dupe):
        """Make ``dupe`` take the :attr:`~core.engine.Group.ref` position of its group."""
        g = self.get_group_of_duplicate(dupe)
        r = g.ref
        if not g.switch_ref(dupe):
            return False
        self._remove_mark_flag(dupe)
        if not r.is_ref:
            self.__total_count += 1
            self.__total_size += r.size
        if not dupe.is_ref:
            self.__total_count -= 1
            self.__total_size -= dupe.size
        self.__dupes = None
        self.is_modified = True
        return True

    def perform_on_marked(self, func, remove_from_results):
        """Performs ``func`` on all marked dupes.

        If an ``EnvironmentError`` is raised during the call, the problematic dupe is added to
        self.problems.

        :param bool remove_from_results: If true, dupes which had ``func`` applied and didn't cause
                                         any problem.
        """
        self.problems = []
        to_remove = []
        marked = (dupe for dupe in self.dupes if self.is_marked(dupe))
        for dupe in marked:
            try:
                func(dupe)
                to_remove.append(dupe)
            except (OSError, UnicodeEncodeError) as e:
                self.problems.append((dupe, str(e)))
        if remove_from_results:
            self.remove_duplicates(to_remove)
            self.mark_none()
            for dupe, _ in self.problems:
                self.mark(dupe)

    def remove_duplicates(self, dupes):
        """Remove ``dupes`` from their respective :class:`~core.engine.Group`.

        Also, remove the group from :attr:`groups` if it ends up empty.
        """
        removals_by_group = {}
        for dupe in dupes:
            group = self.get_group_of_duplicate(dupe)
            if group is None or dupe is group.ref or dupe not in group.unordered:
                break
            removals_by_group.setdefault(group, set()).add(dupe)

        if not removals_by_group:
            return

        markable_removals = {
            dupe for removals in removals_by_group.values() for dupe in removals if self._is_markable(dupe)
        }
        dropped_members = set()
        removed_dupes = set()
        empty_groups = set()
        for group, requested_removals in removals_by_group.items():
            members_before = set(group.unordered)
            removed = group.remove_dupes(requested_removals, False)
            if not removed:
                continue
            removed_dupes.update(removed)
            dropped_members.update(removed)
            for dupe in removed:
                self.__group_of_duplicate.pop(dupe, None)
            if group:
                group.discard_matches()
            else:
                empty_groups.add(group)
                dropped_members.update(members_before)
                for member in members_before:
                    self.__group_of_duplicate.pop(member, None)

        if empty_groups:
            self.__groups = [group for group in self.__groups if group not in empty_groups]
        if self.__filtered_dupes is not None:
            self.__filtered_dupes.difference_update(dropped_members)
        if self.__filtered_groups is not None:
            visible_groups = {
                self.__group_of_duplicate[dupe] for dupe in self.__filtered_dupes if dupe in self.__group_of_duplicate
            }
            self.__filtered_groups = [
                group for group in self.__filtered_groups if group not in empty_groups and group in visible_groups
            ]
        for member in dropped_members:
            self._remove_mark_flag(member)
        markable_removals.intersection_update(removed_dupes)
        self.__total_count = max(0, self.__total_count - len(markable_removals))
        self.__total_size = max(
            0,
            self.__total_size - sum(dupe.size for dupe in markable_removals),
        )
        self.__dupes = None
        self.is_modified = bool(self.__groups)

    def save_to_xml(self, outfile):
        """Save results to ``outfile`` in XML.

        Path destinations are replaced atomically after a complete preflight.
        File-like destinations are written directly after that preflight, so
        an I/O failure can leave the caller-owned stream partially written.

        :param outfile: binary file object or path.
        """
        # Save the complete backing collection without mutating the active
        # review filter.  A failed preflight or write must leave the UI state
        # exactly as it was.
        groups = self.__groups
        saved_at_ns = time.time_ns()
        root_attributes = {
            "schema_version": str(RESULTS_SCHEMA_VERSION),
            "saved_at_ns": str(saved_at_ns),
            "destructive_proof": "requires_live_reverification",
        }
        if self.scan_receipt is not None:
            root_attributes.update(
                {
                    "scan_id": self.scan_receipt.scan_id,
                    "scan_status": self.scan_receipt.status.value,
                    "scan_complete": "y" if self.scan_receipt.complete else "n",
                }
            )
        try:
            expected_bytes = _validate_results_save_contract(
                groups,
                root_attributes,
            )
        except MemoryError as error:
            raise ValueError("Insufficient memory to validate Results XML") from error

        def do_write(outfile):
            try:
                write_xml_stream(
                    lambda stream: _write_results_xml(
                        stream,
                        groups,
                        root_attributes,
                        self.is_marked,
                        expected_bytes,
                    ),
                    outfile,
                )
            except MemoryError as error:
                raise ValueError("Insufficient memory to serialize Results XML") from error

        try:
            do_write(outfile)
        except OSError as e:
            # If our OSError is because dest is already a directory, we want to handle that. 21 is
            # the code we get on OS X and Linux (EISDIR), 13 is what we get on Windows (EACCES).
            if e.errno in (EISDIR, EACCES):
                p = str(outfile)
                dirname, basename = op.split(p)
                otherfiles = os.listdir(dirname)
                newname = get_conflicted_name(otherfiles, basename)
                do_write(op.join(dirname, newname))
            else:
                raise
        self.is_modified = False

    def sort_dupes(self, key, asc=True, delta=False):
        """Sort :attr:`dupes` according to ``key``.

        :param str key: key attribute name to sort with.
        :param bool asc: If false, sorting is reversed.
        :param bool delta: If true, sorting occurs using :ref:`delta values <deltavalues>`.
        """
        if not self.__dupes:
            self.__get_dupe_list()
        self.__dupes.sort(
            key=lambda d: self.app._get_dupe_sort_key(d, lambda: self.get_group_of_duplicate(d), key, delta),
            reverse=not asc,
        )
        self.__dupes_sort_descriptor = (key, asc, delta)

    def sort_groups(self, key, asc=True):
        """Sort :attr:`groups` according to ``key``.

        The :attr:`~core.engine.Group.ref` of each group is used to extract values for sorting.

        :param str key: key attribute name to sort with.
        :param bool asc: If false, sorting is reversed.
        """
        self.groups.sort(key=lambda g: self.app._get_group_sort_key(g, key), reverse=not asc)
        self.__groups_sort_descriptor = (key, asc)

    # ---Properties
    dupes = property(__get_dupe_list)
    groups = property(__get_groups, __set_groups)
    stat_line = property(__get_stat_line)
