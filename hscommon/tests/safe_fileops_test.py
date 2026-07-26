# Copyright 2026 dupeGuru contributors
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import contextlib
import errno
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import hscommon.conflict as conflict
from hscommon import atomic_rename
import hscommon.safe_fileops as safe_fileops
from core import fs
from core.safe_action import platform_file_system
from hscommon.conflict import smart_copy, smart_move
from hscommon.safe_fileops import ensure_plain_directory, remove_empty_directories


@pytest.fixture
def rename_no_replace():
    return platform_file_system().rename_no_replace_bound


def _staging_entries(directory: Path):
    return tuple(directory.glob("{}*{}".format(safe_fileops.STAGING_PREFIX, safe_fileops.STAGING_SUFFIX)))


def test_competing_plain_directory_creators_converge_without_replacing_components(tmp_path):
    destination = tmp_path / "one" / "two" / "three"
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(ensure_plain_directory, (destination,) * 32))

    assert results == (destination,) * 32
    assert destination.is_dir()
    assert not destination.is_symlink()


def test_directory_creation_rejects_a_non_directory_component(tmp_path):
    protected = tmp_path / "protected.bin"
    protected.write_bytes(b"must remain")

    with pytest.raises((NotADirectoryError, FileExistsError, OSError)):
        ensure_plain_directory(protected / "child")

    assert protected.read_bytes() == b"must remain"


def test_absolute_path_switches_to_an_authenticated_root_alias_target(
    tmp_path,
    monkeypatch,
):
    lexical_root = Path(tmp_path.anchor) / "dupeguru-lexical-root-alias"
    canonical_root = tmp_path / "canonical-root"
    lexical_path = lexical_root / "one" / "two"
    observed = []

    def authenticate(candidate):
        observed.append(candidate)
        if candidate == lexical_root:
            return canonical_root
        return None

    monkeypatch.setattr(
        safe_fileops,
        "_authenticated_darwin_root_alias",
        authenticate,
    )

    assert safe_fileops._absolute(lexical_path) == canonical_root / "one" / "two"
    assert observed == [lexical_root]


@pytest.mark.skipif(safe_fileops.sys.platform != "darwin", reason="macOS standard root aliases")
def test_copy_accepts_authenticated_darwin_var_alias(
    tmp_path,
    rename_no_replace,
):
    canonical_parent = tmp_path.resolve(strict=True)
    try:
        relative = canonical_parent.relative_to(Path("/private/var"))
    except ValueError:
        pytest.skip("temporary directory is not below /private/var")
    lexical_parent = Path("/var").joinpath(relative)
    lexical_destination = lexical_parent / "alias-created"
    canonical_destination = canonical_parent / "alias-created"
    source = canonical_parent / "source.bin"
    source.write_bytes(b"darwin alias payload")

    result = ensure_plain_directory(lexical_destination)
    smart_copy(
        source,
        lexical_destination / "copied.bin",
        rename_no_replace=rename_no_replace,
    )

    assert result == canonical_destination
    assert canonical_destination.is_dir()
    assert (canonical_destination / "copied.bin").read_bytes() == b"darwin alias payload"


def test_copy_destination_appearing_during_publish_is_not_overwritten(tmp_path, rename_no_replace):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"copy payload")
    sentinel = b"concurrent destination"
    inserted = False

    def publish(source_directory, source_name, candidate_directory, candidate_name, **rename_options):
        nonlocal inserted
        candidate = candidate_directory.path.joinpath(candidate_name)
        if not inserted and candidate == destination:
            destination.write_bytes(sentinel)
            inserted = True
        return rename_no_replace(
            source_directory,
            source_name,
            candidate_directory,
            candidate_name,
            **rename_options,
        )

    smart_copy(source, destination, rename_no_replace=publish)

    assert source.read_bytes() == b"copy payload"
    assert destination.read_bytes() == sentinel
    assert (tmp_path / "[000] destination.bin").read_bytes() == b"copy payload"
    assert not _staging_entries(tmp_path)


def test_move_destination_appearing_during_publish_is_not_overwritten(tmp_path, rename_no_replace):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"move payload")
    sentinel = b"concurrent destination"
    inserted = False

    def publish(source_directory, source_name, candidate_directory, candidate_name, **rename_options):
        nonlocal inserted
        candidate = candidate_directory.path.joinpath(candidate_name)
        if not inserted and candidate == destination:
            destination.write_bytes(sentinel)
            inserted = True
        return rename_no_replace(
            source_directory,
            source_name,
            candidate_directory,
            candidate_name,
            **rename_options,
        )

    smart_move(source, destination, rename_no_replace=publish)

    assert not source.exists()
    assert destination.read_bytes() == sentinel
    assert (tmp_path / "[000] destination.bin").read_bytes() == b"move payload"


@pytest.mark.parametrize("operation", [smart_copy, smart_move])
@pytest.mark.parametrize("replacement_kind", ["in_place", "new_identity"])
def test_scan_bound_operation_rejects_source_generation_drift(
    tmp_path,
    rename_no_replace,
    operation,
    replacement_kind,
):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"reviewed")
    expected = fs.FileSnapshot.from_path_with_content_digest(source)
    if replacement_kind == "in_place":
        source.write_bytes(b"replaced")
        # Model a filesystem timestamp tick which reports unchanged generation
        # metadata even though equal-length bytes were rewritten.
        current = fs.FileSnapshot.from_path(source)
        expected = replace(
            expected,
            device=current.device,
            file_id=current.file_id,
            size=current.size,
            mtime_ns=current.mtime_ns,
            ctime_ns=current.ctime_ns,
        )
    else:
        replacement = tmp_path / "replacement.bin"
        replacement.write_bytes(b"replaced")
        os.replace(replacement, source)

    with pytest.raises(OSError) as caught:
        operation(
            source,
            destination,
            rename_no_replace=rename_no_replace,
            expected_source_snapshot=expected,
        )

    assert caught.value.errno == errno.ESTALE
    assert source.read_bytes() == b"replaced"
    assert not destination.exists()
    assert not _staging_entries(tmp_path)


@pytest.mark.parametrize("operation", [smart_copy, smart_move])
def test_scan_bound_regular_operation_requires_a_full_content_proof(
    tmp_path,
    rename_no_replace,
    operation,
):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"metadata alone is not a proof")

    with pytest.raises(OSError) as caught:
        operation(
            source,
            destination,
            rename_no_replace=rename_no_replace,
            expected_source_snapshot=fs.FileSnapshot.from_path(source),
        )

    assert caught.value.errno == errno.ESTALE
    assert source.read_bytes() == b"metadata alone is not a proof"
    assert not destination.exists()


@pytest.mark.parametrize(
    ("operation", "source_remains"),
    [
        (smart_copy, True),
        (smart_move, False),
    ],
)
def test_parent_swap_cannot_redirect_bound_publish(
    tmp_path,
    rename_no_replace,
    operation,
    source_remains,
):
    source_parent = tmp_path / "source-parent"
    destination_parent = tmp_path / "destination-parent"
    relocated_parent = tmp_path / "relocated-original-parent"
    source_parent.mkdir()
    destination_parent.mkdir()
    source = source_parent / "source.bin"
    destination = destination_parent / "destination.bin"
    source.write_bytes(b"bound payload")
    attempted = False
    swapped = False

    def publish(source_directory, source_name, candidate_directory, candidate_name, **rename_options):
        nonlocal attempted, swapped
        if not attempted:
            attempted = True
            try:
                os.rename(destination_parent, relocated_parent)
            except OSError:
                # Windows directory leases intentionally exclude delete sharing,
                # so the namespace swap is blocked until the commit is complete.
                assert os.name == "nt"
            else:
                # POSIX permits directory renames while a dir_fd is open.  The
                # replacement path must not receive the payload.
                swapped = True
                destination_parent.mkdir()
        return rename_no_replace(
            source_directory,
            source_name,
            candidate_directory,
            candidate_name,
            **rename_options,
        )

    operation(source, destination, rename_no_replace=publish)

    assert attempted
    committed_parent = relocated_parent if swapped else destination_parent
    assert (committed_parent / destination.name).read_bytes() == b"bound payload"
    if swapped:
        assert not destination.exists()
        assert not tuple(destination_parent.iterdir())
    assert source.exists() is source_remains
    assert not _staging_entries(committed_parent)
    assert not _staging_entries(destination_parent)


def test_directory_copy_stays_bound_when_parent_is_swapped_during_staging(
    tmp_path,
    rename_no_replace,
    monkeypatch,
):
    source = tmp_path / "source"
    destination_parent = tmp_path / "destination-parent"
    relocated_parent = tmp_path / "relocated-original-parent"
    destination = destination_parent / "destination"
    source.mkdir()
    destination_parent.mkdir()
    (source / "nested").mkdir()
    (source / "nested" / "payload.bin").write_bytes(b"nested bound payload")
    real_copy_regular = safe_fileops._copy_regular
    attempted = False
    swapped = False

    def copy_after_parent_swap(*args, **kwargs):
        nonlocal attempted, swapped
        if not attempted:
            attempted = True
            try:
                os.rename(destination_parent, relocated_parent)
            except OSError:
                assert os.name == "nt"
            else:
                swapped = True
                destination_parent.mkdir()
        return real_copy_regular(*args, **kwargs)

    monkeypatch.setattr(safe_fileops, "_copy_regular", copy_after_parent_swap)

    smart_copy(source, destination, rename_no_replace=rename_no_replace)

    assert attempted
    committed_parent = relocated_parent if swapped else destination_parent
    assert (committed_parent / "destination" / "nested" / "payload.bin").read_bytes() == b"nested bound payload"
    if swapped:
        assert not tuple(destination_parent.iterdir())
    assert (source / "nested" / "payload.bin").read_bytes() == b"nested bound payload"
    assert not _staging_entries(committed_parent)
    assert not _staging_entries(destination_parent)


def test_directory_copy_reads_the_bound_source_or_blocks_its_parent_swap(
    tmp_path,
    rename_no_replace,
    monkeypatch,
):
    source_parent = tmp_path / "source-parent"
    source = source_parent / "source"
    relocated_parent = tmp_path / "relocated-reviewed-parent"
    replacement_parent = tmp_path / "replacement-parent"
    replacement_after = tmp_path / "replacement-after"
    destination = tmp_path / "destination"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "payload.bin").write_bytes(b"GOOD")
    (replacement_parent / "source" / "nested").mkdir(parents=True)
    (replacement_parent / "source" / "nested" / "payload.bin").write_bytes(b"EVIL")
    real_copy_tree = safe_fileops._copy_tree
    attempted = False
    swapped = False

    def copy_after_source_parent_swap(*args, **kwargs):
        nonlocal attempted, swapped
        if not attempted and kwargs.get("source_directory") is not None:
            attempted = True
            try:
                os.rename(source_parent, relocated_parent)
            except OSError:
                assert os.name == "nt"
                return real_copy_tree(*args, **kwargs)
            swapped = True
            os.rename(replacement_parent, source_parent)
            try:
                return real_copy_tree(*args, **kwargs)
            finally:
                os.rename(source_parent, replacement_after)
                os.rename(relocated_parent, source_parent)
        return real_copy_tree(*args, **kwargs)

    monkeypatch.setattr(
        safe_fileops,
        "_copy_tree",
        copy_after_source_parent_swap,
    )

    smart_copy(
        source,
        destination,
        rename_no_replace=rename_no_replace,
    )

    assert attempted
    assert swapped is (os.name == "posix")
    assert (destination / "nested" / "payload.bin").read_bytes() == b"GOOD"
    assert (source / "nested" / "payload.bin").read_bytes() == b"GOOD"
    evil_parent = replacement_after if swapped else replacement_parent
    assert (evil_parent / "source" / "nested" / "payload.bin").read_bytes() == b"EVIL"


@pytest.mark.parametrize(
    ("operation", "source_remains"),
    [
        (smart_copy, True),
        (smart_move, False),
    ],
)
def test_committed_rename_with_failed_postcheck_is_reported_as_unverified(
    tmp_path,
    rename_no_replace,
    monkeypatch,
    operation,
    source_remains,
):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"committed payload")

    def fail_after_commit(*_args, **_kwargs):
        raise OSError(errno.ESTALE, "simulated post-commit inspection failure")

    monkeypatch.setattr(atomic_rename, "_verify_commit", fail_after_commit)

    with pytest.raises(safe_fileops.UnverifiedRenameCommitError) as caught:
        operation(source, destination, rename_no_replace=rename_no_replace)

    assert caught.value.destination == destination
    assert not caught.value.commit.postcondition_verified
    assert "simulated post-commit inspection failure" in caught.value.reason
    assert destination.read_bytes() == b"committed payload"
    assert source.exists() is source_remains
    assert not _staging_entries(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows namespace rules")
@pytest.mark.parametrize(
    "invalid_name",
    [
        "payload:alternate-stream",
        "trailing-dot.",
        "trailing-space ",
        "CON",
        "con.txt",
        "PRN.log",
        "AUX",
        "NUL.bin",
        "COM1.txt",
        "COM9",
        "LPT1.data",
        "lpt9",
    ],
)
@pytest.mark.parametrize("invalid_side", ["source", "destination"])
def test_windows_bound_rename_rejects_ambiguous_or_device_leaf_names(
    tmp_path,
    invalid_name,
    invalid_side,
):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source remains")
    source_name = invalid_name if invalid_side == "source" else source.name
    destination_name = invalid_name if invalid_side == "destination" else "destination.bin"

    with atomic_rename.open_bound_directory(tmp_path) as directory:
        with pytest.raises(ValueError):
            atomic_rename.rename_no_replace(
                directory,
                source_name,
                directory,
                destination_name,
            )

    assert source.read_bytes() == b"source remains"
    assert not (tmp_path / "destination.bin").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows NTSTATUS contract")
def test_windows_native_rename_accepts_nonnegative_ntstatus(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    source_handle = os.open(source, os.O_RDONLY | getattr(os, "O_BINARY", 0))

    class NativeCall:
        def __call__(self, *_args):
            return 1

    class NativeLibrary:
        NtSetInformationFile = NativeCall()

    try:
        with atomic_rename.open_bound_directory(tmp_path) as directory:
            monkeypatch.setattr(
                atomic_rename.ctypes,
                "WinDLL",
                lambda *_args, **_kwargs: NativeLibrary(),
            )
            atomic_rename._windows_rename_no_replace(
                source_handle,
                directory,
                "destination.bin",
            )
    finally:
        os.close(source_handle)

    assert source.read_bytes() == b"source"
    assert not (tmp_path / "destination.bin").exists()


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links are unavailable")
def test_multiply_linked_source_is_rejected_without_changing_either_name(tmp_path, rename_no_replace):
    source = tmp_path / "source.bin"
    alias = tmp_path / "source-alias.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"linked payload")
    os.link(source, alias)

    with pytest.raises(OSError) as caught:
        smart_move(source, destination, rename_no_replace=rename_no_replace)

    assert caught.value.errno == errno.EMLINK
    assert source.read_bytes() == b"linked payload"
    assert alias.read_bytes() == b"linked payload"
    assert os.stat(source).st_ino == os.stat(alias).st_ino
    assert not destination.exists()


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links are unavailable")
def test_existing_hardlink_destination_bytes_and_identity_are_preserved(tmp_path, rename_no_replace):
    source = tmp_path / "source.bin"
    protected = tmp_path / "protected.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"new payload")
    protected.write_bytes(b"protected payload")
    os.link(protected, destination)
    protected_identity = (os.stat(protected).st_dev, os.stat(protected).st_ino)

    smart_copy(source, destination, rename_no_replace=rename_no_replace)

    assert protected.read_bytes() == b"protected payload"
    assert destination.read_bytes() == b"protected payload"
    assert (os.stat(destination).st_dev, os.stat(destination).st_ino) == protected_identity
    assert (tmp_path / "[000] destination.bin").read_bytes() == b"new payload"


@pytest.mark.parametrize("operation", [smart_copy, smart_move])
def test_cross_volume_or_unsupported_publish_fails_closed(tmp_path, operation):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"source remains")

    def unsupported(
        _source_directory,
        _source_name,
        _destination_directory,
        _destination_name,
        **_rename_options,
    ):
        raise OSError(errno.EXDEV, "simulated cross-volume operation")

    with pytest.raises(OSError) as caught:
        operation(source, destination, rename_no_replace=unsupported)

    assert caught.value.errno == errno.EXDEV
    assert source.read_bytes() == b"source remains"
    assert not destination.exists()
    assert not _staging_entries(tmp_path)


def test_directory_copy_is_staged_and_published_as_a_complete_tree(tmp_path, rename_no_replace):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "nested" / "payload.bin").write_bytes(b"nested payload")

    smart_copy(source, destination, rename_no_replace=rename_no_replace)

    assert (destination / "nested" / "payload.bin").read_bytes() == b"nested payload"
    assert (source / "nested" / "payload.bin").read_bytes() == b"nested payload"
    assert not _staging_entries(tmp_path)


def test_bound_directory_proof_detects_descendant_rewrite_with_restored_mtime(
    tmp_path,
):
    source = tmp_path / "source"
    payload = source / "nested" / "payload.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"AAAA")
    payload_stat = os.stat(payload, follow_symlinks=False)
    source_snapshot = safe_fileops._inspect_source(source)
    parent_identity = safe_fileops._validate_directory(source.parent)

    with atomic_rename.open_bound_directory(
        source.parent,
        expected_identity=parent_identity,
    ) as source_directory:
        safe_fileops._assert_bound_snapshot(
            source_directory,
            source.name,
            source_snapshot,
            source,
        )
        payload.write_bytes(b"BBBB")
        os.utime(
            payload,
            ns=(payload_stat.st_atime_ns, payload_stat.st_mtime_ns),
        )

        with pytest.raises(OSError) as caught:
            safe_fileops._assert_bound_snapshot(
                source_directory,
                source.name,
                source_snapshot,
                source,
            )

    assert caught.value.errno == errno.ESTALE
    assert payload.read_bytes() == b"BBBB"


@pytest.mark.parametrize("operation", [smart_copy, smart_move])
def test_directory_cannot_be_relocated_into_its_own_tree(tmp_path, rename_no_replace, operation):
    source = tmp_path / "source"
    destination = source / "nested" / "destination"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "payload.bin").write_bytes(b"source remains")

    with pytest.raises(OSError) as caught:
        operation(source, destination, rename_no_replace=rename_no_replace)

    assert caught.value.errno == errno.EINVAL
    assert (source / "payload.bin").read_bytes() == b"source remains"
    assert not destination.exists()
    assert not _staging_entries(source / "nested")


def _make_symlink_or_skip(link: Path, target: Path, *, target_is_directory=False):
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as error:
        pytest.skip("Creating a test symlink is unavailable: {}".format(error))


def test_source_symlink_is_rejected_without_publishing(tmp_path, rename_no_replace):
    target = tmp_path / "target.bin"
    source = tmp_path / "source-link.bin"
    destination = tmp_path / "destination.bin"
    target.write_bytes(b"protected target")
    _make_symlink_or_skip(source, target)

    with pytest.raises(OSError) as caught:
        smart_copy(source, destination, rename_no_replace=rename_no_replace)

    assert caught.value.errno == errno.ELOOP
    assert target.read_bytes() == b"protected target"
    assert source.is_symlink()
    assert not destination.exists()


def test_destination_directory_symlink_is_rejected_without_writing_target(tmp_path, rename_no_replace):
    source = tmp_path / "source.bin"
    target_directory = tmp_path / "protected"
    destination_link = tmp_path / "destination"
    source.write_bytes(b"source payload")
    target_directory.mkdir()
    _make_symlink_or_skip(destination_link, target_directory, target_is_directory=True)

    with pytest.raises(OSError) as caught:
        smart_copy(source, destination_link, rename_no_replace=rename_no_replace)

    assert caught.value.errno == errno.ELOOP
    assert source.read_bytes() == b"source payload"
    assert not tuple(target_directory.iterdir())


def test_directory_creation_does_not_follow_an_existing_symlink_component(tmp_path):
    protected = tmp_path / "protected"
    destination_link = tmp_path / "destination"
    protected.mkdir()
    _make_symlink_or_skip(destination_link, protected, target_is_directory=True)

    with pytest.raises(OSError):
        ensure_plain_directory(destination_link / "must-not-exist")

    assert not (protected / "must-not-exist").exists()


@pytest.mark.parametrize("operation", [smart_copy, smart_move])
def test_link_inside_directory_is_rejected_and_source_tree_remains(tmp_path, rename_no_replace, operation):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    protected = tmp_path / "protected.bin"
    source.mkdir()
    protected.write_bytes(b"protected")
    _make_symlink_or_skip(source / "link.bin", protected)

    with pytest.raises(OSError) as caught:
        operation(source, destination, rename_no_replace=rename_no_replace)

    assert caught.value.errno == errno.ELOOP
    assert protected.read_bytes() == b"protected"
    assert (source / "link.bin").is_symlink()
    assert not destination.exists()
    assert not _staging_entries(tmp_path)


def test_partial_os_writes_are_retried_until_the_copy_is_complete(tmp_path, rename_no_replace, monkeypatch):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    payload = bytes(range(251)) * 10_000
    source.write_bytes(payload)
    real_write = safe_fileops.os.write
    forced_partial = False

    def partial_write(handle, data):
        nonlocal forced_partial
        if not forced_partial and len(data) > 7:
            forced_partial = True
            return real_write(handle, data[:7])
        return real_write(handle, data)

    monkeypatch.setattr(safe_fileops.os, "write", partial_write)
    smart_copy(source, destination, rename_no_replace=rename_no_replace)

    assert forced_partial
    assert destination.read_bytes() == payload
    assert source.read_bytes() == payload


def test_failed_partial_write_never_publishes_and_keeps_source(tmp_path, rename_no_replace, monkeypatch):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    payload = b"x" * 4096
    source.write_bytes(payload)
    real_write = safe_fileops.os.write
    calls = 0

    def fail_after_partial(handle, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(handle, data[:13])
        raise OSError(errno.ENOSPC, "simulated full destination")

    monkeypatch.setattr(safe_fileops.os, "write", fail_after_partial)
    with pytest.raises(OSError) as caught:
        smart_copy(source, destination, rename_no_replace=rename_no_replace)

    assert caught.value.errno == errno.ENOSPC
    assert source.read_bytes() == payload
    assert not destination.exists()
    assert not _staging_entries(tmp_path)


def test_silent_staging_write_corruption_is_detected_before_publish(tmp_path, rename_no_replace, monkeypatch):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    payload = b"verified source payload"
    source.write_bytes(payload)
    real_write = safe_fileops.os.write
    corrupted = False

    def corrupt_write(handle, data):
        nonlocal corrupted
        if not corrupted and data:
            changed = bytearray(data)
            changed[0] ^= 0xFF
            corrupted = True
            return real_write(handle, changed)
        return real_write(handle, data)

    monkeypatch.setattr(safe_fileops.os, "write", corrupt_write)
    with pytest.raises(OSError) as caught:
        smart_copy(source, destination, rename_no_replace=rename_no_replace)

    assert corrupted
    assert caught.value.errno == errno.EIO
    assert source.read_bytes() == payload
    assert not destination.exists()
    assert not _staging_entries(tmp_path)


def test_copy_closes_staging_writer_before_verified_readonly_reopen(
    tmp_path,
    rename_no_replace,
    monkeypatch,
):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"writer lifecycle")
    real_new_file = safe_fileops._new_file
    real_open_readonly = safe_fileops._open_staging_readonly
    observed = {}

    def capture_writer(*args, **kwargs):
        handle, identity = real_new_file(*args, **kwargs)
        observed["writer"] = handle
        return handle, identity

    def require_closed_writer(*args, **kwargs):
        with pytest.raises(OSError) as caught:
            os.fstat(observed["writer"])
        assert caught.value.errno == errno.EBADF
        observed["verified"] = True
        return real_open_readonly(*args, **kwargs)

    monkeypatch.setattr(safe_fileops, "_new_file", capture_writer)
    monkeypatch.setattr(safe_fileops, "_open_staging_readonly", require_closed_writer)

    smart_copy(source, destination, rename_no_replace=rename_no_replace)

    assert observed["verified"]
    assert destination.read_bytes() == b"writer lifecycle"
    assert not _staging_entries(tmp_path)


def test_staging_replacement_before_readonly_reopen_fails_closed(
    tmp_path,
    rename_no_replace,
    monkeypatch,
):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    replacement = tmp_path / "replacement.bin"
    source.write_bytes(b"reviewed payload")
    replacement.write_bytes(b"unrecognized replacement")
    real_open_readonly = safe_fileops._open_staging_readonly
    replaced = {}

    def replace_before_reopen(path, **kwargs):
        if not replaced:
            os.replace(replacement, path)
            replaced["path"] = path
        return real_open_readonly(path, **kwargs)

    monkeypatch.setattr(safe_fileops, "_open_staging_readonly", replace_before_reopen)

    with pytest.raises(OSError) as caught:
        smart_copy(source, destination, rename_no_replace=rename_no_replace)

    assert caught.value.errno == errno.ESTALE
    assert source.read_bytes() == b"reviewed payload"
    assert not destination.exists()
    assert replaced["path"].read_bytes() == b"unrecognized replacement"


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing contract")
def test_windows_competing_staging_writer_blocks_verified_reopen(
    tmp_path,
    rename_no_replace,
    monkeypatch,
):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"writer exclusion")
    real_open_readonly = safe_fileops._open_staging_readonly
    attempted = False

    def open_while_writer_is_active(path, **kwargs):
        nonlocal attempted
        attempted = True
        writer = os.open(
            path,
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
        try:
            return real_open_readonly(path, **kwargs)
        finally:
            os.close(writer)

    monkeypatch.setattr(safe_fileops, "_open_staging_readonly", open_while_writer_is_active)

    with pytest.raises(OSError):
        smart_copy(source, destination, rename_no_replace=rename_no_replace)

    assert attempted
    assert source.read_bytes() == b"writer exclusion"
    assert not destination.exists()
    assert not _staging_entries(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing contract")
def test_windows_publish_holds_staging_no_write_lease(
    tmp_path,
    rename_no_replace,
):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"publish lease")
    write_blocked = False

    def publish(source_directory, source_name, destination_directory, destination_name, **rename_options):
        nonlocal write_blocked
        staged_path = source_directory.path / source_name
        try:
            writer = os.open(
                staged_path,
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
        except OSError:
            write_blocked = True
        else:
            os.close(writer)
        return rename_no_replace(
            source_directory,
            source_name,
            destination_directory,
            destination_name,
            **rename_options,
        )

    smart_copy(source, destination, rename_no_replace=publish)

    assert write_blocked
    assert destination.read_bytes() == b"publish lease"
    assert not _staging_entries(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows same-handle publish contract")
def test_windows_publish_renames_the_verified_handle_not_a_name_replacement(
    tmp_path,
    rename_no_replace,
):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    replacement = tmp_path / "replacement.bin"
    stolen = tmp_path / "stolen-reviewed.bin"
    source.write_bytes(b"reviewed payload")
    replacement.write_bytes(b"unrecognized replacement")
    replacement_blocked = False
    observed_commit = None

    def publish(
        source_directory,
        source_name,
        destination_directory,
        destination_name,
        *,
        preopened_source,
    ):
        nonlocal replacement_blocked, observed_commit
        staged_path = source_directory.path / source_name
        try:
            os.replace(staged_path, stolen)
        except OSError:
            replacement_blocked = True
        else:
            os.replace(replacement, staged_path)
        observed_commit = rename_no_replace(
            source_directory,
            source_name,
            destination_directory,
            destination_name,
            preopened_source=preopened_source,
        )
        return observed_commit

    smart_copy(source, destination, rename_no_replace=publish)

    assert replacement_blocked
    assert observed_commit is not None
    assert observed_commit.preopened_source_used
    assert destination.read_bytes() == b"reviewed payload"
    assert replacement.read_bytes() == b"unrecognized replacement"
    assert not stolen.exists()
    assert not _staging_entries(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows same-handle publish contract")
def test_windows_publish_callback_cannot_drop_the_preopened_capability(
    tmp_path,
    rename_no_replace,
):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"reviewed payload")
    capability_received = False

    def publish(
        source_directory,
        source_name,
        destination_directory,
        destination_name,
        *,
        preopened_source,
    ):
        nonlocal capability_received
        capability_received = preopened_source is not None
        # Deliberately violate the callback contract.  The path reopen must
        # fail against the existing no-delete-share lease; no fallback may
        # commit under a second source handle.
        return rename_no_replace(
            source_directory,
            source_name,
            destination_directory,
            destination_name,
        )

    with pytest.raises(OSError):
        smart_copy(source, destination, rename_no_replace=publish)

    assert capability_received
    assert source.read_bytes() == b"reviewed payload"
    assert not destination.exists()
    assert not _staging_entries(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows same-handle move contract")
def test_windows_move_renames_the_verified_handle_not_a_name_replacement(
    tmp_path,
    rename_no_replace,
):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    replacement = tmp_path / "replacement.bin"
    stolen = tmp_path / "stolen-reviewed.bin"
    source.write_bytes(b"reviewed move payload")
    replacement.write_bytes(b"unrecognized replacement")
    replacement_blocked = False
    observed_commit = None

    def publish(
        source_directory,
        source_name,
        destination_directory,
        destination_name,
        *,
        preopened_source,
    ):
        nonlocal replacement_blocked, observed_commit
        try:
            os.replace(source, stolen)
        except OSError:
            replacement_blocked = True
        else:
            os.replace(replacement, source)
        observed_commit = rename_no_replace(
            source_directory,
            source_name,
            destination_directory,
            destination_name,
            preopened_source=preopened_source,
        )
        return observed_commit

    smart_move(source, destination, rename_no_replace=publish)

    assert replacement_blocked
    assert observed_commit is not None
    assert observed_commit.preopened_source_used
    assert destination.read_bytes() == b"reviewed move payload"
    assert replacement.read_bytes() == b"unrecognized replacement"
    assert not source.exists()
    assert not stolen.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows same-handle move contract")
def test_windows_move_callback_cannot_drop_the_preopened_capability(
    tmp_path,
    rename_no_replace,
):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"reviewed move payload")
    capability_received = False

    def publish(
        source_directory,
        source_name,
        destination_directory,
        destination_name,
        *,
        preopened_source,
    ):
        nonlocal capability_received
        capability_received = preopened_source is not None
        return rename_no_replace(
            source_directory,
            source_name,
            destination_directory,
            destination_name,
        )

    with pytest.raises(OSError):
        smart_move(source, destination, rename_no_replace=publish)

    assert capability_received
    assert source.read_bytes() == b"reviewed move payload"
    assert not destination.exists()


def test_move_rejects_generation_drift_after_preflight_before_rename(
    tmp_path,
    rename_no_replace,
    monkeypatch,
):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"reviewed")
    expected = fs.FileSnapshot.from_path_with_content_digest(source)
    rename_called = False
    real_verified_source = safe_fileops._verified_publish_source

    @contextlib.contextmanager
    def mutate_before_capability(staged_path, staged_snapshot, parent_directory):
        before = os.stat(staged_path, follow_symlinks=False)
        staged_path.write_bytes(b"modified")
        os.utime(
            staged_path,
            ns=(before.st_atime_ns, staged_snapshot.mtime_ns),
        )
        with real_verified_source(
            staged_path,
            staged_snapshot,
            parent_directory,
        ) as capability:
            yield capability

    def publish(*args, **kwargs):
        nonlocal rename_called
        rename_called = True
        return rename_no_replace(*args, **kwargs)

    monkeypatch.setattr(
        safe_fileops,
        "_verified_publish_source",
        mutate_before_capability,
    )

    with pytest.raises(OSError) as caught:
        smart_move(
            source,
            destination,
            rename_no_replace=publish,
            expected_source_snapshot=expected,
        )

    assert caught.value.errno == errno.ESTALE
    assert not rename_called
    assert source.read_bytes() == b"modified"
    assert not destination.exists()


def test_directory_move_rechecks_recursive_tree_immediately_before_rename(
    tmp_path,
    rename_no_replace,
    monkeypatch,
):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    nested = source / "nested"
    nested.mkdir(parents=True)
    payload = nested / "payload.bin"
    payload.write_bytes(b"AAAA")
    payload_stat = os.stat(payload, follow_symlinks=False)
    real_bound_assert = safe_fileops._assert_bound_snapshot
    source_assertions = 0
    rename_called = False

    def mutate_before_terminal_tree_assert(
        directory,
        name,
        expected,
        label,
        **assert_options,
    ):
        nonlocal source_assertions
        real_bound_assert(
            directory,
            name,
            expected,
            label,
            **assert_options,
        )
        if label == source:
            source_assertions += 1
            if source_assertions == 3:
                payload.write_bytes(b"BBBB")
                os.utime(
                    payload,
                    ns=(payload_stat.st_atime_ns, payload_stat.st_mtime_ns),
                )

    def publish(*args, **kwargs):
        nonlocal rename_called
        rename_called = True
        return rename_no_replace(*args, **kwargs)

    monkeypatch.setattr(
        safe_fileops,
        "_assert_bound_snapshot",
        mutate_before_terminal_tree_assert,
    )

    with pytest.raises(OSError) as caught:
        smart_move(source, destination, rename_no_replace=publish)

    assert caught.value.errno == errno.ESTALE
    assert source_assertions == 3
    assert not rename_called
    assert payload.read_bytes() == b"BBBB"
    assert os.stat(payload, follow_symlinks=False).st_mtime_ns == payload_stat.st_mtime_ns
    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound cleanup contract")
def test_windows_cleanup_deletes_its_verified_handle_not_a_name_replacement(
    tmp_path,
    monkeypatch,
):
    staging = tmp_path / "{}owned{}".format(
        safe_fileops.STAGING_PREFIX,
        safe_fileops.STAGING_SUFFIX,
    )
    replacement = tmp_path / "replacement.bin"
    stolen = tmp_path / "stolen-owned.bin"
    staging.write_bytes(b"owned staging payload")
    replacement.write_bytes(b"unrecognized replacement")
    replacement_blocked = False
    real_disposition = atomic_rename._set_windows_delete_disposition

    def race_before_disposition(descriptor, path):
        nonlocal replacement_blocked
        try:
            os.replace(staging, stolen)
        except OSError:
            replacement_blocked = True
        else:
            os.replace(replacement, staging)
        return real_disposition(descriptor, path)

    monkeypatch.setattr(
        atomic_rename,
        "_set_windows_delete_disposition",
        race_before_disposition,
    )
    with atomic_rename.open_bound_directory(tmp_path) as directory:
        created = safe_fileops._CreatedEntries(directory)
        created.add_stat(staging, os.lstat(staging))
        created.cleanup()

    assert replacement_blocked
    assert not staging.exists()
    assert replacement.read_bytes() == b"unrecognized replacement"
    assert not stolen.exists()


@pytest.mark.parametrize("operation", [smart_copy, smart_move])
def test_tree_entry_budget_fails_closed_and_cleans_staging(tmp_path, rename_no_replace, monkeypatch, operation):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    for index in range(4):
        (source / "{}.bin".format(index)).write_bytes(bytes((index,)))
    monkeypatch.setattr(safe_fileops, "MAX_TREE_ENTRIES", 3)

    with pytest.raises(OSError) as caught:
        operation(source, destination, rename_no_replace=rename_no_replace)

    assert caught.value.errno == errno.E2BIG
    assert sorted(path.read_bytes() for path in source.glob("*.bin")) == [b"\x00", b"\x01", b"\x02", b"\x03"]
    assert not destination.exists()
    assert not _staging_entries(tmp_path)


@pytest.mark.parametrize("operation", [smart_copy, smart_move])
def test_tree_depth_budget_fails_closed_and_cleans_staging(tmp_path, rename_no_replace, monkeypatch, operation):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    deepest = source
    for index in range(4):
        deepest = deepest / str(index)
        deepest.mkdir(parents=True)
    (deepest / "payload.bin").write_bytes(b"deep payload")
    monkeypatch.setattr(safe_fileops, "MAX_TREE_DEPTH", 2)

    with pytest.raises(OSError) as caught:
        operation(source, destination, rename_no_replace=rename_no_replace)

    assert caught.value.errno == errno.E2BIG
    assert (deepest / "payload.bin").read_bytes() == b"deep payload"
    assert not destination.exists()
    assert not _staging_entries(tmp_path)


def test_reparse_destination_is_rejected_before_publish(tmp_path, rename_no_replace, monkeypatch):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"source")
    destination.write_bytes(b"protected")
    monkeypatch.setattr(conflict, "_is_reparse_point", lambda value: True)

    with pytest.raises(OSError) as caught:
        smart_move(source, destination, rename_no_replace=rename_no_replace)

    assert caught.value.errno == errno.ELOOP
    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"protected"


def test_empty_directory_cleanup_never_removes_boundary_or_nonempty_directory(tmp_path):
    boundary = tmp_path / "selected-root"
    empty = boundary / "empty" / "nested"
    nonempty = boundary / "nonempty"
    empty.mkdir(parents=True)
    nonempty.mkdir()
    marker = nonempty / ".DS_Store"
    marker.write_bytes(b"preserve")

    assert remove_empty_directories(empty, boundary) == 2
    assert boundary.is_dir()
    assert remove_empty_directories(nonempty, boundary) == 0
    assert marker.read_bytes() == b"preserve"
