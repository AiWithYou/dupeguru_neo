# Created By: Virgil Dupras
# Created On: 2006/05/02
# Copyright 2015 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

from xml.etree import ElementTree as ET

from core.safe_xml import parse_xml, write_xml

IGNORE_XML_MAX_BYTES = 32 * 1024 * 1024
IGNORE_XML_MAX_FILE_NODES = 200_000
IGNORE_XML_MAX_EDGES = 100_000
IGNORE_XML_MAX_PATH_CHARS = 32_767
IGNORE_XML_MAX_TOTAL_CHARS = 24 * 1024 * 1024
# Conservative upper bounds for ElementTree's serialized tag overhead.  Path
# bytes are accounted after XML attribute escaping.
_IGNORE_XML_BASE_BYTES = 128
_IGNORE_XML_OUTER_FILE_BYTES = 32
_IGNORE_XML_CHILD_FILE_BYTES = 24


class IgnoreListLoadError(ValueError):
    """An ignore-list document failed bounded schema validation."""


class IgnoreListLimitError(ValueError):
    """An in-memory mutation would create a document the loader must reject."""


def _escaped_attribute_bytes(value):
    # Keep this in sync with xml.etree.ElementTree._escape_attrib().  In
    # particular, XML normalizes literal attribute whitespace, so
    # ElementTree publishes CR/LF/TAB as numeric character references.
    escaped = (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\r", "&#13;")
        .replace("\n", "&#10;")
        .replace("\t", "&#09;")
    )
    return len(escaped.encode("utf-8"))


class IgnoreList:
    """An ignore list implementation that is iterable, filterable and exportable to XML.

    Call Ignore to add an ignore list entry, and AreIgnore to check if 2 items are in the list.
    When iterated, 2 sized tuples will be returned, the tuples containing 2 items ignored together.
    """

    # ---Override
    def __init__(self):
        self.revision = 0
        self.clear()
        self.revision = 0

    def __iter__(self):
        for first, seconds in self._ignored.items():
            for second in seconds:
                yield (first, second)

    def __len__(self):
        return self._count

    # ---Public
    def are_ignored(self, first, second):
        return second in self._ignored.get(first, ()) or second in self._reverse.get(first, ())

    def ignored_neighbors(self, path):
        """Return every path joined to *path* by one stored ignore edge."""

        direct = self._ignored.get(path, ())
        reverse = self._reverse.get(path, ())
        if not direct:
            return frozenset(reverse)
        if not reverse:
            return frozenset(direct)
        return frozenset(direct | reverse)

    def clear(self):
        changed = bool(getattr(self, "_count", 0))
        self._ignored = {}
        self._reverse = {}
        self._count = 0
        self._file_nodes = 0
        self._total_chars = len("ignore_list")
        self._projected_bytes = _IGNORE_XML_BASE_BYTES
        if changed:
            self.revision += 1

    def _recalculate_limits(self):
        outer_count = len(self._ignored)
        self._file_nodes = outer_count + self._count
        self._total_chars = (
            len("ignore_list")
            + (len("file") + len("path")) * self._file_nodes
            + sum(len(first) for first in self._ignored)
            + sum(len(second) for seconds in self._ignored.values() for second in seconds)
        )
        self._projected_bytes = (
            _IGNORE_XML_BASE_BYTES
            + outer_count * _IGNORE_XML_OUTER_FILE_BYTES
            + self._count * _IGNORE_XML_CHILD_FILE_BYTES
            + sum(_escaped_attribute_bytes(first) for first in self._ignored)
            + sum(_escaped_attribute_bytes(second) for seconds in self._ignored.values() for second in seconds)
        )

    @staticmethod
    def _validate_runtime_path(value, description):
        try:
            return IgnoreList._validate_path(value, description)
        except (IgnoreListLoadError, TypeError) as error:
            raise IgnoreListLimitError(str(error)) from error

    @staticmethod
    def _check_projected_limits(*, edges, file_nodes, total_chars, projected_bytes):
        if edges > IGNORE_XML_MAX_EDGES:
            raise IgnoreListLimitError(
                "The ignore list is full (maximum {} relationships).".format(IGNORE_XML_MAX_EDGES)
            )
        if file_nodes > IGNORE_XML_MAX_FILE_NODES:
            raise IgnoreListLimitError(
                "The ignore list would exceed its {} XML-node limit.".format(IGNORE_XML_MAX_FILE_NODES)
            )
        if total_chars > IGNORE_XML_MAX_TOTAL_CHARS:
            raise IgnoreListLimitError(
                "The ignore list would exceed its {} character limit.".format(IGNORE_XML_MAX_TOTAL_CHARS)
            )
        if projected_bytes > IGNORE_XML_MAX_BYTES:
            raise IgnoreListLimitError("The ignore list would exceed its {} byte limit.".format(IGNORE_XML_MAX_BYTES))

    def filter(self, func):
        """Applies a filter on all ignored items, and remove all matches where func(first,second)
        doesn't return True.
        """
        filtered = IgnoreList()
        for first, second in self:
            if func(first, second):
                filtered.ignore(first, second)
        changed = filtered._count != self._count
        self._ignored = filtered._ignored
        self._reverse = filtered._reverse
        self._count = filtered._count
        self._file_nodes = filtered._file_nodes
        self._total_chars = filtered._total_chars
        self._projected_bytes = filtered._projected_bytes
        if changed:
            self.revision += 1

    def ignore(self, first, second):
        first = self._validate_runtime_path(first, "first ignore item")
        second = self._validate_runtime_path(second, "second ignore item")
        if first == second:
            raise IgnoreListLimitError("An ignore relationship requires two different paths.")
        if self.are_ignored(first, second):
            return False
        new_outer = False
        if first in self._ignored:
            stored_first, stored_second = first, second
        elif second in self._ignored:
            stored_first, stored_second = second, first
        else:
            stored_first, stored_second = first, second
            new_outer = True
        added_nodes = 1 + int(new_outer)
        added_chars = (
            (len("file") + len("path")) * added_nodes + len(stored_second) + (len(stored_first) if new_outer else 0)
        )
        added_bytes = (
            _IGNORE_XML_CHILD_FILE_BYTES
            + _escaped_attribute_bytes(stored_second)
            + (_IGNORE_XML_OUTER_FILE_BYTES + _escaped_attribute_bytes(stored_first) if new_outer else 0)
        )
        self._check_projected_limits(
            edges=self._count + 1,
            file_nodes=self._file_nodes + added_nodes,
            total_chars=self._total_chars + added_chars,
            projected_bytes=self._projected_bytes + added_bytes,
        )
        if new_outer:
            self._ignored[stored_first] = {stored_second}
        else:
            self._ignored[stored_first].add(stored_second)
        self._reverse.setdefault(stored_second, set()).add(stored_first)
        self._count += 1
        self._file_nodes += added_nodes
        self._total_chars += added_chars
        self._projected_bytes += added_bytes
        self.revision += 1
        return True

    def ignore_many(self, pairs):
        """Transactionally add a bounded stream of relationships.

        At most one loader-sized batch is inspected.  This makes a "select
        everything" action on a very large group fail before mutating state
        instead of generating an unbounded quadratic edge set.
        """

        candidate = IgnoreList()
        candidate._ignored = {first: set(seconds) for first, seconds in self._ignored.items()}
        candidate._reverse = {second: set(firsts) for second, firsts in self._reverse.items()}
        candidate._count = self._count
        candidate._file_nodes = self._file_nodes
        candidate._total_chars = self._total_chars
        candidate._projected_bytes = self._projected_bytes
        inspected = 0
        for first, second in pairs:
            inspected += 1
            if inspected > IGNORE_XML_MAX_EDGES + 1:
                raise IgnoreListLimitError("The requested ignore operation contains too many relationships.")
            candidate.ignore(first, second)
        changed = candidate._count != self._count
        self._ignored = candidate._ignored
        self._reverse = candidate._reverse
        self._count = candidate._count
        self._file_nodes = candidate._file_nodes
        self._total_chars = candidate._total_chars
        self._projected_bytes = candidate._projected_bytes
        if changed:
            self.revision += 1

    def remove(self, first, second):
        def inner(first, second):
            try:
                matches = self._ignored[first]
                if second in matches:
                    matches.discard(second)
                    if not matches:
                        del self._ignored[first]
                    reverse = self._reverse[second]
                    reverse.discard(first)
                    if not reverse:
                        del self._reverse[second]
                    self._count -= 1
                    return True
                else:
                    return False
            except KeyError:
                return False

        if not inner(first, second) and not inner(second, first):
            raise ValueError()
        self._recalculate_limits()
        self.revision += 1

    @staticmethod
    def _require_whitespace(value, description):
        if value and value.strip():
            raise IgnoreListLoadError(f"{description} must not contain text")

    @staticmethod
    def _validate_path(value, description):
        if not value:
            raise IgnoreListLoadError(f"{description} has an empty path")
        if len(value) > IGNORE_XML_MAX_PATH_CHARS:
            raise IgnoreListLoadError(f"{description} path is too long")
        if "\0" in value:
            raise IgnoreListLoadError(f"{description} path contains a NUL byte")
        # XML 1.0 cannot round-trip the remaining C0 controls or surrogate
        # code points.  ElementTree can emit some of them, but the resulting
        # document is rejected by its own parser.  Refuse the relationship
        # transactionally instead of creating an ignore list that cannot be
        # loaded on the next launch.
        if any(
            not (
                character in "\t\n\r"
                or "\x20" <= character <= "\ud7ff"
                or "\ue000" <= character <= "\ufffd"
                or "\U00010000" <= character <= "\U0010ffff"
            )
            for character in value
        ):
            raise IgnoreListLoadError(f"{description} path contains a character XML 1.0 cannot represent")
        return value

    def _parse_loaded_state(self, infile):
        root = parse_xml(
            infile,
            max_bytes=IGNORE_XML_MAX_BYTES,
            max_elements=IGNORE_XML_MAX_FILE_NODES + 1,
            max_depth=3,
            max_attributes_per_element=1,
            max_attributes=IGNORE_XML_MAX_FILE_NODES,
            max_name_chars=32,
            max_attribute_chars=IGNORE_XML_MAX_PATH_CHARS,
            max_text_chars=4096,
            max_tail_chars=4096,
            max_total_chars=IGNORE_XML_MAX_TOTAL_CHARS,
        )
        if root.tag != "ignore_list":
            raise IgnoreListLoadError("ignore-list XML has the wrong root element")
        if root.attrib:
            raise IgnoreListLoadError("ignore_list must not have attributes")
        self._require_whitespace(root.text, "ignore_list")
        self._require_whitespace(root.tail, "ignore_list")

        ignored = {}
        reverse = {}
        outer_paths = set()
        seen_edges = set()
        file_nodes = 0
        edge_count = 0
        for parent_number, parent in enumerate(root, 1):
            file_nodes += 1
            if parent.tag != "file" or set(parent.attrib) != {"path"}:
                raise IgnoreListLoadError(f"ignore parent {parent_number} has an invalid schema")
            self._require_whitespace(parent.text, f"ignore parent {parent_number}")
            self._require_whitespace(parent.tail, f"ignore parent {parent_number}")
            first = self._validate_path(parent.attrib["path"], f"ignore parent {parent_number}")
            if first in outer_paths:
                raise IgnoreListLoadError(f"ignore parent {parent_number} duplicates an earlier parent")
            outer_paths.add(first)
            if not len(parent):
                raise IgnoreListLoadError(f"ignore parent {parent_number} has no child paths")

            for child_number, child in enumerate(parent, 1):
                file_nodes += 1
                edge_count += 1
                if file_nodes > IGNORE_XML_MAX_FILE_NODES or edge_count > IGNORE_XML_MAX_EDGES:
                    raise IgnoreListLoadError("ignore-list item count exceeds the supported limit")
                if child.tag != "file" or set(child.attrib) != {"path"} or len(child):
                    raise IgnoreListLoadError(f"ignore child {parent_number}.{child_number} has an invalid schema")
                self._require_whitespace(
                    child.text,
                    f"ignore child {parent_number}.{child_number}",
                )
                self._require_whitespace(
                    child.tail,
                    f"ignore child {parent_number}.{child_number}",
                )
                second = self._validate_path(
                    child.attrib["path"],
                    f"ignore child {parent_number}.{child_number}",
                )
                if first == second:
                    raise IgnoreListLoadError(f"ignore child {parent_number}.{child_number} references the same path")
                edge = (first, second) if first < second else (second, first)
                if edge in seen_edges:
                    raise IgnoreListLoadError(f"ignore child {parent_number}.{child_number} duplicates an earlier pair")
                seen_edges.add(edge)
                ignored.setdefault(first, set()).add(second)
                reverse.setdefault(second, set()).add(first)

        return ignored, reverse, edge_count

    def load_from_xml(self, infile):
        """Transactionally load an ignore list from bounded, strict XML.

        infile can be a file object or a filename.
        """
        try:
            ignored, reverse, count = self._parse_loaded_state(infile)
        except Exception as error:
            failure = (
                error
                if isinstance(error, IgnoreListLoadError)
                else IgnoreListLoadError(f"could not load ignore-list XML: {type(error).__name__}")
            )
            return failure

        self._ignored = ignored
        self._reverse = reverse
        self._count = count
        self._recalculate_limits()
        self.revision += 1
        return None

    def save_to_xml(self, outfile):
        """Create a XML file that can be used by load_from_xml.

        outfile can be a file object or a filename.
        """
        self._check_projected_limits(
            edges=self._count,
            file_nodes=self._file_nodes,
            total_chars=self._total_chars,
            projected_bytes=self._projected_bytes,
        )
        root = ET.Element("ignore_list")
        for filename, subfiles in self._ignored.items():
            file_node = ET.SubElement(root, "file")
            file_node.set("path", filename)
            for subfilename in subfiles:
                subfile_node = ET.SubElement(file_node, "file")
                subfile_node.set("path", subfilename)
        write_xml(ET.ElementTree(root), outfile)
