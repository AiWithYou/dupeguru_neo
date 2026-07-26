from email.parser import Parser
import hashlib
import io
import json
from pathlib import PurePosixPath
import platform
import tarfile

import pytest

from scripts import frozen_runtime_license_inventory


class FakePyInstallerDistribution:
    def __init__(self, root, content):
        self.root = root
        self.version = "6.21.0"
        self.metadata = Parser().parsestr(
            "Metadata-Version: 2.4\n"
            "Name: PyInstaller\n"
            "Version: 6.21.0\n"
            "License-Expression: GPL-2.0-or-later\n"
            "License-File: COPYING.txt\n"
        )
        self.files = [PurePosixPath("pyinstaller-6.21.0.dist-info/licenses/COPYING.txt")]
        path = root.joinpath(str(self.files[0]))
        path.parent.mkdir(parents=True)
        path.write_bytes(content)

    def locate_file(self, path):
        return self.root.joinpath(str(path))


def _source_lock(
    tmp_path,
    *,
    cpython_archive=None,
    cpython_url=None,
    cpython_overrides=None,
):
    path = tmp_path / "release-sources.json"
    cpython_sha256 = hashlib.sha256(cpython_archive).hexdigest() if cpython_archive is not None else "1" * 64
    cpython_size = len(cpython_archive) if cpython_archive is not None else 1
    cpython_source = {
        "filename": f"Python-{platform.python_version()}.tar.xz",
        "kind": "python-official-source",
        "name": "CPython",
        "provides": ["cpython-runtime"],
        "sha256": cpython_sha256,
        "size": cpython_size,
        "url": cpython_url
        or (
            "https://www.python.org/ftp/python/"
            f"{platform.python_version()}/"
            f"Python-{platform.python_version()}.tar.xz"
        ),
        "version": platform.python_version(),
    }
    cpython_source.update(cpython_overrides or {})
    document = {
        "schema": "dupeguru.release-source-lock",
        "schema_version": 1,
        "sources": [
            cpython_source,
            {
                "filename": "pyinstaller-6.21.0.tar.gz",
                "kind": "pypi-sdist",
                "name": "PyInstaller",
                "provides": ["pyinstaller-bootloader"],
                "sha256": "2" * 64,
                "size": 1,
                "url": "https://files.pythonhosted.org/source.tar.gz",
                "version": "6.21.0",
            },
        ],
    }
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _cpython_archive(
    *,
    license_content=b"PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n",
    license_name=None,
    license_type=tarfile.REGTYPE,
    extra_members=(),
):
    version = platform.python_version()
    root = f"Python-{version}"
    member_name = license_name or f"{root}/LICENSE"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
        root_info = tarfile.TarInfo(root)
        root_info.type = tarfile.DIRTYPE
        archive.addfile(root_info)
        license_info = tarfile.TarInfo(member_name)
        license_info.type = license_type
        if license_type == tarfile.REGTYPE:
            license_info.size = len(license_content)
            archive.addfile(license_info, io.BytesIO(license_content))
        else:
            license_info.linkname = f"{root}/OTHER"
            archive.addfile(license_info)
        for member_name, member_content in extra_members:
            member = tarfile.TarInfo(member_name)
            member.size = len(member_content)
            archive.addfile(member, io.BytesIO(member_content))
    return buffer.getvalue()


def _force_cpython_fallback(monkeypatch):
    def absent():
        raise frozen_runtime_license_inventory._CPythonLicenseAbsent("test runtime has no license")

    monkeypatch.setattr(
        frozen_runtime_license_inventory,
        "_cpython_license",
        absent,
    )


def _resolve_downloaded_license(
    tmp_path,
    monkeypatch,
    *,
    pinned_archive,
    downloaded_archive=None,
    source_url=None,
):
    source_lock = _source_lock(
        tmp_path,
        cpython_archive=pinned_archive,
        cpython_url=source_url,
    )
    source = frozen_runtime_license_inventory._source_components(source_lock)["cpython-runtime"]
    downloaded_paths = []

    def fake_curl(url, output_path, maximum_size):
        assert url == source["url"]
        assert maximum_size == len(pinned_archive)
        downloaded_paths.append(output_path)
        output_path.write_bytes(pinned_archive if downloaded_archive is None else downloaded_archive)

    monkeypatch.setattr(
        frozen_runtime_license_inventory,
        "_run_curl",
        fake_curl,
    )
    _force_cpython_fallback(monkeypatch)
    try:
        result = frozen_runtime_license_inventory._resolved_cpython_license(
            source_lock,
            source,
        )
    finally:
        assert downloaded_paths
        assert not downloaded_paths[0].parent.exists()
    return result


def test_inventory_copies_and_verifies_cpython_and_bootloader_terms(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    pyinstaller_license = b"GNU GENERAL PUBLIC LICENSE\n\nBootloader Exception\n"
    distribution = FakePyInstallerDistribution(
        tmp_path / "site-packages",
        pyinstaller_license,
    )
    monkeypatch.setattr(
        frozen_runtime_license_inventory.metadata,
        "distribution",
        lambda name: distribution,
    )
    cpython_license = b"PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n"
    monkeypatch.setattr(
        frozen_runtime_license_inventory,
        "_cpython_license",
        lambda: ("LICENSE.txt", cpython_license),
    )
    source_lock = _source_lock(tmp_path)

    output = frozen_runtime_license_inventory.generate_inventory(
        source_lock,
        tmp_path / "FROZEN-RUNTIME-LICENSES",
    )

    frozen_runtime_license_inventory.verify_inventory(
        output,
        source_lock,
        expected_system=platform.system(),
    )
    document = frozen_runtime_license_inventory._load_json(
        output / "index.json",
        "test inventory",
    )
    assert [component["component"] for component in document["components"]] == [
        "cpython-runtime",
        "pyinstaller-bootloader",
    ]
    assert document["source_lock"]["sha256"] == hashlib.sha256(source_lock.read_bytes()).hexdigest()
    copied = {
        component["component"]: output / component["files"][0]["copied_path"] for component in document["components"]
    }
    assert b"PYTHON SOFTWARE FOUNDATION" in copied["cpython-runtime"].read_bytes()
    assert b"Bootloader Exception" in copied["pyinstaller-bootloader"].read_bytes()

    copied["pyinstaller-bootloader"].write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="digest mismatch"):
        frozen_runtime_license_inventory.verify_inventory(
            output,
            source_lock,
        )


def test_inventory_rejects_pyinstaller_text_without_bootloader_exception(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    distribution = FakePyInstallerDistribution(
        tmp_path / "site-packages",
        b"GNU GENERAL PUBLIC LICENSE only\n",
    )
    monkeypatch.setattr(
        frozen_runtime_license_inventory.metadata,
        "distribution",
        lambda name: distribution,
    )
    monkeypatch.setattr(
        frozen_runtime_license_inventory,
        "_cpython_license",
        lambda: (
            "LICENSE.txt",
            b"PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n",
        ),
    )

    with pytest.raises(RuntimeError, match="required bootloader terms"):
        frozen_runtime_license_inventory.generate_inventory(
            _source_lock(tmp_path),
            tmp_path / "FROZEN-RUNTIME-LICENSES",
        )


def test_cpython_license_fallback_downloads_exact_pinned_archive_and_cleans_temp(
    tmp_path,
    monkeypatch,
):
    archive = _cpython_archive()

    filename, content = _resolve_downloaded_license(
        tmp_path,
        monkeypatch,
        pinned_archive=archive,
    )

    assert filename == "LICENSE"
    assert content == b"PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n"


@pytest.mark.parametrize(
    ("downloaded_archive", "error"),
    [
        (lambda archive: archive + b"x", "size mismatch"),
        (
            lambda archive: archive[:-1] + bytes([archive[-1] ^ 1]),
            "digest mismatch",
        ),
    ],
)
def test_cpython_license_fallback_rejects_unpinned_download(
    tmp_path,
    monkeypatch,
    downloaded_archive,
    error,
):
    archive = _cpython_archive()

    with pytest.raises(RuntimeError, match=error):
        _resolve_downloaded_license(
            tmp_path,
            monkeypatch,
            pinned_archive=archive,
            downloaded_archive=downloaded_archive(archive),
        )


def test_cpython_license_fallback_rejects_missing_exact_license_member(
    tmp_path,
    monkeypatch,
):
    version = platform.python_version()
    archive = _cpython_archive(license_name=f"Python-{version}/LICENSE.txt")

    with pytest.raises(RuntimeError, match="missing .*?/LICENSE"):
        _resolve_downloaded_license(
            tmp_path,
            monkeypatch,
            pinned_archive=archive,
        )


def test_cpython_license_fallback_rejects_link_license_member(
    tmp_path,
    monkeypatch,
):
    archive = _cpython_archive(license_type=tarfile.SYMTYPE)

    with pytest.raises(RuntimeError, match="must be a regular archive member"):
        _resolve_downloaded_license(
            tmp_path,
            monkeypatch,
            pinned_archive=archive,
        )


def test_cpython_license_fallback_rejects_license_without_psf_terms(
    tmp_path,
    monkeypatch,
):
    archive = _cpython_archive(license_content=b"not the CPython license\n")

    with pytest.raises(RuntimeError, match="expected PSF terms"):
        _resolve_downloaded_license(
            tmp_path,
            monkeypatch,
            pinned_archive=archive,
        )


def test_cpython_license_fallback_bounds_decompressed_archive(
    tmp_path,
    monkeypatch,
):
    archive = _cpython_archive()
    monkeypatch.setattr(
        frozen_runtime_license_inventory,
        "_MAX_CPYTHON_SOURCE_EXPANDED_SIZE",
        1024,
    )

    with pytest.raises(RuntimeError, match="decompressed-size limit"):
        _resolve_downloaded_license(
            tmp_path,
            monkeypatch,
            pinned_archive=archive,
        )


def test_cpython_license_fallback_rejects_non_https_source(
    tmp_path,
    monkeypatch,
):
    archive = _cpython_archive()
    source_lock = _source_lock(
        tmp_path,
        cpython_archive=archive,
        cpython_url=(
            "http://www.python.org/ftp/python/"
            f"{platform.python_version()}/"
            f"Python-{platform.python_version()}.tar.xz"
        ),
    )
    source = frozen_runtime_license_inventory._source_components(source_lock)["cpython-runtime"]
    _force_cpython_fallback(monkeypatch)

    with pytest.raises(RuntimeError, match="source URL is unsafe"):
        frozen_runtime_license_inventory._resolved_cpython_license(
            source_lock,
            source,
        )


def test_cpython_local_license_still_requires_a_safe_pinned_source(
    tmp_path,
    monkeypatch,
):
    archive = _cpython_archive()
    source_lock = _source_lock(
        tmp_path,
        cpython_archive=archive,
        cpython_url=(
            "http://www.python.org/ftp/python/"
            f"{platform.python_version()}/"
            f"Python-{platform.python_version()}.tar.xz"
        ),
    )
    source = frozen_runtime_license_inventory._source_components(source_lock)["cpython-runtime"]
    monkeypatch.setattr(
        frozen_runtime_license_inventory,
        "_cpython_license",
        lambda: ("LICENSE.txt", b"PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n"),
    )

    with pytest.raises(RuntimeError, match="source URL is unsafe"):
        frozen_runtime_license_inventory._resolved_cpython_license(
            source_lock,
            source,
        )


@pytest.mark.parametrize(
    ("overrides", "error"),
    (
        (
            {
                "url": (
                    "https://example.com/ftp/python/"
                    f"{platform.python_version()}/"
                    f"Python-{platform.python_version()}.tar.xz"
                )
            },
            "source URL is unsafe",
        ),
        ({"kind": "pypi-sdist"}, "source kind is invalid"),
        ({"filename": "Python-unpinned.tar.xz"}, "source filename is invalid"),
    ),
)
def test_cpython_license_rejects_an_invalid_official_source_mapping(
    tmp_path,
    monkeypatch,
    overrides,
    error,
):
    archive = _cpython_archive()
    source_lock = _source_lock(
        tmp_path,
        cpython_archive=archive,
        cpython_overrides=overrides,
    )
    source = frozen_runtime_license_inventory._source_components(source_lock)["cpython-runtime"]
    monkeypatch.setattr(
        frozen_runtime_license_inventory,
        "_cpython_license",
        lambda: ("LICENSE.txt", b"PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n"),
    )

    with pytest.raises(RuntimeError, match=error):
        frozen_runtime_license_inventory._resolved_cpython_license(
            source_lock,
            source,
        )


def test_cpython_license_fallback_rejects_archive_member_path_traversal(
    tmp_path,
    monkeypatch,
):
    version = platform.python_version()
    archive = _cpython_archive(
        extra_members=((f"Python-{version}/../escape.txt", b"escape"),),
    )

    with pytest.raises(RuntimeError, match="outside its expected root"):
        _resolve_downloaded_license(
            tmp_path,
            monkeypatch,
            pinned_archive=archive,
        )


def test_cpython_license_fallback_rejects_duplicate_archive_member(
    tmp_path,
    monkeypatch,
):
    version = platform.python_version()
    archive = _cpython_archive(
        extra_members=((f"Python-{version}/LICENSE", b"duplicate"),),
    )

    with pytest.raises(RuntimeError, match="duplicate member path"):
        _resolve_downloaded_license(
            tmp_path,
            monkeypatch,
            pinned_archive=archive,
        )


def test_cpython_license_fallback_enforces_archive_member_count(
    tmp_path,
    monkeypatch,
):
    archive = _cpython_archive()
    monkeypatch.setattr(
        frozen_runtime_license_inventory,
        "_MAX_CPYTHON_SOURCE_MEMBER_COUNT",
        1,
    )

    with pytest.raises(RuntimeError, match="too many members"):
        _resolve_downloaded_license(
            tmp_path,
            monkeypatch,
            pinned_archive=archive,
        )
