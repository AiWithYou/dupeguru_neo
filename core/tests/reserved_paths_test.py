# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

import os

import pytest

from core.dataset_discovery import (
    DatasetDiscoveryError,
    DatasetRootRequest,
    _normalize_request_paths,
)
from core.dataset_executor import DatasetBundleExecutor
from core.reserved_paths import (
    RESERVED_INTERNAL_DIRECTORY_NAMES,
    is_reserved_internal_directory,
    is_within_reserved_internal_directory,
)


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC anchors")
@pytest.mark.parametrize(
    "template",
    (
        r"\\server\{name}\operations\payload",
        r"\\?\UNC\server\{name}\operations\payload",
        r"\\server\{upper}\operations\payload",
        r"\\server\{name}.\operation\payload",
        r"\\server\{name}::$INDEX_ALLOCATION\operation\payload",
    ),
)
@pytest.mark.parametrize("reserved_name", sorted(RESERVED_INTERNAL_DIRECTORY_NAMES))
def test_reserved_unc_share_names_are_part_of_the_private_namespace(template, reserved_name):
    path = template.format(name=reserved_name, upper=reserved_name.upper())
    assert is_within_reserved_internal_directory(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC anchors")
@pytest.mark.parametrize("reserved_name", sorted(RESERVED_INTERNAL_DIRECTORY_NAMES))
def test_reserved_unc_share_root_is_itself_an_internal_directory(reserved_name):
    assert is_reserved_internal_directory(
        r"\\server\{}".format(reserved_name),
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC anchors")
def test_ordinary_unc_share_does_not_become_a_private_namespace():
    assert not is_within_reserved_internal_directory(
        r"\\server\ordinary-share\operations\payload",
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC anchors")
@pytest.mark.parametrize("reserved_name", sorted(RESERVED_INTERNAL_DIRECTORY_NAMES))
def test_dataset_state_base_rejects_a_reserved_unc_share(reserved_name):
    with pytest.raises(ValueError, match="outside every private namespace"):
        DatasetBundleExecutor(
            state_root=r"\\server\{}".format(reserved_name),
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC anchors")
@pytest.mark.parametrize("entry", ("input", "destination", "protected", "state"))
@pytest.mark.parametrize("reserved_name", sorted(RESERVED_INTERNAL_DIRECTORY_NAMES))
def test_dataset_discovery_rejects_reserved_unc_share_before_filesystem_access(
    tmp_path,
    entry,
    reserved_name,
):
    input_root = tmp_path / "input"
    destination_root = tmp_path / "destination"
    input_root.mkdir()
    destination_root.mkdir()
    reserved_unc = r"\\server\{}".format(reserved_name)
    roots = (reserved_unc,) if entry == "input" else (str(input_root),)
    destination = reserved_unc if entry == "destination" else str(destination_root)
    protected = (reserved_unc,) if entry == "protected" else ()
    state_root = reserved_unc if entry == "state" else None
    request = DatasetRootRequest(
        roots=roots,
        destination_root=destination,
        protected_roots=protected,
        state_root=state_root,
    )

    with pytest.raises(DatasetDiscoveryError) as raised:
        _normalize_request_paths(request)

    assert raised.value.code == "reserved_internal_path"
