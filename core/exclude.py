# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

from core.markable import Markable
from xml.etree import ElementTree as ET

# TODO: perhaps use regex module for better Unicode support? https://pypi.org/project/regex/
# also https://pypi.org/project/re2/
# TODO update the Result list with newly added regexes if possible
import io
import re
from os import sep
import logging
import functools
from hscommon.plat import ISWINDOWS
import time
from core.safe_xml import parse_xml, write_xml

EXCLUDE_XML_MAX_BYTES = 4 * 1024 * 1024
EXCLUDE_XML_MAX_ITEMS = 4096
EXCLUDE_XML_MAX_REGEX_CHARS = 4096
EXCLUDE_XML_MAX_TOTAL_REGEX_CHARS = 256 * 1024
EXCLUDE_XML_MAX_TOTAL_CHARS = 2 * 1024 * 1024

default_regexes = [
    r"^thumbs\.db$",  # Obsolete after WindowsXP
    r"^desktop\.ini$",  # Windows metadata
    r"^\.DS_Store$",  # MacOS metadata
    r"^\.Trash\-.*",  # Linux trash directories
    r"^\$Recycle\.Bin$",  # Windows
    r"^\..*",  # Hidden files on Unix-like
]
# These are too broad
forbidden_regexes = [r".*", r"\/.*", r".*\/.*", r".*\\\\.*", r".*\..*"]


def timer(func):
    @functools.wraps(func)
    def wrapper_timer(*args):
        start = time.perf_counter_ns()
        value = func(*args)
        end = time.perf_counter_ns()
        print(f"DEBUG: func {func.__name__!r} took {end - start} ns.")
        return value

    return wrapper_timer


def memoize(func):
    func.cache = dict()

    @functools.wraps(func)
    def _memoize(*args):
        if args not in func.cache:
            func.cache[args] = func(*args)
        return func.cache[args]

    return _memoize


class AlreadyThereException(Exception):
    """Expression already in the list"""

    def __init__(self, arg="Expression is already in excluded list."):
        super().__init__(arg)


class ExcludeListLoadError(ValueError):
    """An exclusion-list document failed bounded schema validation."""


class ExcludeListLimitError(ValueError):
    """A runtime mutation would create an exclusion list the loader rejects."""


class ExcludeList(Markable):
    """A list of lists holding regular expression strings and the compiled re.Pattern"""

    # Used to filter out directories and files that we would rather avoid scanning.
    # The list() class allows us to preserve item order without too much hassle.
    # The downside is we have to compare strings every time we look for an item in the list
    # since we use regex strings as keys.
    # If _use_union is True, the compiled regexes will be combined into one single
    # Pattern instead of separate Patterns which may or may not give better
    # performance compared to looping through each Pattern individually.

    # ---Override
    def __init__(self, union_regex=True):
        Markable.__init__(self)
        self.revision = 0
        self._use_union = union_regex
        # list([str regex, bool iscompilable, re.error exception, Pattern compiled], ...)
        self._excluded = []
        self._excluded_compiled = set()
        self._dirty = True

    def __iter__(self):
        """Iterate in order."""
        for item in self._excluded:
            regex = item[0]
            yield self.is_marked(regex), regex

    def __contains__(self, item):
        return self.has_entry(item)

    def __len__(self):
        """Returns the total number of regexes regardless of mark status."""
        return len(self._excluded)

    def __getitem__(self, key):
        """Returns the list item corresponding to key."""
        for item in self._excluded:
            if item[0] == key:
                return item
        raise KeyError(f"Key {key} is not in exclusion list.")

    def __setitem__(self, key, value):
        # TODO if necessary
        pass

    def __delitem__(self, key):
        # TODO if necessary
        pass

    def get_compiled(self, key):
        """Returns the (precompiled) Pattern for key"""
        return self.__getitem__(key)[3]

    def is_markable(self, regex):
        return self._is_markable(regex)

    def _is_markable(self, regex):
        """Return the cached result of "compilable" property"""
        for item in self._excluded:
            if item[0] == regex:
                return item[1]
        return False  # should not be necessary, the regex SHOULD be in there

    def _did_mark(self, regex):
        self._add_compiled(regex)
        self.revision += 1

    def _did_unmark(self, regex):
        self._remove_compiled(regex)
        self.revision += 1

    def _add_compiled(self, regex):
        self._dirty = True
        if self._use_union:
            return
        for item in self._excluded:
            # FIXME probably faster to just rebuild the set from the compiled instead of comparing strings
            if item[0] == regex:
                # no need to test if already present since it's a set()
                self._excluded_compiled.add(item[3])
                break

    def _remove_compiled(self, regex):
        self._dirty = True
        if self._use_union:
            return
        for item in self._excluded_compiled:
            if regex in item.pattern:
                self._excluded_compiled.remove(item)
                break

    # @timer
    @memoize
    def _do_compile(self, expr):
        return re.compile(expr)

    # @timer
    # @memoize  # probably not worth memoizing this one if we memoize the above
    def compile_re(self, regex):
        compiled = None
        try:
            compiled = self._do_compile(regex)
        except Exception as e:
            return False, e, compiled
        return True, None, compiled

    def error(self, regex):
        """Return the compilation error Exception for regex.
        It should have a "msg" attr."""
        for item in self._excluded:
            if item[0] == regex:
                return item[2]

    def build_compiled_caches(self, union=False):
        if not union:
            self._cached_compiled_files = [x for x in self._excluded_compiled if not has_sep(x.pattern)]
            self._cached_compiled_paths = [x for x in self._excluded_compiled if has_sep(x.pattern)]
            self._dirty = False
            return

        marked_count = [x for marked, x in self if marked]
        # If there is no item, the compiled Pattern will be '' and match everything!
        if not marked_count:
            self._cached_compiled_union_all = []
            self._cached_compiled_union_files = []
            self._cached_compiled_union_paths = []
        else:
            # HACK returned as a tuple to get a free iterator and keep interface
            # the same regardless of whether the client asked for union or not
            self._cached_compiled_union_all = (re.compile("|".join(marked_count)),)
            files_marked = [x for x in marked_count if not has_sep(x)]
            if not files_marked:
                self._cached_compiled_union_files = tuple()
            else:
                self._cached_compiled_union_files = (re.compile("|".join(files_marked)),)
            paths_marked = [x for x in marked_count if has_sep(x)]
            if not paths_marked:
                self._cached_compiled_union_paths = tuple()
            else:
                self._cached_compiled_union_paths = (re.compile("|".join(paths_marked)),)
        self._dirty = False

    @property
    def compiled(self):
        """Should be used by other classes to retrieve the up-to-date list of patterns."""
        if self._use_union:
            if self._dirty:
                self.build_compiled_caches(self._use_union)
            return self._cached_compiled_union_all
        return self._excluded_compiled

    @property
    def compiled_files(self):
        """When matching against filenames only, we probably won't be seeing any
        directory separator, so we filter out regexes with os.sep in them.
        The interface should be expected to be a generator, even if it returns only
        one item (one Pattern in the union case)."""
        if self._dirty:
            self.build_compiled_caches(self._use_union)
        return self._cached_compiled_union_files if self._use_union else self._cached_compiled_files

    @property
    def compiled_paths(self):
        """Returns patterns with only separators in them, for more precise filtering."""
        if self._dirty:
            self.build_compiled_caches(self._use_union)
        return self._cached_compiled_union_paths if self._use_union else self._cached_compiled_paths

    # ---Public
    def add(self, regex, forced=False):
        """This interface should throw exceptions if there is an error during
        regex compilation"""
        if self.has_entry(regex):
            # This exception should never be ignored
            raise AlreadyThereException()
        if regex in forbidden_regexes:
            raise ValueError("Forbidden (dangerous) expression.")

        iscompilable, exception, _compiled = self.compile_re(regex)
        if not iscompilable and not forced:
            # This exception can be ignored, but taken into account
            # to avoid adding to compiled set
            raise exception
        candidate = [(False, regex), *list(self)]
        self._validate_runtime_entries(candidate)
        self._replace_runtime_entries(candidate)

    def _do_add(self, regex, iscompilable, exception, compiled):
        # We need to insert at the top
        self._excluded.insert(0, [regex, iscompilable, exception, compiled])

    @property
    def marked_count(self):
        """Returns the number of marked regexes only."""
        return len([x for marked, x in self if marked])

    def has_entry(self, regex):
        for item in self._excluded:
            if regex == item[0]:
                return True
        return False

    def is_excluded(self, dirname, filename):
        """Return True if the file or the absolute path to file is supposed to be
        filtered out, False otherwise."""
        matched = False
        for expr in self.compiled_files:
            if expr.fullmatch(filename):
                matched = True
                break
        if not matched:
            for expr in self.compiled_paths:
                if expr.fullmatch(dirname + sep + filename):
                    matched = True
                    break
        return matched

    def remove(self, regex):
        candidate = [(marked, item_regex) for marked, item_regex in self if item_regex != regex]
        if len(candidate) == len(self._excluded):
            return
        self._replace_runtime_entries(candidate)

    def rename(self, regex, newregex):
        if regex == newregex:
            return
        if not self.has_entry(regex):
            return
        candidate = [(marked, newregex if item_regex == regex else item_regex) for marked, item_regex in self]
        self._validate_runtime_entries(candidate)
        self._replace_runtime_entries(candidate)

    def mark(self, regex):
        """Mark *regex* only if the complete persisted union remains loadable."""

        if self.is_marked(regex) or not self.is_markable(regex):
            return False
        candidate = [(marked or item_regex == regex, item_regex) for marked, item_regex in self]
        self._validate_runtime_entries(candidate)
        self._replace_runtime_entries(candidate)
        return True

    def unmark(self, regex):
        if not self.is_marked(regex):
            return False
        candidate = [(False if item_regex == regex else marked, item_regex) for marked, item_regex in self]
        self._replace_runtime_entries(candidate)
        return True

    def mark_toggle(self, regex):
        if not self.is_markable(regex):
            return False
        candidate = [(not marked if item_regex == regex else marked, item_regex) for marked, item_regex in self]
        self._validate_runtime_entries(candidate)
        self._replace_runtime_entries(candidate)
        return True

    def mark_multiple(self, regexes):
        requested = set(regexes)
        markable = self._markable_regexes()
        candidate = [
            (marked or (item_regex in requested and item_regex in markable), item_regex) for marked, item_regex in self
        ]
        if candidate == list(self):
            return
        self._validate_runtime_entries(candidate)
        self._replace_runtime_entries(candidate)

    def unmark_multiple(self, regexes):
        requested = set(regexes)
        candidate = [(marked and item_regex not in requested, item_regex) for marked, item_regex in self]
        if candidate == list(self):
            return
        self._replace_runtime_entries(candidate)

    def mark_toggle_multiple(self, regexes):
        requested = set()
        for regex in regexes:
            if regex in requested:
                requested.remove(regex)
            else:
                requested.add(regex)
        markable = self._markable_regexes()
        candidate = [
            (
                not marked if item_regex in requested and item_regex in markable else marked,
                item_regex,
            )
            for marked, item_regex in self
        ]
        if candidate == list(self):
            return
        self._validate_runtime_entries(candidate)
        self._replace_runtime_entries(candidate)

    def mark_all(self):
        markable = self._markable_regexes()
        candidate = [(regex in markable, regex) for _marked, regex in self]
        if candidate == list(self):
            return
        self._validate_runtime_entries(candidate)
        self._replace_runtime_entries(candidate)

    def mark_invert(self):
        markable = self._markable_regexes()
        candidate = [(not marked and regex in markable, regex) for marked, regex in self]
        if candidate == list(self):
            return
        self._validate_runtime_entries(candidate)
        self._replace_runtime_entries(candidate)

    def mark_none(self):
        candidate = [(False, regex) for _marked, regex in self]
        if candidate == list(self):
            return
        self._replace_runtime_entries(candidate)

    # def change_index(self, regex, new_index):
    # """Internal list must be a list, not dict."""
    #     item = self._excluded.pop(regex)
    #     self._excluded.insert(new_index, item)

    def restore_defaults(self):
        current = list(self)
        present = {regex for _marked, regex in current}
        candidate = [(regex in default_regexes, regex) for _marked, regex in current]
        for default_regex in default_regexes:
            if default_regex not in present:
                candidate.insert(0, (True, default_regex))
        if candidate == current:
            return
        self._validate_runtime_entries(candidate)
        self._replace_runtime_entries(candidate)

    @staticmethod
    def _require_whitespace(value, description):
        if value and value.strip():
            raise ExcludeListLoadError(f"{description} must not contain text")

    @staticmethod
    def _compile_loaded_regex(regex, item_number):
        if not regex:
            raise ExcludeListLoadError(f"exclude item {item_number} has an empty regex")
        if len(regex) > EXCLUDE_XML_MAX_REGEX_CHARS:
            raise ExcludeListLoadError(f"exclude item {item_number} regex is too long")
        if regex in forbidden_regexes:
            raise ExcludeListLoadError(f"exclude item {item_number} contains a forbidden regex")
        try:
            return re.compile(regex)
        except re.error as error:
            raise ExcludeListLoadError(f"exclude item {item_number} contains an invalid regex") from error

    def _replace_loaded_entries(self, records):
        """Install validated ``(regex, marked, compiled)`` records in O(n)."""

        # XML is saved in reverse display order because entries are normally
        # inserted at index zero. Reversing once restores the original order
        # without repeatedly inserting or updating dictionary indices.
        ordered = list(reversed(records))
        marked = {regex for regex, is_marked, _compiled in ordered if is_marked}
        if isinstance(self._excluded, dict):
            replacement = {
                regex: {
                    "index": index,
                    "compilable": True,
                    "error": None,
                    "compiled": compiled,
                }
                for index, (regex, _is_marked, compiled) in enumerate(ordered)
            }
        else:
            replacement = [[regex, True, None, compiled] for regex, _is_marked, compiled in ordered]
        compiled = set() if self._use_union else {compiled for regex, is_marked, compiled in ordered if is_marked}

        self._replace_marked_state(marked)
        self._excluded = replacement
        self._excluded_compiled = compiled
        self._dirty = True
        self.revision += 1

    def _replace_runtime_entries(self, entries):
        """Install already validated display-order entries transactionally."""

        records = [(regex, marked, re.compile(regex)) for marked, regex in reversed(entries)]
        self._replace_loaded_entries(records)

    def _ordered_regexes(self):
        return [item[0] for item in self._excluded]

    def _markable_regexes(self):
        if isinstance(self._excluded, dict):
            return {regex for regex, value in self._excluded.items() if value["compilable"]}
        return {item[0] for item in self._excluded if item[1]}

    def _tree_for_entries(self, entries):
        root = ET.Element("exclude_list")
        # Reverse display order so loading restores the same top-to-bottom order.
        for marked, regex in reversed(entries):
            exclude_node = ET.SubElement(root, "exclude")
            exclude_node.set("regex", regex)
            exclude_node.set("marked", "y" if marked else "n")
        return ET.ElementTree(root)

    def _validate_runtime_entries(self, entries):
        """Apply the exact bounded loader contract before a runtime mutation."""

        entries = list(entries)
        try:
            tree = self._tree_for_entries(entries)
            payload = ET.tostring(
                tree.getroot(),
                encoding="utf-8",
                xml_declaration=True,
            )
            self._parse_loaded_entries(io.BytesIO(payload))
        except Exception as error:
            failure = (
                error
                if isinstance(error, ExcludeListLoadError)
                else ExcludeListLoadError("could not validate exclusion-list XML: {}".format(type(error).__name__))
            )
            raise ExcludeListLimitError(str(failure)) from error
        return tree

    def _parse_loaded_entries(self, infile):
        root = parse_xml(
            infile,
            max_bytes=EXCLUDE_XML_MAX_BYTES,
            max_elements=EXCLUDE_XML_MAX_ITEMS + 1,
            max_depth=2,
            max_attributes_per_element=2,
            max_attributes=EXCLUDE_XML_MAX_ITEMS * 2,
            max_name_chars=32,
            max_attribute_chars=EXCLUDE_XML_MAX_REGEX_CHARS,
            max_text_chars=4096,
            max_tail_chars=4096,
            max_total_chars=EXCLUDE_XML_MAX_TOTAL_CHARS,
        )
        if root.tag != "exclude_list":
            raise ExcludeListLoadError("exclusion-list XML has the wrong root element")
        if root.attrib:
            raise ExcludeListLoadError("exclude_list must not have attributes")
        self._require_whitespace(root.text, "exclude_list")
        self._require_whitespace(root.tail, "exclude_list")

        records = []
        seen = set()
        total_regex_chars = 0
        for item_number, element in enumerate(root, 1):
            if item_number > EXCLUDE_XML_MAX_ITEMS:
                raise ExcludeListLoadError("exclusion-list item count exceeds the supported limit")
            if element.tag != "exclude":
                raise ExcludeListLoadError(f"exclude item {item_number} has an unknown element")
            if set(element.attrib) != {"regex", "marked"}:
                raise ExcludeListLoadError(f"exclude item {item_number} has invalid attributes")
            if len(element):
                raise ExcludeListLoadError(f"exclude item {item_number} must not have child elements")
            self._require_whitespace(element.text, f"exclude item {item_number}")
            self._require_whitespace(element.tail, f"exclude item {item_number}")

            regex = element.attrib["regex"]
            marked_value = element.attrib["marked"]
            if marked_value not in {"y", "n"}:
                raise ExcludeListLoadError(f"exclude item {item_number} has an invalid marked value")
            if regex in seen:
                raise ExcludeListLoadError(f"exclude item {item_number} duplicates an earlier regex")
            seen.add(regex)
            total_regex_chars += len(regex)
            if total_regex_chars > EXCLUDE_XML_MAX_TOTAL_REGEX_CHARS:
                raise ExcludeListLoadError("exclusion-list regex content exceeds the supported total limit")
            records.append(
                (
                    regex,
                    marked_value == "y",
                    self._compile_loaded_regex(regex, item_number),
                )
            )
        if self._use_union:
            marked = [regex for regex, is_marked, _compiled in records if is_marked]
            try:
                if marked:
                    re.compile("|".join(marked))
                marked_files = [regex for regex in marked if not has_sep(regex)]
                if marked_files:
                    re.compile("|".join(marked_files))
                marked_paths = [regex for regex in marked if has_sep(regex)]
                if marked_paths:
                    re.compile("|".join(marked_paths))
            except re.error as error:
                raise ExcludeListLoadError("marked regexes cannot be safely combined") from error
        return records

    def load_from_xml(self, infile):
        """Transactionally load an exclusion list from bounded, strict XML.

        infile can be a file object or a filename.
        """
        try:
            records = self._parse_loaded_entries(infile)
        except Exception as error:
            failure = (
                error
                if isinstance(error, ExcludeListLoadError)
                else ExcludeListLoadError(f"could not load exclusion-list XML: {type(error).__name__}")
            )
            logging.warning("Error while loading exclusion-list XML: %s", failure)
            # A missing first-run preference file historically initializes the
            # default exclusions. Other failures preserve the current state.
            if isinstance(error, FileNotFoundError) and not self._excluded:
                defaults = [
                    (regex, True, self._compile_loaded_regex(regex, index))
                    for index, regex in enumerate(default_regexes, 1)
                ]
                self._replace_loaded_entries(defaults)
            return failure

        self._replace_loaded_entries(records)
        return None

    def save_to_xml(self, outfile):
        """Create a XML file that can be used by load_from_xml.
        outfile can be a file object or a filename."""
        entries = [(self.is_marked(regex), regex) for regex in self._ordered_regexes()]
        tree = self._validate_runtime_entries(entries)
        write_xml(tree, outfile)


class ExcludeDict(ExcludeList):
    """Exclusion list holding a set of regular expressions as keys, the compiled
    Pattern, compilation error and compilable boolean as values."""

    # Implemntation around a dictionary instead of a list, which implies
    # to keep the index of each string-key as its sub-element and keep it updated
    # whenever insert/remove is done.

    def __init__(self, union_regex=False):
        Markable.__init__(self)
        self.revision = 0
        self._use_union = union_regex
        # { "regex string":
        #   {
        #       "index": int,
        #       "compilable": bool,
        #       "error": str,
        #       "compiled": Pattern or None
        #   }
        # }
        self._excluded = {}
        self._excluded_compiled = set()
        self._dirty = True

    def __iter__(self):
        """Iterate in order."""
        for regex in ordered_keys(self._excluded):
            yield self.is_marked(regex), regex

    def __getitem__(self, key):
        """Returns the dict item correponding to key"""
        return self._excluded.__getitem__(key)

    def get_compiled(self, key):
        """Returns the compiled item for key"""
        return self.__getitem__(key).get("compiled")

    def is_markable(self, regex):
        return self._is_markable(regex)

    def _is_markable(self, regex):
        """Return the cached result of "compilable" property"""
        exists = self._excluded.get(regex)
        if exists:
            return exists.get("compilable")
        return False

    def _add_compiled(self, regex):
        self._dirty = True
        if self._use_union:
            return
        try:
            self._excluded_compiled.add(self._excluded.get(regex).get("compiled"))
        except Exception as e:
            logging.error(f"Exception while adding regex {regex} to compiled set: {e}")
            return

    def is_compilable(self, regex):
        """Returns the cached "compilable" value"""
        return self._excluded[regex]["compilable"]

    def error(self, regex):
        """Return the compilation error message for regex string"""
        return self._excluded.get(regex).get("error")

    # ---Public
    def _do_add(self, regex, iscompilable, exception, compiled):
        # We always insert at the top, so index should be 0
        # and other indices should be pushed by one
        for value in self._excluded.values():
            value["index"] += 1
        self._excluded[regex] = {"index": 0, "compilable": iscompilable, "error": exception, "compiled": compiled}

    def has_entry(self, regex):
        if regex in self._excluded.keys():
            return True
        return False

    def remove(self, regex):
        super().remove(regex)

    def rename(self, regex, newregex):
        super().rename(regex, newregex)

    def _ordered_regexes(self):
        return list(ordered_keys(self._excluded))

    def save_to_xml(self, outfile):
        """Create a XML file that can be used by load_from_xml.

        outfile can be a file object or a filename.
        """
        super().save_to_xml(outfile)


def ordered_keys(_dict):
    """Returns an iterator over the keys of dictionary sorted by "index" key"""
    if not len(_dict):
        return
    list_of_items = []
    for item in _dict.items():
        list_of_items.append(item)
    list_of_items.sort(key=lambda x: x[1].get("index"))
    for item in list_of_items:
        yield item[0]


if ISWINDOWS:

    def has_sep(regexp):
        return "\\" + sep in regexp

else:

    def has_sep(regexp):
        return sep in regexp
