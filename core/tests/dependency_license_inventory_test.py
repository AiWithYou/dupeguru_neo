import hashlib
import json
from email.parser import Parser
from pathlib import PurePosixPath

import pytest

from scripts import dependency_license_inventory


class FakeDistribution:
    def __init__(
        self,
        root,
        *,
        name="Example",
        version="1.0",
        license_metadata="License: BSD-2-Clause\nLicense-File: LICENSE\n",
        files=(
            "example-1.0.dist-info/licenses/LICENSE",
            "example-1.0.dist-info/RECORD",
            "example/__init__.py",
            "../../Scripts/example.exe",
        ),
    ):
        self.root = root
        self.version = version
        self.metadata = Parser().parsestr(
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n" f"{license_metadata}\n"
        )
        self.files = [PurePosixPath(path) for path in files]

    def locate_file(self, path):
        return self.root.joinpath(str(path))


def _lock(tmp_path, text="Example==1.0\n"):
    path = tmp_path / "requirements-release.txt"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _source_lock(tmp_path, *, version="1.0", providers=("example",)):
    path = tmp_path / "release-sources.json"
    path.write_text(
        json.dumps(
            {
                "schema": "dupeguru.release-source-lock",
                "schema_version": 1,
                "sources": [
                    {
                        "filename": f"example-{version}.tar.gz",
                        "kind": "pypi-sdist",
                        "name": "Example",
                        "provides": list(providers),
                        "sha256": "a" * 64,
                        "size": 1234,
                        "url": ("https://files.pythonhosted.org/packages/" f"example-{version}.tar.gz"),
                        "version": version,
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _distribution(tmp_path, **kwargs):
    installation_root = tmp_path / "install"
    root = installation_root / "Lib" / "site-packages"
    root.mkdir(parents=True, exist_ok=True)
    distribution = FakeDistribution(root, **kwargs)
    for package_path in distribution.files:
        path = root.joinpath(str(package_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        if package_path.name == "LICENSE":
            content = b"Example license text\n"
        elif package_path.name == "RECORD":
            content = (
                b"example/__init__.py,,\n"
                b"example-1.0.dist-info/licenses/LICENSE,,\n"
                b"example-1.0.dist-info/RECORD,,\n"
            )
        elif package_path.name == "example.exe":
            content = b"console script\n"
        else:
            content = b'__version__ = "1.0"\n'
        path.write_bytes(content)
    return distribution, installation_root


def _generate(tmp_path, monkeypatch, distribution, installation_root):
    monkeypatch.setattr(
        dependency_license_inventory.metadata,
        "distribution",
        lambda _name: distribution,
    )
    lock = _lock(tmp_path)
    source_lock = _source_lock(tmp_path)
    output = dependency_license_inventory.generate_inventory(
        lock,
        source_lock,
        tmp_path / "THIRD-PARTY-LICENSES",
        installation_root=installation_root,
    )
    return lock, source_lock, output


def test_inventory_records_source_record_and_installed_manifest(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    distribution, installation_root = _distribution(tmp_path)
    lock, source_lock, output = _generate(
        tmp_path,
        monkeypatch,
        distribution,
        installation_root,
    )

    dependency_license_inventory.verify_inventory(
        output,
        lock,
        source_lock,
        expected_system=dependency_license_inventory.platform.system(),
        installation_root=installation_root,
    )
    document = dependency_license_inventory._load_json(output / "index.json")
    package = document["packages"][0]
    provenance = package["installed_provenance"]
    record = installation_root / provenance["record"]["path"]
    assert document["schema_version"] == 2
    assert document["lock"]["sha256"] == dependency_license_inventory._sha256_file(lock)
    assert document["source_lock"]["sha256"] == dependency_license_inventory._sha256_file(source_lock)
    assert package["canonical_name"] == "example"
    assert package["name"] == "Example"
    assert package["version"] == "1.0"
    assert package["source_provider"] == "example"
    assert package["source_archive"]["filename"] == "example-1.0.tar.gz"
    assert package["metadata_warnings"] == ["License-Expression metadata is absent"]
    assert package["files"][0]["declared_by_metadata"] is True
    assert provenance["record"]["sha256"] == hashlib.sha256(record.read_bytes()).hexdigest()
    assert provenance["files_manifest"]["algorithm"] == "sha256-length-prefixed-v1"
    assert provenance["files_manifest"]["file_count"] == 4
    assert provenance["files_manifest"]["total_size"] == sum(
        distribution.locate_file(path).stat().st_size for path in distribution.files
    )

    output.joinpath(package["files"][0]["copied_path"]).write_text(
        "tampered",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="(?:size|digest) mismatch"):
        dependency_license_inventory.verify_inventory(
            output,
            lock,
            source_lock,
            installation_root=installation_root,
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "example/__init__.py",
        "example-1.0.dist-info/RECORD",
    ],
)
def test_inventory_recomputes_installed_distribution_provenance(
    tmp_path,
    monkeypatch,
    relative_path,
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    distribution, installation_root = _distribution(tmp_path)
    lock, source_lock, output = _generate(
        tmp_path,
        monkeypatch,
        distribution,
        installation_root,
    )

    distribution.root.joinpath(relative_path).write_bytes(b"changed after inventory\n")

    with pytest.raises(RuntimeError, match="installed distribution provenance mismatch"):
        dependency_license_inventory.verify_inventory(
            output,
            lock,
            source_lock,
            installation_root=installation_root,
        )


def test_inventory_rejects_source_provider_or_archive_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    distribution, installation_root = _distribution(tmp_path)
    monkeypatch.setattr(
        dependency_license_inventory.metadata,
        "distribution",
        lambda _name: distribution,
    )
    lock = _lock(tmp_path)
    source_lock = _source_lock(tmp_path, providers=("other",))
    with pytest.raises(RuntimeError, match="missing provider"):
        dependency_license_inventory.generate_inventory(
            lock,
            source_lock,
            tmp_path / "missing-provider",
            installation_root=installation_root,
        )

    source_lock = _source_lock(tmp_path)
    output = dependency_license_inventory.generate_inventory(
        lock,
        source_lock,
        tmp_path / "source-tamper",
        installation_root=installation_root,
    )
    index = output / "index.json"
    document = dependency_license_inventory._load_json(index)
    document["packages"][0]["source_archive"]["sha256"] = "b" * 64
    index.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    output.joinpath("index.txt").write_text(
        dependency_license_inventory._render_text(document),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(RuntimeError, match="source archive mismatch"):
        dependency_license_inventory.verify_inventory(
            output,
            lock,
            source_lock,
            installation_root=installation_root,
        )


def test_inventory_rejects_missing_or_escaping_distribution_files(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    lock = _lock(tmp_path)
    source_lock = _source_lock(tmp_path)
    missing, installation_root = _distribution(tmp_path, files=())
    monkeypatch.setattr(
        dependency_license_inventory.metadata,
        "distribution",
        lambda _name: missing,
    )
    with pytest.raises(RuntimeError, match="file count is outside its bound"):
        dependency_license_inventory.generate_inventory(
            lock,
            source_lock,
            tmp_path / "missing",
            installation_root=installation_root,
        )

    escaping_root = tmp_path / "escaping-install"
    site_root = escaping_root / "Lib" / "site-packages"
    site_root.mkdir(parents=True)
    escaping = FakeDistribution(
        site_root,
        files=(
            "example-1.0.dist-info/licenses/LICENSE",
            "example-1.0.dist-info/RECORD",
            "../../../../outside.py",
        ),
    )
    for path in escaping.files[:2]:
        located = escaping.locate_file(path)
        located.parent.mkdir(parents=True, exist_ok=True)
        located.write_bytes(b"license or record\n")
    (tmp_path / "outside.py").write_bytes(b"outside\n")
    monkeypatch.setattr(
        dependency_license_inventory.metadata,
        "distribution",
        lambda _name: escaping,
    )
    with pytest.raises(RuntimeError, match="escapes sys.prefix"):
        dependency_license_inventory.generate_inventory(
            lock,
            source_lock,
            tmp_path / "escaping",
            installation_root=escaping_root,
        )


def test_inventory_rejects_version_or_license_metadata_mismatch(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    lock = _lock(tmp_path)
    source_lock = _source_lock(tmp_path)
    wrong_version, installation_root = _distribution(tmp_path, version="2.0")
    monkeypatch.setattr(
        dependency_license_inventory.metadata,
        "distribution",
        lambda _name: wrong_version,
    )
    with pytest.raises(RuntimeError, match="does not match pinned"):
        dependency_license_inventory.generate_inventory(
            lock,
            source_lock,
            tmp_path / "wrong-version",
            installation_root=installation_root,
        )

    no_metadata_root = tmp_path / "no-metadata-install"
    no_metadata_site = no_metadata_root / "Lib" / "site-packages"
    no_metadata_site.mkdir(parents=True)
    no_metadata = FakeDistribution(
        no_metadata_site,
        license_metadata="",
    )
    for path in no_metadata.files:
        located = no_metadata.locate_file(path)
        located.parent.mkdir(parents=True, exist_ok=True)
        located.write_bytes(b"installed file\n")
    monkeypatch.setattr(
        dependency_license_inventory.metadata,
        "distribution",
        lambda _name: no_metadata,
    )
    with pytest.raises(RuntimeError, match="no license metadata"):
        dependency_license_inventory.generate_inventory(
            lock,
            source_lock,
            tmp_path / "no-metadata",
            installation_root=no_metadata_root,
        )


def test_inventory_enforces_installed_file_count_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "123456789")
    distribution, installation_root = _distribution(tmp_path)
    monkeypatch.setattr(
        dependency_license_inventory.metadata,
        "distribution",
        lambda _name: distribution,
    )
    monkeypatch.setattr(dependency_license_inventory, "_MAX_DISTRIBUTION_FILES", 3)

    with pytest.raises(RuntimeError, match="file count is outside its bound"):
        dependency_license_inventory.generate_inventory(
            _lock(tmp_path),
            _source_lock(tmp_path),
            tmp_path / "bounded",
            installation_root=installation_root,
        )


def test_inventory_normalizes_windows_record_separator_and_duplicate(tmp_path):
    distribution, installation_root = _distribution(tmp_path)
    distribution.files.append(PurePosixPath(r"example-1.0.dist-info\RECORD"))

    provenance = dependency_license_inventory._installed_distribution_provenance(
        distribution,
        dependency_license_inventory._installation_root(installation_root),
    )

    assert provenance["files_manifest"]["file_count"] == 4
    assert provenance["record"]["path"].endswith("/example-1.0.dist-info/RECORD")


def test_platform_marker_selects_pywin32_only_for_windows(tmp_path):
    lock = _lock(
        tmp_path,
        'Example==1.0\npywin32==312; sys_platform == "win32"\n',
    )

    _, linux = dependency_license_inventory._read_lock(lock, system="Linux")
    _, windows = dependency_license_inventory._read_lock(lock, system="Windows")

    assert [requirement.name for requirement in linux] == ["Example"]
    assert [requirement.name for requirement in windows] == ["Example", "pywin32"]
