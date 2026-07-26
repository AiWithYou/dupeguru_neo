import configparser
import json
import subprocess
import sys
from pathlib import Path

from core import __version__

REPOSITORY = Path(__file__).resolve().parents[2]


def test_python_package_exposes_distinct_cli_and_gui_entry_points():
    configuration = configparser.ConfigParser()
    configuration.read(REPOSITORY / "setup.cfg", encoding="utf-8")

    entry_points = configuration["options.entry_points"]
    assert "dupeguru = core.cli:main" in entry_points["console_scripts"]
    assert "dupeguru-gui = run:main" in entry_points["gui_scripts"]
    assert {"run", "run_cli"} <= {
        item.strip() for item in configuration["options"]["py_modules"].splitlines() if item.strip()
    }
    assert "license" not in configuration["metadata"]
    assert 'license_expression="GPL-3.0-only"' in (REPOSITORY / "setup.py").read_text(encoding="utf-8")
    classifiers = {item.strip() for item in configuration["metadata"]["classifiers"].splitlines() if item.strip()}
    assert "Development Status :: 4 - Beta" in classifiers
    assert "Development Status :: 5 - Production/Stable" not in classifiers


def test_cli_launcher_is_qt_free_and_reports_the_package_version():
    result = subprocess.run(
        [sys.executable, str(REPOSITORY / "run_cli.py"), "--version"],
        cwd=REPOSITORY,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == __version__
    assert result.stderr == ""


def test_native_desktop_launchers_use_the_gui_command():
    assert "Exec=dupeguru-gui" in (REPOSITORY / "pkg" / "dupeguru.desktop").read_text(encoding="utf-8")
    for distribution in ("arch", "debian"):
        package_directory = REPOSITORY / "pkg" / distribution
        options = json.loads((package_directory / "dupeguru.json").read_text(encoding="utf-8"))
        desktop = (package_directory / "dupeguru.desktop").read_text(encoding="utf-8")
        assert options["execname"] == "dupeguru"
        assert options["guiexecname"] == "dupeguru-gui"
        assert "Exec={guiexecname}" in desktop


def test_native_installers_map_command_names_to_the_expected_launchers():
    makefile = (REPOSITORY / "Makefile").read_text(encoding="utf-8")
    debian_makefile = (REPOSITORY / "pkg" / "debian" / "Makefile").read_text(encoding="utf-8")

    assert "/bin/dupeguru" in makefile
    assert "/run_cli.py ${DESTDIR}${PREFIX}/bin/dupeguru" in makefile
    assert "/bin/dupeguru-gui" in makefile
    assert "/run.py ${DESTDIR}${PREFIX}/bin/dupeguru-gui" in makefile
    assert 'run_cli.py" "$(CURDIR)/debian/{pkgname}/usr/bin/{execname}"' in debian_makefile
    assert 'run.py" "$(CURDIR)/debian/{pkgname}/usr/bin/{guiexecname}"' in debian_makefile
