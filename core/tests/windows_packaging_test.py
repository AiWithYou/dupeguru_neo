import json
from pathlib import Path
import subprocess

import package as packaging
import pytest

REPOSITORY = Path(__file__).resolve().parents[2]


def test_windows_wheel_build_completes_missing_python_abi_metadata():
    setup_source = (REPOSITORY / "setup.py").read_text(encoding="utf-8")

    assert 'if config_vars.get("Py_DEBUG") is None:' in setup_source
    assert 'config_vars["Py_DEBUG"] = int(hasattr(sys, "gettotalrefcount"))' in setup_source


def test_nsis_cli_freezer_is_console_only_and_excludes_qt():
    arguments = packaging._windows_cli_pyinstaller_arguments()

    assert "--name=dupeguru" in arguments
    assert "--onefile" in arguments
    assert "--console" in arguments
    assert "--windowed" not in arguments
    assert "--exclude-module=PyQt6" in arguments
    assert "--exclude-module=qt" in arguments
    assert not any(argument.startswith("--add-data") for argument in arguments)
    assert arguments[-1] == "run_cli.py"


def test_frozen_gui_embeds_project_notices_and_source_locks():
    arguments = packaging._frozen_notice_pyinstaller_arguments(";")

    assert arguments == [
        "--add-data=LICENSE;.",
        "--add-data=THIRD_PARTY_NOTICES.md;.",
        f"--add-data={Path('hscommon', 'LICENSE')};hscommon",
        "--add-data=release-sources.json;.",
        "--add-data=requirements-release.txt;.",
        f"--add-data={Path('docs', 'PORTABLE-NOTICE.txt')};.",
    ]


def test_nsis_input_requires_the_separately_named_cli_executable():
    script = (REPOSITORY / "setup.nsi").read_text(encoding="utf-8")

    assert '!define APPBINARY "dupeguru-neo"' in script
    assert '!define CLIBINARY "dupeguru"' in script
    assert ('File /r /x "${CLIBINARY}.exe" ' '"${SOURCEPATH}\\${APPBINARY}-win${BITS}\\*"') in script
    assert 'File "${SOURCEPATH}\\${APPBINARY}-win${BITS}\\${CLIBINARY}.exe"' in script
    assert 'WriteRegStr HKCR ".dupeguru" "" "${APPID}.File"' in script
    assert 'ReadRegStr $1 HKCR ".dupeguru" ""' in script
    assert ".dupeguruneo" not in script


def test_nsis_installs_and_uninstalls_legal_notices_and_source_locks():
    script = (REPOSITORY / "setup.nsi").read_text(encoding="utf-8")

    required_installed_files = {
        "LICENSE": "${APPLICENSE}",
        "THIRD_PARTY_NOTICES.md": "${APPNOTICE}",
        "HSCOMMON-BSD-3-CLAUSE.txt": "${HSCOMMONLICENSE}",
        "release-sources.json": "${SOURCELOCK}",
        "requirements-release.txt": "${DEPENDENCYLOCK}",
        "PORTABLE-NOTICE.txt": "${PORTABLENOTICE}",
    }
    for destination, source in required_installed_files.items():
        assert f'File /oname={destination} "{source}"' in script
        assert f'Delete "$INSTDIR\\{destination}"' in script
    assert 'RMDir /r "$INSTDIR\\_internal"' in script


def test_nsis_preserves_and_only_restores_a_previous_file_association():
    script = (REPOSITORY / "setup.nsi").read_text(encoding="utf-8")

    install_association = script.split("; Set file association", 1)[1].split("; Uninstall Entry", 1)[0]
    assert 'ReadRegStr $1 HKCR ".dupeguru" ""' in install_association
    assert 'StrCmp $1 "${APPID}.File" NoAssociationBackup' in install_association
    assert 'WriteRegStr HKCR ".dupeguru" "backup_val" "$1"' in install_association
    assert install_association.index('"backup_val"') < install_association.index(
        'WriteRegStr HKCR ".dupeguru" "" "${APPID}.File"'
    )

    uninstall_association = script.split('ReadRegStr $1 HKCR ".dupeguru" ""', 2)[-1]
    assert 'StrCmp $1 "${APPID}.File" 0 NotOwn' in uninstall_association
    assert 'ReadRegStr $1 HKCR ".dupeguru" "backup_val"' in uninstall_association
    assert 'StrCmp $1 "" 0 RestoreAssociation' in uninstall_association
    assert "RestoreAssociation:" in uninstall_association
    assert 'WriteRegStr HKCR ".dupeguru" "" $1' in uninstall_association
    assert 'DeleteRegValue HKCR ".dupeguru" "backup_val"' in uninstall_association


def test_nsis_rejects_windows_versions_unsupported_by_the_qt6_runtime():
    script = (REPOSITORY / "setup.nsi").read_text(encoding="utf-8")

    assert "${AtLeastWin10}" in script
    assert "${AtLeastWin7}" not in script
    assert "Windows 10 or Windows 11 is required" in script


def test_nsis_offers_the_japanese_installer_language():
    script = (REPOSITORY / "setup.nsi").read_text(encoding="utf-8")

    assert '!insertmacro MUI_LANGUAGE "Japanese"' in script


def test_nsis_cli_smoke_checks_version_doctor_and_schema(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((tuple(command), kwargs))
        arguments = tuple(command[1:])
        if arguments == ("--version",):
            stdout = "5.0.0\n"
        elif arguments == ("doctor",):
            stdout = json.dumps(
                {
                    "schema": "dupeguru.doctor-report",
                    "pyqt_imported": False,
                }
            )
        elif arguments == ("schema", "deletion-plan"):
            stdout = json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "urn:dupeguru-neo:schema:deletion-plan:1",
                }
            )
        else:
            raise AssertionError(f"unexpected CLI smoke: {command}")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(packaging.subprocess, "run", run)

    packaging._smoke_windows_cli(
        REPOSITORY / "dist" / "dupeguru.exe",
        "5.0.0",
    )

    assert [call[0][1:] for call in calls] == [
        ("--version",),
        ("doctor",),
        ("schema", "deletion-plan"),
    ]
    assert all(call[1]["timeout"] == 90 for call in calls)
    assert all(call[1]["env"]["PYTHONUTF8"] == "1" for call in calls)


def test_nsis_cli_is_staged_without_overwriting(tmp_path):
    source = tmp_path / "dist" / "dupeguru.exe"
    destination = tmp_path / "dist" / "dupeguru-neo-win64" / "dupeguru.exe"
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    source.write_bytes(b"console-cli")

    packaging._stage_windows_cli(source, destination)

    assert destination.read_bytes() == b"console-cli"
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        packaging._stage_windows_cli(source, destination)
