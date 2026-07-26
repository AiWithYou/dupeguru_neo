from email.parser import Parser
import hashlib
import json
from pathlib import PurePosixPath
import platform

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


def _source_lock(tmp_path):
    path = tmp_path / "release-sources.json"
    document = {
        "schema": "dupeguru.release-source-lock",
        "schema_version": 1,
        "sources": [
            {
                "filename": f"Python-{platform.python_version()}.tar.xz",
                "name": "CPython",
                "provides": ["cpython-runtime"],
                "sha256": "1" * 64,
                "url": "https://www.python.org/source.tar.xz",
                "version": platform.python_version(),
            },
            {
                "filename": "pyinstaller-6.21.0.tar.gz",
                "name": "PyInstaller",
                "provides": ["pyinstaller-bootloader"],
                "sha256": "2" * 64,
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
