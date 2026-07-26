# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Shared bounded JSON decoder for video tool and cache inputs."""

from __future__ import annotations

import json
import math

from core.safe_json import JsonStructuralLimits, JsonStructureError, preflight_json_structure

_UTF8_SIZE_CHUNK_CHARS = 64 * 1024
_MAX_JSON_INTEGER_BITS = 256


class VideoJsonError(ValueError):
    """A video JSON payload is malformed or exceeds its resource contract."""


def strict_bounded_json_loads(
    payload,
    *,
    max_bytes: int,
    limits: JsonStructuralLimits,
    label: str,
):
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    try:
        if isinstance(payload, bytes):
            if len(payload) > max_bytes:
                raise VideoJsonError("{} exceeds the {}-byte limit".format(label, max_bytes))
            text = payload.decode("utf-8", errors="strict")
        elif isinstance(payload, str):
            if _utf8_size_exceeds(payload, max_bytes):
                raise VideoJsonError("{} exceeds the {}-byte limit".format(label, max_bytes))
            text = payload
        else:
            raise VideoJsonError("{} must be UTF-8 text or bytes".format(label))
        preflight_json_structure(text, limits=limits, label=label)
        return json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
            parse_int=_bounded_json_int,
        )
    except VideoJsonError:
        raise
    except (
        JsonStructureError,
        MemoryError,
        OverflowError,
        RecursionError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        raise VideoJsonError("{} is not valid bounded UTF-8 JSON".format(label)) from error


def _utf8_size_exceeds(text: str, maximum: int) -> bool:
    total = 0
    for offset in range(0, len(text), _UTF8_SIZE_CHUNK_CHARS):
        total += len(text[offset : offset + _UTF8_SIZE_CHUNK_CHARS].encode("utf-8", errors="strict"))
        if total > maximum:
            return True
    return False


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VideoJsonError("video JSON contains a duplicate object key")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise VideoJsonError("video JSON contains a non-finite number: {}".format(value))


def _finite_json_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise VideoJsonError("video JSON contains a non-finite number")
    return result


def _bounded_json_int(value):
    unsigned = value[1:] if value.startswith("-") else value
    # 2**256 has 78 decimal digits. Rejecting longer tokens before int()
    # avoids allocating an arbitrary-precision integer for hostile JSON.
    if len(unsigned) > 78:
        raise VideoJsonError("video JSON contains an integer wider than 256 bits")
    result = int(value)
    if result.bit_length() > _MAX_JSON_INTEGER_BITS:
        raise VideoJsonError("video JSON contains an integer wider than 256 bits")
    return result


__all__ = [
    "VideoJsonError",
    "strict_bounded_json_loads",
]
