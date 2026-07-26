# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import pytest

from qt.file_formats import (
    DIRECTORIES_EXTENSION,
    DIRECTORIES_FILTER,
    RESULTS_EXTENSION,
    RESULTS_FILTER,
    ensure_extension,
    translated_directories_filter,
    translated_results_filter,
)


def test_picker_filters_use_the_canonical_persisted_extensions(monkeypatch):
    assert RESULTS_FILTER.endswith("(*{})".format(RESULTS_EXTENSION))
    assert DIRECTORIES_FILTER.endswith("(*{})".format(DIRECTORIES_EXTENSION))
    assert RESULTS_EXTENSION == ".dupeguru"
    assert DIRECTORIES_EXTENSION == ".dupegurudirs"
    monkeypatch.setattr("qt.file_formats.tr", lambda message: "translated:{}".format(message))
    assert translated_results_filter() == "translated:{}".format(RESULTS_FILTER)
    assert translated_directories_filter() == "translated:{}".format(DIRECTORIES_FILTER)


@pytest.mark.parametrize(
    ("path", "extension", "expected"),
    [
        ("results", RESULTS_EXTENSION, "results.dupeguru"),
        ("results.dupeguru", RESULTS_EXTENSION, "results.dupeguru"),
        ("RESULTS.DUPEGURU", RESULTS_EXTENSION, "RESULTS.DUPEGURU"),
        ("folders", DIRECTORIES_EXTENSION, "folders.dupegurudirs"),
        ("folders.DUPEGURUDIRS", DIRECTORIES_EXTENSION, "folders.DUPEGURUDIRS"),
    ],
)
def test_ensure_extension_appends_exactly_once(path, extension, expected):
    assert ensure_extension(path, extension) == expected


@pytest.mark.parametrize("extension", ["", ".", "dupeguru"])
def test_ensure_extension_rejects_invalid_extension_contract(extension):
    with pytest.raises(ValueError):
        ensure_extension("results", extension)
