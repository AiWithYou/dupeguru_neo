# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import ctypes
import errno
import os

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
    get_file_generation_token_from_fd,
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
def test_windows_generation_uses_usn_and_detects_restored_mtime(tmpdir):
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

    assert first.namespace == second.namespace == "windows-usn-journal-file"
    assert first.version == second.version == 2
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
def test_windows_directory_generation_combines_usn_and_tree_state(tmpdir):
    path = Path(str(tmpdir)).joinpath("directory")
    path.mkdir()
    first_stat = os.stat(path, follow_symlinks=False)
    first = get_entry_generation_token(path, stat_result=first_stat)

    path.joinpath("child.txt").write_text("content", encoding="utf-8")
    os.utime(path, ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns))
    second_stat = os.stat(path, follow_symlinks=False)
    second = get_entry_generation_token(path, stat_result=second_stat)

    assert first.namespace == second.namespace == "windows-usn-journal-directory-tree"
    assert first.version == second.version == 2
    assert first != second
    assert first_stat.st_mtime_ns == second_stat.st_mtime_ns
    with pytest.raises(FileGenerationError, match="directory"):
        get_file_generation_token(path, stat_result=second_stat)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_tree_detects_child_content_with_restored_mtime(tmpdir):
    root = Path(str(tmpdir)).joinpath("directory")
    root.mkdir()
    child = root.joinpath("child.bin")
    child.write_bytes(b"AAAA")
    child_stat = os.stat(child, follow_symlinks=False)
    first = get_entry_generation_token(root)

    child.write_bytes(b"BBBB")
    os.utime(child, ns=(child_stat.st_atime_ns, child_stat.st_mtime_ns))
    second = get_entry_generation_token(root)

    assert first != second
    assert os.stat(child, follow_symlinks=False).st_mtime_ns == child_stat.st_mtime_ns


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_tree_detects_child_change_reverted_to_same_bytes(tmpdir):
    root = Path(str(tmpdir)).joinpath("directory")
    root.mkdir()
    child = root.joinpath("child.bin")
    child.write_bytes(b"AAAA")
    child_stat = os.stat(child, follow_symlinks=False)
    first = get_entry_generation_token(root)

    child.write_bytes(b"BBBB")
    child.write_bytes(b"AAAA")
    os.utime(child, ns=(child_stat.st_atime_ns, child_stat.st_mtime_ns))
    second = get_entry_generation_token(root)

    assert first != second
    assert child.read_bytes() == b"AAAA"
    assert os.stat(child, follow_symlinks=False).st_mtime_ns == child_stat.st_mtime_ns


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_tree_detects_grandchild_content_change(tmpdir):
    root = Path(str(tmpdir)).joinpath("directory")
    nested = root.joinpath("nested")
    nested.mkdir(parents=True)
    child = nested.joinpath("child.bin")
    child.write_bytes(b"AAAA")
    child_stat = os.stat(child, follow_symlinks=False)
    first = get_entry_generation_token(root)

    child.write_bytes(b"BBBB")
    os.utime(child, ns=(child_stat.st_atime_ns, child_stat.st_mtime_ns))
    second = get_entry_generation_token(root)

    assert first != second
    assert os.stat(child, follow_symlinks=False).st_mtime_ns == child_stat.st_mtime_ns


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_tree_detects_external_hardlink_count_change(tmpdir):
    base = Path(str(tmpdir))
    root = base.joinpath("directory")
    root.mkdir()
    child = root.joinpath("child.bin")
    child.write_bytes(b"content")
    first = get_entry_generation_token(root)

    outside_link = base.joinpath("outside-link.bin")
    os.link(child, outside_link)
    second = get_entry_generation_token(root)

    assert first != second
    assert os.stat(child, follow_symlinks=False).st_nlink == 2


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_generation_rejects_unstable_membership(tmpdir, monkeypatch):
    path = Path(str(tmpdir)).joinpath("directory")
    path.mkdir()
    observations = iter((b"a" * 32, b"b" * 32))
    monkeypatch.setattr(
        file_generation_module,
        "_windows_directory_membership_digest",
        lambda _path: next(observations),
    )

    with pytest.raises(FileGenerationError, match="membership changed"):
        get_entry_generation_token(path)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_tree_rejects_entry_limit(tmpdir, monkeypatch):
    root = Path(str(tmpdir)).joinpath("directory")
    root.mkdir()
    root.joinpath("child.bin").write_bytes(b"content")
    monkeypatch.setattr(
        file_generation_module,
        "_MAX_WINDOWS_DIRECTORY_GENERATION_ENTRIES",
        0,
    )

    with pytest.raises(FileGenerationError, match="entry limit exceeded"):
        get_entry_generation_token(root)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_tree_rejects_depth_limit(tmpdir, monkeypatch):
    root = Path(str(tmpdir)).joinpath("directory")
    root.joinpath("nested").mkdir(parents=True)
    monkeypatch.setattr(
        file_generation_module,
        "_MAX_WINDOWS_DIRECTORY_GENERATION_DEPTH",
        0,
    )

    with pytest.raises(FileGenerationError, match="depth limit exceeded"):
        get_entry_generation_token(root)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_tree_rejects_expired_deadline(tmpdir, monkeypatch):
    root = Path(str(tmpdir)).joinpath("directory")
    root.mkdir()
    root.joinpath("child.bin").write_bytes(b"content")
    monkeypatch.setattr(
        file_generation_module,
        "_MAX_WINDOWS_DIRECTORY_GENERATION_SECONDS",
        -1.0,
    )

    with pytest.raises(FileGenerationError, match="deadline exceeded"):
        get_entry_generation_token(root)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_tree_rejects_reparse_child(tmpdir):
    root = Path(str(tmpdir)).joinpath("directory")
    root.mkdir()
    target = Path(str(tmpdir)).joinpath("target.bin")
    target.write_bytes(b"content")
    link = root.joinpath("link.bin")
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip("symbolic links are unavailable: {}".format(error))

    with pytest.raises(FileGenerationError) as caught:
        get_entry_generation_token(root)

    assert caught.value.errno == errno.ELOOP


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_tree_requires_child_file_id_128(tmpdir, monkeypatch):
    root = Path(str(tmpdir)).joinpath("directory")
    root.mkdir()
    child = root.joinpath("child.bin")
    child.write_bytes(b"content")
    real_get_identity = file_generation_module.get_file_identity
    real_handle_identity = file_generation_module._windows_identity_from_handle

    def low_confidence(identity):
        return FileIdentity(
            namespace="windows",
            volume_id=identity.volume_id,
            file_id=123,
            capability=IdentityCapability.WINDOWS_FILE_INDEX_64,
            confidence=IdentityConfidence.MEDIUM,
        )

    def get_identity(path, *args, **kwargs):
        identity = real_get_identity(path, *args, **kwargs)
        return low_confidence(identity) if Path(path).name == child.name else identity

    def handle_identity(handle, path):
        identity = real_handle_identity(handle, path)
        return low_confidence(identity) if Path(path).name == child.name else identity

    monkeypatch.setattr(file_generation_module, "get_file_identity", get_identity)
    monkeypatch.setattr(
        file_generation_module,
        "_windows_identity_from_handle",
        handle_identity,
    )

    with pytest.raises(FileGenerationError, match="128-bit file identity"):
        get_entry_generation_token(root)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_tree_rejects_child_usn_change(tmpdir, monkeypatch):
    root = Path(str(tmpdir)).joinpath("directory")
    root.mkdir()
    child = root.joinpath("child.bin")
    child.write_bytes(b"content")
    real_query = file_generation_module._query_windows_file_usn
    child_queries = 0

    def moving_child_usn(handle, path):
        nonlocal child_queries
        value = real_query(handle, path)
        if Path(path).name == child.name:
            child_queries += 1
            return value + child_queries - 1
        return value

    monkeypatch.setattr(
        file_generation_module,
        "_query_windows_file_usn",
        moving_child_usn,
    )

    with pytest.raises(FileGenerationError, match="entry USN changed"):
        get_entry_generation_token(root)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_tree_requires_child_directory_change_time(tmpdir, monkeypatch):
    root = Path(str(tmpdir)).joinpath("directory")
    nested = root.joinpath("nested")
    nested.mkdir(parents=True)
    real_validate = file_generation_module._validate_windows_generation_handle

    def missing_child_change_time(*args, **kwargs):
        identity, change_time, links = real_validate(*args, **kwargs)
        if Path(args[1]).name == nested.name:
            change_time = 0
        return identity, change_time, links

    monkeypatch.setattr(
        file_generation_module,
        "_validate_windows_generation_handle",
        missing_child_change_time,
    )

    with pytest.raises(FileGenerationError, match="ChangeTime is unavailable"):
        get_entry_generation_token(root)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_tree_closes_handles_when_child_observation_fails(tmpdir, monkeypatch):
    root = Path(str(tmpdir)).joinpath("directory")
    root.mkdir()
    child = root.joinpath("child.bin")
    child.write_bytes(b"content")
    real_query = file_generation_module._query_windows_file_usn
    real_close = file_generation_module._close_handle
    closed_handles = []

    def fail_child_query(handle, path):
        if Path(path).name == child.name:
            raise FileGenerationError(path, "injected child USN failure")
        return real_query(handle, path)

    def recording_close(handle):
        closed_handles.append(handle)
        return real_close(handle)

    monkeypatch.setattr(
        file_generation_module,
        "_query_windows_file_usn",
        fail_child_query,
    )
    monkeypatch.setattr(file_generation_module, "_close_handle", recording_close)

    with pytest.raises(FileGenerationError, match="injected child USN failure"):
        get_entry_generation_token(root)

    assert len(closed_handles) == 2
    assert len(set(closed_handles)) == 2


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_directory_generation_fails_if_change_time_moves_during_read(tmpdir, monkeypatch):
    path = Path(str(tmpdir)).joinpath("directory")
    path.mkdir()
    real_validate = file_generation_module._validate_windows_generation_handle
    calls = 0

    def moving_change_time(*args, **kwargs):
        nonlocal calls
        identity, change_time, links = real_validate(*args, **kwargs)
        calls += 1
        return identity, change_time + calls - 1, links

    monkeypatch.setattr(
        file_generation_module,
        "_validate_windows_generation_handle",
        moving_change_time,
    )

    with pytest.raises(FileGenerationError, match="ChangeTime changed"):
        get_entry_generation_token(path)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_generation_requires_handle_metadata_api(tmpdir, monkeypatch):
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
def test_windows_generation_has_no_timestamp_fallback_when_usn_is_unavailable(tmpdir, monkeypatch):
    path = Path(str(tmpdir)).joinpath("file.bin")
    path.write_bytes(b"content")
    file_usn_called = False

    def unavailable(_handle, observed_path):
        raise FileGenerationError(
            observed_path,
            "query Windows USN journal",
            OSError("journal unavailable"),
        )

    def unexpected_file_usn(_handle, _path):
        nonlocal file_usn_called
        file_usn_called = True
        return 1

    monkeypatch.setattr(file_generation_module, "_query_windows_journal_id", unavailable)
    monkeypatch.setattr(file_generation_module, "_query_windows_file_usn", unexpected_file_usn)

    with pytest.raises(FileGenerationError, match="journal unavailable"):
        get_file_generation_token(path)
    assert not file_usn_called


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_generation_rejects_journal_identifier_race(tmpdir, monkeypatch):
    path = Path(str(tmpdir)).joinpath("file.bin")
    path.write_bytes(b"content")
    journal_ids = iter((10, 11))

    monkeypatch.setattr(
        file_generation_module,
        "_query_windows_journal_id",
        lambda _handle, _path: next(journal_ids),
    )
    monkeypatch.setattr(
        file_generation_module,
        "_query_windows_file_usn",
        lambda _handle, _path: 20,
    )

    with pytest.raises(FileGenerationError, match="journal identifier changed"):
        get_file_generation_token(path)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_generation_rejects_file_usn_race(tmpdir, monkeypatch):
    path = Path(str(tmpdir)).joinpath("file.bin")
    path.write_bytes(b"content")
    file_usns = iter((20, 21))

    monkeypatch.setattr(
        file_generation_module,
        "_query_windows_journal_id",
        lambda _handle, _path: 10,
    )
    monkeypatch.setattr(
        file_generation_module,
        "_query_windows_file_usn",
        lambda _handle, _path: next(file_usns),
    )

    with pytest.raises(FileGenerationError, match="file USN changed"):
        get_file_generation_token(path)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_generation_compounds_journal_and_file_usn(tmpdir, monkeypatch):
    path = Path(str(tmpdir)).joinpath("file.bin")
    path.write_bytes(b"content")
    journal_id = 0x123456789ABCDEF0
    file_usn = 0x0102030405060708

    monkeypatch.setattr(
        file_generation_module,
        "_query_windows_journal_id",
        lambda _handle, _path: journal_id,
    )
    monkeypatch.setattr(
        file_generation_module,
        "_query_windows_file_usn",
        lambda _handle, _path: file_usn,
    )

    token = get_file_generation_token(path)

    assert token == FileGenerationToken(
        "windows-usn-journal-file",
        (journal_id << 64) | file_usn,
        2,
    )
    assert FileGenerationToken.from_encoded(token.encoded) == token


def _windows_usn_record(major_version, file_usn):
    length, offset = (60, 24) if major_version == 2 else (76, 40)
    payload = bytearray(length)
    payload[0:4] = length.to_bytes(4, "little")
    payload[4:6] = major_version.to_bytes(2, "little")
    payload[offset : offset + 8] = file_usn.to_bytes(8, "little", signed=True)
    return bytes(payload)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
@pytest.mark.parametrize("major_version", (2, 3))
def test_windows_generation_parses_supported_usn_records(major_version):
    assert (
        file_generation_module._parse_windows_file_usn(
            _windows_usn_record(major_version, 123456),
            Path("record.bin"),
        )
        == 123456
    )


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_generation_accepts_zero_usn_as_a_journal_scoped_baseline():
    assert (
        file_generation_module._parse_windows_file_usn(
            _windows_usn_record(2, 0),
            Path("record.bin"),
        )
        == 0
    )


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (b"\0" * 7, "header is truncated"),
        (b"\x08\0\0\0\x02\0\0\0", "body is truncated"),
        (_windows_usn_record(2, 1) + b"\0", "length is inconsistent"),
        (_windows_usn_record(4, 1), "version is unsupported"),
        (_windows_usn_record(2, -1), "file USN is negative"),
    ),
)
def test_windows_generation_rejects_malformed_usn_records(payload, message):
    with pytest.raises(FileGenerationError, match=message):
        file_generation_module._parse_windows_file_usn(payload, Path("record.bin"))


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_generation_detects_same_content_rewrite_with_restored_mtime(tmpdir):
    path = Path(str(tmpdir)).joinpath("file.bin")
    original = b"reviewed payload"
    path.write_bytes(original)
    first_stat = path.stat()
    first = get_file_generation_token(path, stat_result=first_stat)

    path.write_bytes(bytes(value ^ 0xFF for value in original))
    path.write_bytes(original)
    os.utime(path, ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns))
    second_stat = path.stat()
    second = get_file_generation_token(path, stat_result=second_stat)

    assert first != second
    assert path.read_bytes() == original
    assert first_stat.st_size == second_stat.st_size
    assert first_stat.st_mtime_ns == second_stat.st_mtime_ns


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
def test_windows_descriptor_generation_reopens_without_write_sharing(tmpdir):
    path = Path(str(tmpdir)).joinpath("file.bin")
    path.write_bytes(b"content")
    writer = file_generation_module._create_file(
        file_generation_module.windows_extended_path(path),
        0x40000000,  # GENERIC_WRITE
        file_generation_module._FILE_SHARE_READ | file_generation_module._FILE_SHARE_DELETE,
        None,
        file_generation_module._OPEN_EXISTING,
        file_generation_module._FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    assert writer != file_generation_module._INVALID_HANDLE_VALUE
    try:
        with path.open("rb") as reader:
            with pytest.raises(FileGenerationError, match="reopen Windows generation handle"):
                get_file_generation_token_from_fd(
                    reader.fileno(),
                    path=path,
                    stat_result=os.fstat(reader.fileno()),
                )
    finally:
        file_generation_module._close_handle(writer)


@pytest.mark.skipif(not ISWINDOWS, reason="Windows-specific generation contract")
def test_windows_generation_rejects_identity_mismatch(tmpdir):
    root = Path(str(tmpdir))
    path = root.joinpath("file.bin")
    other = root.joinpath("other.bin")
    path.write_bytes(b"content")
    other.write_bytes(b"content")

    with pytest.raises(FileGenerationError, match="file identifiers differ"):
        get_file_generation_token(path, expected_identity=get_file_identity(other))
