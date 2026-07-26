#!/usr/bin/env python3

"""Verify and narrowly extract the pinned Transifex CLI release asset."""

from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tarfile
import tempfile

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_ARCHIVE_SIZE = 16 * 1024 * 1024


@dataclass(frozen=True)
class ExpectedMember:
    mode: int
    sha256: str | None
    size: int


@dataclass(frozen=True)
class ExpectedRelease:
    archive_sha256: str
    members: dict[str, ExpectedMember]


_RELEASES = {
    "1.6.10": ExpectedRelease(
        archive_sha256="dcc747ae863dd5a232b6a322f78b8621f43cd6032189ee89e979418cc24927f2",
        members={
            "LICENSE": ExpectedMember(mode=0o644, sha256=None, size=11357),
            "README.md": ExpectedMember(mode=0o644, sha256=None, size=32682),
            "tx": ExpectedMember(
                mode=0o755,
                sha256="32645dc27b82e25d39de6027bbb0ccef5f92a81badac9bd24e736b9eefb63f22",
                size=10977280,
            ),
        },
    )
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_non_symlink(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} must be a regular non-symlink file")
    return path.resolve(strict=True)


def extract_verified_transifex(
    archive_path: Path,
    output_directory: Path,
    *,
    version: str,
    expected_archive_sha256: str,
) -> Path:
    try:
        release = _RELEASES[version]
    except KeyError as error:
        raise RuntimeError(f"unsupported Transifex CLI version: {version!r}") from error
    if _SHA256.fullmatch(expected_archive_sha256) is None or expected_archive_sha256 != release.archive_sha256:
        raise RuntimeError("workflow Transifex digest differs from the audited release digest")

    archive_path = _regular_non_symlink(archive_path, "Transifex archive")
    if not 0 < archive_path.stat().st_size <= _MAX_ARCHIVE_SIZE:
        raise RuntimeError("Transifex archive has an invalid size")
    if _sha256_file(archive_path) != expected_archive_sha256:
        raise RuntimeError("Transifex archive SHA-256 mismatch")

    if output_directory.is_symlink() or os.path.lexists(output_directory):
        raise FileExistsError(f"refusing to overwrite Transifex tool directory: {output_directory}")
    output_directory = output_directory.resolve()
    output_parent = output_directory.parent.resolve(strict=True)
    temporary_directory = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.",
            dir=output_parent,
        )
    )
    temporary_directory.chmod(0o700)
    try:
        found = {}
        executable_content = None
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive:
                if member.name in found:
                    raise RuntimeError(f"duplicate Transifex archive member: {member.name!r}")
                expected = release.members.get(member.name)
                if expected is None:
                    raise RuntimeError(f"unexpected Transifex archive member: {member.name!r}")
                if (
                    member.type != tarfile.REGTYPE
                    or not member.isfile()
                    or member.linkname
                    or member.pax_headers
                    or member.mode != expected.mode
                    or member.size != expected.size
                ):
                    raise RuntimeError(f"unsafe Transifex archive member metadata: {member.name!r}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"cannot read Transifex archive member: {member.name!r}")
                digest = hashlib.sha256()
                size = 0
                content_chunks = [] if member.name == "tx" else None
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
                    if content_chunks is not None:
                        content_chunks.append(chunk)
                if size != expected.size:
                    raise RuntimeError(f"truncated Transifex archive member: {member.name!r}")
                rendered_digest = digest.hexdigest()
                if expected.sha256 is not None and rendered_digest != expected.sha256:
                    raise RuntimeError(f"Transifex executable SHA-256 mismatch: {member.name!r}")
                if content_chunks is not None:
                    executable_content = b"".join(content_chunks)
                found[member.name] = rendered_digest
        if set(found) != set(release.members):
            missing = sorted(set(release.members) - set(found))
            raise RuntimeError(f"Transifex archive members are missing: {missing}")
        if executable_content is None:
            raise RuntimeError("Transifex archive has no executable")

        executable = temporary_directory.joinpath("tx")
        with executable.open("xb") as destination:
            destination.write(executable_content)
            destination.flush()
            os.fsync(destination.fileno())
        executable.chmod(0o755)
        if executable.is_symlink() or not executable.is_file():
            raise RuntimeError("extracted Transifex executable is not a regular file")
        if os.name == "posix" and stat.S_IMODE(executable.stat().st_mode) != 0o755:
            raise RuntimeError("extracted Transifex executable has the wrong mode")
        if _sha256_file(executable) != release.members["tx"].sha256:
            raise RuntimeError("extracted Transifex executable digest mismatch")
        os.replace(temporary_directory, output_directory)
    except BaseException:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise
    return output_directory.joinpath("tx")


def _parser() -> ArgumentParser:
    parser = ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    executable = extract_verified_transifex(
        args.archive,
        args.output_directory,
        version=args.version,
        expected_archive_sha256=args.sha256,
    )
    print(executable)
    return 0


if __name__ == "__main__":
    sys.exit(main())
