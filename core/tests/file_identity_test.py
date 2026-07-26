# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import ctypes
import os
import time

from pathlib import Path

import pytest

from hscommon.plat import ISWINDOWS

import core.file_generation as file_generation_module
import core.file_identity as file_identity_module
from core.file_identity import (
    FileIdentity,
    FileIdentityError,
    IdentityCapability,
    IdentityConfidence,
    IdentityVerdict,
    get_file_identity,
    get_file_identity_from_fd,
    same_physical_file,
)
from core.file_generation import (
    FileGenerationError,
    FileGenerationToken,
    get_entry_generation_token,
    get_file_generation_token,
)


def test_same_path_has_same_physical_identity(tmpdir):
    path = Path(str(tmpdir)).joinpath("file.txt")
    path.write_text("content")

    first = get_file_identity(path)
    second = get_file_identity(path)

    comparison = same_physical_file(first, second)
    assert comparison.verdict == IdentityVerdict.SAME
    assert comparison.is_same is True
    assert comparison.confidence >= IdentityConfidence.MEDIUM


def test_hardlinks_have_same_physical_identity(tmpdir):
    root = Path(str(tmpdir))
    original = root.joinpath("original.bin")
    hardlink = root.joinpath("hardlink.bin")
    original.write_bytes(b"same physical file")
    try:
        os.link(str(original), str(hardlink))
    except OSError as error:
        pytest.skip("hardlinks are unavailable: {}".format(error))

    comparison = same_physical_file(get_file_identity(original), get_file_identity(hardlink))

    assert comparison.verdict == IdentityVerdict.SAME
    assert comparison.is_same is True


def test_different_files_on_same_volume_have_different_identities(tmpdir):
    root = Path(str(tmpdir))
    first_path = root.joinpath("first.bin")
    second_path = root.joinpath("second.bin")
    first_path.write_bytes(b"same bytes")
    second_path.write_bytes(b"same bytes")

    comparison = same_physical_file(get_file_identity(first_path), get_file_identity(second_path))

    assert comparison.verdict == IdentityVerdict.DIFFERENT
    assert comparison.is_same is False
    assert comparison.reason == "file identifiers differ on the same volume"


def test_different_volumes_are_not_the_same_physical_file():
    first = FileIdentity(
        namespace="posix",
        volume_id=1,
        file_id=10,
        capability=IdentityCapability.POSIX_DEVICE_INODE,
        confidence=IdentityConfidence.HIGH,
    )
    second = FileIdentity(
        namespace="posix",
        volume_id=2,
        file_id=10,
        capability=IdentityCapability.POSIX_DEVICE_INODE,
        confidence=IdentityConfidence.HIGH,
    )

    comparison = same_physical_file(first, second)

    assert comparison.verdict == IdentityVerdict.DIFFERENT
    assert comparison.reason == "volume identifiers differ"


def test_foreign_identity_namespaces_are_not_guessed_from_paths():
    posix = FileIdentity(
        namespace="posix",
        volume_id=1,
        file_id=10,
        capability=IdentityCapability.POSIX_DEVICE_INODE,
        confidence=IdentityConfidence.HIGH,
    )
    windows = FileIdentity(
        namespace="windows",
        volume_id=1,
        file_id=10,
        capability=IdentityCapability.WINDOWS_FILE_INDEX_64,
        confidence=IdentityConfidence.MEDIUM,
    )

    comparison = same_physical_file(posix, windows)

    assert comparison.verdict == IdentityVerdict.UNKNOWN
    assert comparison.is_same is None
    assert comparison.confidence == IdentityConfidence.LOW


def test_incompatible_capabilities_are_unknown():
    modern = FileIdentity(
        namespace="windows",
        volume_id=1,
        file_id=b"\x01" * 16,
        capability=IdentityCapability.WINDOWS_FILE_ID_128,
        confidence=IdentityConfidence.HIGH,
    )
    legacy = FileIdentity(
        namespace="windows",
        volume_id=1,
        file_id=1,
        capability=IdentityCapability.WINDOWS_FILE_INDEX_64,
        confidence=IdentityConfidence.MEDIUM,
    )

    comparison = same_physical_file(modern, legacy)

    assert comparison.verdict == IdentityVerdict.UNKNOWN
    assert comparison.is_same is None


def test_nonexistent_path_has_explicit_identity_error(tmpdir):
    missing = Path(str(tmpdir)).joinpath("missing")

    with pytest.raises(FileIdentityError) as error:
        get_file_identity(missing)

    assert error.value.path == missing
    assert error.value.operation
    assert isinstance(error.value.cause, OSError)


def test_generation_token_serialization_is_versioned_and_namespaced():
    token = FileGenerationToken("test-counter", 123)

    assert token.encoded == b"dupeguru-content-generation\x00v1\x00test-counter\x00123"
    assert token.encoded != b"123"


@pytest.mark.parametrize(
    ("namespace", "value", "version"),
    (
        ("token", True, 1),
        ("token", 1.0, 1),
        ("token", -1, 1),
        ("token", 1, True),
        ("token", 1, 1.0),
        ("token", 1, 0),
        (1, 1, 1),
    ),
)
def test_generation_token_rejects_noncanonical_fields(namespace, value, version):
    with pytest.raises(ValueError):
        FileGenerationToken(namespace, value, version)


def test_generation_token_rejects_unsafe_path_and_following(tmpdir):
    path = Path(str(tmpdir)).joinpath("file")
    path.touch()

    with pytest.raises(FileGenerationError, match="NUL"):
        get_file_generation_token("{}\0suffix".format(path))
    with pytest.raises(FileGenerationError, match="symlink following"):
        get_file_generation_token(path, follow_symlinks=True)


def test_symlink_identity_is_distinct_when_not_followed(tmpdir):
    root = Path(str(tmpdir))
    target = root.joinpath("target.txt")
    link = root.joinpath("link.txt")
    target.write_text("target")
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip("symbolic links are unavailable: {}".format(error))

    target_identity = get_file_identity(target)
    link_identity = get_file_identity(link, follow_symlinks=False)
    followed_identity = get_file_identity(link, follow_symlinks=True)

    assert same_physical_file(target_identity, link_identity).verdict == IdentityVerdict.DIFFERENT
    assert same_physical_file(target_identity, followed_identity).verdict == IdentityVerdict.SAME
    with pytest.raises(FileGenerationError):
        get_file_generation_token(link)


@pytest.mark.skipif(ISWINDOWS, reason="POSIX-specific generation contract")
def test_posix_generation_token_uses_ctime_ns(tmpdir):
    path = Path(str(tmpdir)).joinpath("file")
    path.touch()
    stat_result = os.stat(path, follow_symlinks=False)

    token = get_file_generation_token(
        path,
        stat_result=stat_result,
        expected_identity=get_file_identity(path, stat_result=stat_result),
    )

    assert token == FileGenerationToken("posix-ctime-ns", stat_result.st_ctime_ns)


@pytest.mark.skipif(ISWINDOWS, reason="POSIX-specific identity contract")
def test_posix_identity_uses_device_and_inode(tmpdir):
    path = Path(str(tmpdir)).joinpath("file")
    path.touch()
    stat_result = path.stat()

    identity = get_file_identity(path)

    assert identity.namespace == "posix"
    assert identity.volume_id == stat_result.st_dev
    assert identity.file_id == stat_result.st_ino
    assert identity.capability == IdentityCapability.POSIX_DEVICE_INODE
    assert identity.confidence == IdentityConfidence.HIGH


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific identity contract")
def test_windows_identity_uses_volume_and_file_id(tmpdir):
    root = Path(str(tmpdir))
    file_path = root.joinpath("file.txt")
    file_path.write_text("content")

    root_identity = get_file_identity(root)
    file_identity = get_file_identity(file_path)

    assert root_identity.namespace == "windows"
    assert file_identity.namespace == "windows"
    assert root_identity.volume_id == file_identity.volume_id
    assert file_identity.capability in {
        IdentityCapability.WINDOWS_FILE_ID_128,
        IdentityCapability.WINDOWS_FILE_INDEX_64,
    }
    if file_identity.capability == IdentityCapability.WINDOWS_FILE_ID_128:
        assert isinstance(file_identity.file_id, bytes)
        assert len(file_identity.file_id) == 16
        assert file_identity.confidence == IdentityConfidence.HIGH
    else:
        assert isinstance(file_identity.file_id, int)
        assert file_identity.confidence == IdentityConfidence.MEDIUM


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_generation_uses_change_time_and_detects_restored_mtime(tmpdir):
    path = Path(str(tmpdir)).joinpath("file.bin")
    path.write_bytes(b"first")
    first_stat = os.stat(path, follow_symlinks=False)
    first_identity = get_file_identity(path, stat_result=first_stat)
    first = get_file_generation_token(
        path,
        stat_result=first_stat,
        expected_identity=first_identity,
    )

    path.write_bytes(b"other")
    os.utime(path, ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns))
    second_stat = os.stat(path, follow_symlinks=False)
    second_identity = get_file_identity(path, stat_result=second_stat)
    second = get_file_generation_token(
        path,
        stat_result=second_stat,
        expected_identity=second_identity,
    )

    assert first.namespace == second.namespace == "windows-change-time-100ns"
    assert first != second
    assert first_identity == second_identity
    assert first_stat.st_size == second_stat.st_size
    assert first_stat.st_mtime_ns == second_stat.st_mtime_ns


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific identity contract")
def test_windows_open_descriptor_identity_is_nonzero_file_id_128(tmpdir):
    path = Path(str(tmpdir)).joinpath("file.bin")
    path.write_bytes(b"content")

    with path.open("rb") as handle:
        identity = get_file_identity_from_fd(handle.fileno(), path=path)

    assert identity.capability is IdentityCapability.WINDOWS_FILE_ID_128
    assert identity.confidence is IdentityConfidence.HIGH
    assert isinstance(identity.file_id, bytes)
    assert len(identity.file_id) == 16
    assert any(identity.file_id)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific identity contract")
def test_windows_all_zero_file_id_128_is_never_high_confidence(monkeypatch):
    def zero_extended(_handle, _info_class, info_pointer, _size):
        info = ctypes.cast(
            info_pointer,
            ctypes.POINTER(file_identity_module._FILE_ID_INFO),
        ).contents
        info.VolumeSerialNumber = 123
        return 1

    def legacy(_handle, info_pointer):
        info = ctypes.cast(
            info_pointer,
            ctypes.POINTER(file_identity_module._BY_HANDLE_FILE_INFORMATION),
        ).contents
        info.dwVolumeSerialNumber = 123
        info.nFileIndexLow = 456
        return 1

    monkeypatch.setattr("core.file_identity._get_file_information_ex", zero_extended)
    monkeypatch.setattr("core.file_identity._get_file_information", legacy)

    identity = file_identity_module._windows_identity_from_handle(1, Path("zero-id"))

    assert identity.capability is IdentityCapability.WINDOWS_FILE_INDEX_64
    assert identity.confidence is IdentityConfidence.MEDIUM


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific identity contract")
def test_windows_zero_volume_file_id_128_is_never_high_confidence(monkeypatch):
    def zero_volume_extended(_handle, _info_class, info_pointer, _size):
        info = ctypes.cast(
            info_pointer,
            ctypes.POINTER(file_identity_module._FILE_ID_INFO),
        ).contents
        info.VolumeSerialNumber = 0
        info.FileId.Identifier[0] = 1
        return 1

    def legacy(_handle, info_pointer):
        info = ctypes.cast(
            info_pointer,
            ctypes.POINTER(file_identity_module._BY_HANDLE_FILE_INFORMATION),
        ).contents
        info.dwVolumeSerialNumber = 123
        info.nFileIndexLow = 456
        return 1

    monkeypatch.setattr("core.file_identity._get_file_information_ex", zero_volume_extended)
    monkeypatch.setattr("core.file_identity._get_file_information", legacy)

    identity = file_identity_module._windows_identity_from_handle(1, Path("zero-volume"))

    assert identity.capability is IdentityCapability.WINDOWS_FILE_INDEX_64
    assert identity.confidence is IdentityConfidence.MEDIUM


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_generation_uses_change_time_not_creation_time(tmpdir):
    path = Path(str(tmpdir)).joinpath("directory")
    path.mkdir()
    first_stat = os.stat(path, follow_symlinks=False)
    first = get_entry_generation_token(path, stat_result=first_stat)

    time.sleep(0.02)
    path.joinpath("child.txt").write_text("content", encoding="utf-8")
    os.utime(path, ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns))
    second_stat = os.stat(path, follow_symlinks=False)
    second = get_entry_generation_token(path, stat_result=second_stat)

    assert first.namespace == second.namespace == "windows-change-time-100ns"
    assert first != second
    assert first_stat.st_mtime_ns == second_stat.st_mtime_ns
    with pytest.raises(FileGenerationError, match="directory"):
        get_file_generation_token(path, stat_result=second_stat)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_generation_has_no_creation_time_fallback(tmpdir, monkeypatch):
    path = Path(str(tmpdir)).joinpath("file.bin")
    path.write_bytes(b"content")
    identity = get_file_identity(path)
    close_calls = []
    real_close = file_generation_module._close_handle

    def recording_close(handle):
        close_calls.append(handle)
        return real_close(handle)

    monkeypatch.setattr(file_generation_module, "_close_handle", recording_close)
    monkeypatch.setattr("core.file_generation._get_file_information_ex", None)

    with pytest.raises(FileGenerationError, match="GetFileInformationByHandleEx is unavailable"):
        get_file_generation_token(path, expected_identity=identity)
    assert len(close_calls) == 1
    path.unlink()


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_generation_rejects_an_open_writer(tmpdir):
    path = Path(str(tmpdir)).joinpath("file.bin")
    path.write_bytes(b"content")
    handle = file_generation_module._create_file(
        file_generation_module.windows_extended_path(path),
        0x40000000,  # GENERIC_WRITE
        file_generation_module._FILE_SHARE_READ | file_generation_module._FILE_SHARE_DELETE,
        None,
        file_generation_module._OPEN_EXISTING,
        file_generation_module._FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    assert handle != file_generation_module._INVALID_HANDLE_VALUE
    try:
        with pytest.raises(FileGenerationError, match="open no-follow Windows generation handle"):
            get_file_generation_token(path)
    finally:
        file_generation_module._close_handle(handle)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_generation_rejects_identity_mismatch(tmpdir):
    root = Path(str(tmpdir))
    path = root.joinpath("file.bin")
    other = root.joinpath("other.bin")
    path.write_bytes(b"content")
    other.write_bytes(b"content")

    with pytest.raises(FileGenerationError, match="file identifiers differ"):
        get_file_generation_token(path, expected_identity=get_file_identity(other))
