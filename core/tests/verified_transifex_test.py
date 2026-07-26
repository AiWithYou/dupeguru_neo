import hashlib
import io
import tarfile

import pytest

from scripts import verified_transifex


def _build_archive(path, *, name="tx", member_type=tarfile.REGTYPE, linkname=""):
    content = b"verified tx executable"
    with tarfile.open(path, mode="w:gz", format=tarfile.GNU_FORMAT) as archive:
        member = tarfile.TarInfo(name)
        member.type = member_type
        member.linkname = linkname
        member.mode = 0o755
        member.size = len(content) if member_type == tarfile.REGTYPE else 0
        archive.addfile(
            member,
            io.BytesIO(content) if member_type == tarfile.REGTYPE else None,
        )
    return content


def _trust_archive(monkeypatch, archive, content):
    archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(
        verified_transifex,
        "_RELEASES",
        {
            "1.2.3": verified_transifex.ExpectedRelease(
                archive_sha256=archive_digest,
                members={
                    "tx": verified_transifex.ExpectedMember(
                        mode=0o755,
                        sha256=hashlib.sha256(content).hexdigest(),
                        size=len(content),
                    )
                },
            )
        },
    )
    return archive_digest


def test_verified_transifex_extracts_only_the_pinned_regular_executable(
    tmp_path,
    monkeypatch,
):
    archive = tmp_path / "tx.tar.gz"
    content = _build_archive(archive)
    archive_digest = _trust_archive(monkeypatch, archive, content)

    executable = verified_transifex.extract_verified_transifex(
        archive,
        tmp_path / "tool",
        version="1.2.3",
        expected_archive_sha256=archive_digest,
    )

    assert executable.read_bytes() == content
    assert {path.name for path in executable.parent.iterdir()} == {"tx"}


def test_verified_transifex_rejects_a_workflow_digest_that_is_not_audited(
    tmp_path,
    monkeypatch,
):
    archive = tmp_path / "tx.tar.gz"
    content = _build_archive(archive)
    _trust_archive(monkeypatch, archive, content)

    with pytest.raises(RuntimeError, match="workflow Transifex digest"):
        verified_transifex.extract_verified_transifex(
            archive,
            tmp_path / "tool",
            version="1.2.3",
            expected_archive_sha256="0" * 64,
        )


@pytest.mark.parametrize(
    ("name", "member_type", "linkname", "message"),
    (
        ("../tx", tarfile.REGTYPE, "", "unexpected"),
        ("tx", tarfile.SYMTYPE, "elsewhere", "unsafe"),
        ("tx", tarfile.LNKTYPE, "elsewhere", "unsafe"),
    ),
)
def test_verified_transifex_rejects_traversal_and_archive_links(
    tmp_path,
    monkeypatch,
    name,
    member_type,
    linkname,
    message,
):
    archive = tmp_path / "tx.tar.gz"
    content = _build_archive(
        archive,
        name=name,
        member_type=member_type,
        linkname=linkname,
    )
    archive_digest = _trust_archive(monkeypatch, archive, content)

    with pytest.raises(RuntimeError, match=message):
        verified_transifex.extract_verified_transifex(
            archive,
            tmp_path / "tool",
            version="1.2.3",
            expected_archive_sha256=archive_digest,
        )


def test_verified_transifex_refuses_to_overwrite_output_directory(
    tmp_path,
    monkeypatch,
):
    archive = tmp_path / "tx.tar.gz"
    content = _build_archive(archive)
    archive_digest = _trust_archive(monkeypatch, archive, content)
    output = tmp_path / "tool"
    output.mkdir()

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        verified_transifex.extract_verified_transifex(
            archive,
            output,
            version="1.2.3",
            expected_archive_sha256=archive_digest,
        )
