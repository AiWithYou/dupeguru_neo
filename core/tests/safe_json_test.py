import pytest

from core.safe_json import (
    JsonStructuralLimits,
    JsonStructureError,
    preflight_json_structure,
    strict_bounded_json_loads,
)


def limits(**overrides):
    values = {
        "max_depth": 8,
        "max_container_entries": 8,
        "max_total_nodes": 32,
        "max_scalar_tokens": 24,
        "max_total_string_chars": 32,
        "max_string_chars": 16,
        "max_scalar_chars": 8,
    }
    values.update(overrides)
    return JsonStructuralLimits(**values)


def test_preflight_accepts_valid_nested_json_at_the_boundaries():
    preflight_json_structure(
        '{"items":[1,true,null,{"escaped":"\\u0061\\n"}]}',
        limits=limits(max_depth=3),
        label="test JSON",
    )


@pytest.mark.parametrize(
    ("payload", "overrides", "message"),
    [
        ("[[[0]]]", {"max_depth": 2}, "depth"),
        ("[0,1,2]", {"max_container_entries": 2}, "container"),
        ("[[],[],[]]", {"max_total_nodes": 3}, "node"),
        ("[0,1,2]", {"max_scalar_tokens": 2}, "scalar"),
        ('["ab","cd"]', {"max_total_string_chars": 3}, "cumulative string"),
        ('["abcd"]', {"max_string_chars": 3}, "string longer"),
        ("1234", {"max_scalar_chars": 3}, "scalar token"),
    ],
)
def test_preflight_rejects_each_independent_structural_limit(payload, overrides, message):
    with pytest.raises(JsonStructureError, match=message):
        preflight_json_structure(
            payload,
            limits=limits(**overrides),
            label="test JSON",
        )


def test_escaped_characters_count_as_decoded_characters_cumulatively():
    payload = '["\\u0061","\\n"]'
    preflight_json_structure(
        payload,
        limits=limits(max_total_string_chars=2),
    )
    with pytest.raises(JsonStructureError, match="cumulative string"):
        preflight_json_structure(
            payload,
            limits=limits(max_total_string_chars=1),
        )


@pytest.mark.parametrize(("name", "value"), [("max_depth", 0), ("max_total_nodes", True)])
def test_limits_require_positive_plain_integers(name, value):
    values = limits().__dict__
    values[name] = value
    with pytest.raises(ValueError, match=name):
        JsonStructuralLimits(**values)


@pytest.mark.parametrize(
    "payload",
    (
        '{"key":1,"key":2}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"value":1e9999}',
        '{"value":' + str(1 << 300) + "}",
    ),
)
def test_strict_decoder_rejects_ambiguous_or_unbounded_numbers(payload):
    with pytest.raises(JsonStructureError):
        strict_bounded_json_loads(
            payload,
            limits=limits(max_scalar_chars=1024),
            label="test JSON",
        )
