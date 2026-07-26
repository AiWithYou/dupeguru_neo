# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Allocation-free structural preflight for untrusted JSON text.

The standard-library decoder necessarily constructs every list, dictionary,
string, and scalar before a caller can enforce schema-level collection limits.
This module performs a conservative lexical pass first.  It is intentionally
not a second JSON parser: malformed syntax is still diagnosed by
``json.loads`` after the resource limits have been enforced.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any


class JsonStructureError(ValueError):
    """The JSON text exceeds a configured structural resource limit."""


DEFAULT_MAX_JSON_INTEGER_BITS = 256


@dataclass(frozen=True)
class JsonStructuralLimits:
    max_depth: int
    max_container_entries: int
    max_total_nodes: int
    max_scalar_tokens: int
    max_total_string_chars: int
    max_string_chars: int
    max_scalar_chars: int = 1024

    def __post_init__(self) -> None:
        for name, value in (
            ("max_depth", self.max_depth),
            ("max_container_entries", self.max_container_entries),
            ("max_total_nodes", self.max_total_nodes),
            ("max_scalar_tokens", self.max_scalar_tokens),
            ("max_total_string_chars", self.max_total_string_chars),
            ("max_string_chars", self.max_string_chars),
            ("max_scalar_chars", self.max_scalar_chars),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("{} must be a positive integer".format(name))


SERVICE_DOCUMENT_JSON_LIMITS = JsonStructuralLimits(
    max_depth=64,
    max_container_entries=1_000_000,
    max_total_nodes=4_000_000,
    max_scalar_tokens=4_000_000,
    max_total_string_chars=16 * 1024 * 1024,
    max_string_chars=1024 * 1024,
)

SERVICE_JSONL_RECORD_LIMITS = JsonStructuralLimits(
    max_depth=64,
    max_container_entries=250_000,
    max_total_nodes=500_000,
    max_scalar_tokens=500_000,
    # A record is already bounded to 8 MiB by the streaming reader. Keep the
    # cumulative string allowance aligned with that byte contract so the
    # existing semantic file-record cap remains the more precise diagnostic.
    max_total_string_chars=8 * 1024 * 1024,
    max_string_chars=1024 * 1024,
)

DATASET_DOCUMENT_JSON_LIMITS = JsonStructuralLimits(
    max_depth=64,
    max_container_entries=250_000,
    max_total_nodes=8_000_000,
    max_scalar_tokens=8_000_000,
    max_total_string_chars=32 * 1024 * 1024,
    max_string_chars=4 * 1024 * 1024,
)

JOURNAL_RECORD_JSON_LIMITS = JsonStructuralLimits(
    max_depth=64,
    max_container_entries=16_384,
    max_total_nodes=32_768,
    max_scalar_tokens=32_768,
    max_total_string_chars=32 * 1024,
    max_string_chars=32 * 1024,
)

SIDECAR_JSON_LIMITS = JsonStructuralLimits(
    max_depth=128,
    max_container_entries=100_000,
    max_total_nodes=250_000,
    max_scalar_tokens=250_000,
    max_total_string_chars=4 * 1024 * 1024,
    max_string_chars=4 * 1024 * 1024,
)


def preflight_json_structure(
    text: str,
    *,
    limits: JsonStructuralLimits,
    label: str = "JSON",
) -> None:
    """Reject resource-amplifying JSON before ``json.loads`` allocates it.

    Counts are conservative for malformed input.  Rejecting malformed input a
    little earlier is safe because the real decoder would reject it anyway.
    Escaped characters, including ``\\uXXXX``, count as one decoded string
    character so a large number of small strings cannot evade the cumulative
    string budget.
    """

    if not isinstance(text, str):
        raise TypeError("JSON structural preflight requires text")

    # Frames store [opening delimiter, comma count, contains content].
    stack: list[list[object]] = []
    node_count = 0
    scalar_count = 0
    total_string_chars = 0
    current_string_chars = 0
    primitive_chars = 0
    in_string = False
    escaped = False
    unicode_escape_remaining = 0
    in_primitive = False

    def claim_node(*, scalar: bool) -> None:
        nonlocal node_count, scalar_count
        node_count += 1
        if node_count > limits.max_total_nodes:
            raise JsonStructureError(
                "{} exceeds the {}-node limit".format(
                    label,
                    limits.max_total_nodes,
                )
            )
        if scalar:
            scalar_count += 1
            if scalar_count > limits.max_scalar_tokens:
                raise JsonStructureError(
                    "{} exceeds the {}-scalar limit".format(
                        label,
                        limits.max_scalar_tokens,
                    )
                )

    def claim_string_character() -> None:
        nonlocal current_string_chars, total_string_chars
        current_string_chars += 1
        total_string_chars += 1
        if current_string_chars > limits.max_string_chars:
            raise JsonStructureError(
                "{} contains a string longer than {} characters".format(
                    label,
                    limits.max_string_chars,
                )
            )
        if total_string_chars > limits.max_total_string_chars:
            raise JsonStructureError(
                "{} exceeds the {} cumulative string-character limit".format(
                    label,
                    limits.max_total_string_chars,
                )
            )

    for character in text:
        if in_string:
            if unicode_escape_remaining:
                unicode_escape_remaining -= 1
                continue
            if escaped:
                escaped = False
                claim_string_character()
                if character == "u":
                    unicode_escape_remaining = 4
                continue
            if character == "\\":
                escaped = True
                continue
            if character == '"':
                in_string = False
                continue
            claim_string_character()
            continue

        if character == '"':
            claim_node(scalar=True)
            in_string = True
            escaped = False
            unicode_escape_remaining = 0
            current_string_chars = 0
            in_primitive = False
            primitive_chars = 0
            if stack:
                stack[-1][2] = True
            continue

        if character in "[{":
            claim_node(scalar=False)
            if stack:
                stack[-1][2] = True
            stack.append([character, 0, False])
            if len(stack) > limits.max_depth:
                raise JsonStructureError(
                    "{} exceeds the {}-level depth limit".format(
                        label,
                        limits.max_depth,
                    )
                )
            in_primitive = False
            primitive_chars = 0
            continue

        if character in "]}":
            in_primitive = False
            primitive_chars = 0
            if stack:
                _opening, comma_count, has_content = stack.pop()
                entry_count = int(comma_count) + 1 if has_content else 0
                if entry_count > limits.max_container_entries:
                    raise JsonStructureError(
                        "{} contains a container with more than {} entries".format(
                            label,
                            limits.max_container_entries,
                        )
                    )
            continue

        if character == ",":
            in_primitive = False
            primitive_chars = 0
            if stack:
                stack[-1][1] = int(stack[-1][1]) + 1
                if int(stack[-1][1]) >= limits.max_container_entries:
                    raise JsonStructureError(
                        "{} contains a container with more than {} entries".format(
                            label,
                            limits.max_container_entries,
                        )
                    )
            continue

        if character == ":" or character.isspace():
            in_primitive = False
            primitive_chars = 0
            continue

        if stack:
            stack[-1][2] = True
        if not in_primitive:
            claim_node(scalar=True)
            in_primitive = True
            primitive_chars = 0
        primitive_chars += 1
        if primitive_chars > limits.max_scalar_chars:
            raise JsonStructureError(
                "{} contains a scalar token longer than {} characters".format(
                    label,
                    limits.max_scalar_chars,
                )
            )


def strict_bounded_json_loads(
    text: str,
    *,
    limits: JsonStructuralLimits,
    label: str = "JSON",
    max_integer_bits: int = DEFAULT_MAX_JSON_INTEGER_BITS,
) -> Any:
    """Decode bounded JSON while rejecting ambiguous or non-finite values.

    Structural preflight runs before the standard decoder allocates the object
    graph. Duplicate object keys, NaN/Infinity, overflowing floats, and
    resource-amplifying integers are rejected uniformly for every persisted
    safety document.
    """

    if type(max_integer_bits) is not int or max_integer_bits <= 0:
        raise ValueError("max_integer_bits must be a positive integer")
    preflight_json_structure(text, limits=limits, label=label)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise JsonStructureError("{} contains duplicate object key {!r}".format(label, key))
            result[key] = value
        return result

    def reject_constant(value: str):
        raise JsonStructureError("{} contains non-finite number {}".format(label, value))

    def finite_float(value: str) -> float:
        try:
            result = float(value)
        except (OverflowError, ValueError) as error:
            raise JsonStructureError("{} contains invalid floating-point number".format(label)) from error
        if not math.isfinite(result):
            raise JsonStructureError("{} contains a non-finite floating-point number".format(label))
        return result

    def bounded_int(value: str) -> int:
        try:
            result = int(value)
        except ValueError as error:
            raise JsonStructureError("{} contains an invalid integer".format(label)) from error
        if result.bit_length() > max_integer_bits:
            raise JsonStructureError(
                "{} contains an integer wider than {} bits".format(
                    label,
                    max_integer_bits,
                )
            )
        return result

    return json.loads(
        text,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
        parse_float=finite_float,
        parse_int=bounded_int,
    )


__all__ = [
    "DATASET_DOCUMENT_JSON_LIMITS",
    "DEFAULT_MAX_JSON_INTEGER_BITS",
    "JOURNAL_RECORD_JSON_LIMITS",
    "JsonStructuralLimits",
    "JsonStructureError",
    "SERVICE_DOCUMENT_JSON_LIMITS",
    "SERVICE_JSONL_RECORD_LIMITS",
    "SIDECAR_JSON_LIMITS",
    "preflight_json_structure",
    "strict_bounded_json_loads",
]
