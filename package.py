# Copyright 2017 Virgil Dupras
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

import sys
import os
import os.path as op
import compileall
import shutil
import json
from argparse import ArgumentParser
import platform
import distro
import re
import subprocess

from hscommon.build import (
    copy_packages,
    build_debian_changelog,
    get_module_version,
    filereplace,
    copy,
    setup_package_argparser,
    copy_all,
)

GUI_ENTRY_SCRIPT = "run.py"
CLI_ENTRY_SCRIPT = "run_cli.py"
WINDOWS_CLI_BINARY = "dupeguru"
LOCALE_DIR = "build/locale"
HELP_DIR = "build/help"
FROZEN_NOTICE_DATA = (
    ("LICENSE", "."),
    ("THIRD_PARTY_NOTICES.md", "."),
    (op.join("hscommon", "LICENSE"), "hscommon"),
    ("release-sources.json", "."),
    ("requirements-release.txt", "."),
    (op.join("docs", "PORTABLE-NOTICE.txt"), "."),
)


def run_checked(args, *, cwd=None):
    """Run a packaging command and abort immediately on a non-zero exit."""

    printable = subprocess.list2cmdline([str(arg) for arg in args])
    print(printable)
    return subprocess.run([str(arg) for arg in args], cwd=cwd, check=True)


def _frozen_notice_pyinstaller_arguments(data_separator):
    return [f"--add-data={source}{data_separator}{destination}" for source, destination in FROZEN_NOTICE_DATA]


def _windows_cli_pyinstaller_arguments():
    return [
        f"--name={WINDOWS_CLI_BINARY}",
        "--onefile",
        "--console",
        "--noconfirm",
        "--clean",
        "--noupx",
        "--exclude-module=PyQt6",
        "--exclude-module=qt",
        CLI_ENTRY_SCRIPT,
    ]


def _smoke_windows_cli(executable, version):
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"

    def run_cli(*arguments):
        try:
            return subprocess.run(
                [str(executable), *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=90,
                env=environment,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"Windows CLI smoke timed out: {arguments[0]}") from error

    version_result = run_cli("--version")
    if version_result.returncode != 0 or version_result.stdout.strip() != version:
        raise RuntimeError(
            "Windows CLI --version smoke failed: " f"{version_result.stdout.strip()!r} {version_result.stderr[-1000:]}"
        )

    doctor_result = run_cli("doctor")
    if doctor_result.returncode != 0:
        raise RuntimeError(
            f"Windows CLI doctor smoke failed ({doctor_result.returncode}): " f"{doctor_result.stderr[-1000:]}"
        )
    try:
        doctor = json.loads(doctor_result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Windows CLI doctor smoke did not emit JSON") from error
    if doctor.get("schema") != "dupeguru.doctor-report" or doctor.get("pyqt_imported") is not False:
        raise RuntimeError("Windows CLI doctor smoke did not prove the Qt-free CLI boundary")

    schema_result = run_cli("schema", "deletion-plan")
    if schema_result.returncode != 0:
        raise RuntimeError(
            f"Windows CLI schema smoke failed ({schema_result.returncode}): " f"{schema_result.stderr[-1000:]}"
        )
    try:
        schema = json.loads(schema_result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Windows CLI schema smoke did not emit JSON") from error
    if (
        schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        or schema.get("$id") != "urn:dupeguru-neo:schema:deletion-plan:1"
    ):
        raise RuntimeError("Windows CLI schema smoke returned an unexpected schema")


def _stage_windows_cli(cli_source, cli_destination):
    if not op.isfile(cli_source) or op.islink(cli_source):
        raise RuntimeError(f"frozen Windows CLI output is not a regular file: {cli_source}")
    if op.lexists(cli_destination):
        raise RuntimeError(f"refusing to overwrite frozen Windows CLI destination: {cli_destination}")
    shutil.copy2(cli_source, cli_destination)


def parse_args():
    parser = ArgumentParser()
    setup_package_argparser(parser)
    return parser.parse_args()


def check_loc_doc():
    if not op.exists(LOCALE_DIR):
        print('Locale files are missing. Have you run "build.py --loc"?')
    # include help files if they are built otherwise exit as they should be included?
    if not op.exists(HELP_DIR):
        print('Help files are missing. Have you run "build.py --doc"?')
    return op.exists(LOCALE_DIR) and op.exists(HELP_DIR)


def copy_files_to_package(destpath, packages, with_so):
    # when with_so is true, we keep .so files in the package, and otherwise, we don't. We need this
    # flag because when building debian src pkg, we *don't* want .so files (they're compiled later)
    # and when we're packaging under Arch, we're packaging a binary package, so we want them.
    if op.exists(destpath):
        shutil.rmtree(destpath)
    os.makedirs(destpath)
    for entry_script in (GUI_ENTRY_SCRIPT, CLI_ENTRY_SCRIPT):
        shutil.copy(entry_script, op.join(destpath, entry_script))
    extra_ignores = ["*.so"] if not with_so else None
    copy_packages(packages, destpath, extra_ignores=extra_ignores)
    # include locale files if they are built otherwise exit as it will break
    # the localization
    if not check_loc_doc():
        print("Exiting...")
        return
    shutil.copytree(op.join("build", "help"), op.join(destpath, "help"))
    shutil.copytree(op.join("build", "locale"), op.join(destpath, "locale"))
    compileall.compile_dir(destpath)


def package_debian_distribution(distribution):
    app_version = get_module_version("core")
    version = "{}~{}".format(app_version, distribution)
    destpath = op.join("build", "dupeguru-{}".format(version))
    srcpath = op.join(destpath, "src")
    packages = ["hscommon", "core", "qt", "images"]
    copy_files_to_package(srcpath, packages, with_so=False)
    os.mkdir(op.join(destpath, "modules"))
    copy_all(op.join("core", "pe", "modules", "*.*"), op.join(destpath, "modules"))
    copy(
        op.join("qt", "pe", "modules", "block.c"),
        op.join(destpath, "modules", "block_qt.c"),
    )
    copy(
        op.join("pkg", "debian", "build_pe_modules.py"),
        op.join(destpath, "build_pe_modules.py"),
    )
    debdest = op.join(destpath, "debian")
    debskel = op.join("pkg", "debian")
    os.makedirs(debdest)
    debopts = json.load(open(op.join(debskel, "dupeguru.json")))
    for fn in ["copyright", "dirs", "rules", "source"]:
        copy(op.join(debskel, fn), op.join(debdest, fn))
    filereplace(op.join(debskel, "control"), op.join(debdest, "control"), **debopts)
    filereplace(op.join(debskel, "Makefile"), op.join(destpath, "Makefile"), **debopts)
    filereplace(op.join(debskel, "dupeguru.desktop"), op.join(debdest, "dupeguru.desktop"), **debopts)
    changelogpath = op.join("help", "changelog")
    changelog_dest = op.join(debdest, "changelog")
    project_name = debopts["pkgname"]
    from_version = "2.9.2"
    build_debian_changelog(
        changelogpath,
        changelog_dest,
        project_name,
        from_version=from_version,
        distribution=distribution,
    )
    shutil.copy(op.join("images", "dgse_logo_128.png"), srcpath)
    run_checked(["dpkg-buildpackage", "-F", "-us", "-uc"], cwd=destpath)


def package_debian():
    print("Packaging for Debian/Ubuntu")
    for distribution in ["unstable"]:
        package_debian_distribution(distribution)


def package_arch():
    # For now, package_arch() will only copy the source files into build/. It copies less packages
    # than package_debian because there are more python packages available in Arch (so we don't
    # need to include them).
    print("Packaging for Arch")
    srcpath = op.join("build", "dupeguru-arch")
    packages = ["hscommon", "core", "qt", "images"]
    copy_files_to_package(srcpath, packages, with_so=True)
    shutil.copy(op.join("images", "dgse_logo_128.png"), srcpath)
    debopts = json.load(open(op.join("pkg", "arch", "dupeguru.json")))
    filereplace(op.join("pkg", "arch", "dupeguru.desktop"), op.join(srcpath, "dupeguru.desktop"), **debopts)


def package_source_txz():
    print("Creating git archive")
    app_version = get_module_version("core")
    name = "dupeguru-src-{}.tar".format(app_version)
    base_path = os.getcwd()
    build_path = op.join(base_path, "build")
    dest = op.join(build_path, name)
    run_checked(["git", "archive", "--format=tar", "--output", dest, "HEAD"])
    run_checked(["xz", "--force", dest])


def package_windows():
    app_version = get_module_version("core")
    arch = platform.architecture()[0]
    # Information to pass to pyinstaller and NSIS
    match = re.search("[0-9]+.[0-9]+.[0-9]+", app_version)
    version_array = match.group(0).split(".")
    match = re.search("[0-9]+", arch)
    bits = match.group(0)
    # include locale files if they are built otherwise exit as it will break
    # the localization
    if not check_loc_doc():
        print("Exiting...")
        return
    # create version information file from template
    with open("win_version_info.temp", "r", encoding="utf-8") as version_template:
        version_info = version_template.read()
    with open("win_version_info.txt", "w", encoding="utf-8", newline="\n") as version_info_file:
        version_info_file.write(version_info.format(version_array[0], version_array[1], version_array[2], bits))
    # run pyinstaller from here:
    import PyInstaller.__main__

    # UCRT dlls are included if the system has the windows kit installed
    try:
        PyInstaller.__main__.run(
            [
                "--name=dupeguru-neo-win{0}".format(bits),
                "--windowed",
                "--noconfirm",
                "--icon=images/dgse_logo.ico",
                "--add-data={0};locale".format(LOCALE_DIR),
                "--add-data={0};help".format(HELP_DIR),
                *_frozen_notice_pyinstaller_arguments(";"),
                "--collect-data=images",
                "--version-file=win_version_info.txt",
                GUI_ENTRY_SCRIPT,
            ]
        )
        PyInstaller.__main__.run(_windows_cli_pyinstaller_arguments())
    finally:
        os.remove("win_version_info.txt")
    cli_source = op.join("dist", f"{WINDOWS_CLI_BINARY}.exe")
    cli_destination = op.join(
        "dist",
        "dupeguru-neo-win{0}".format(bits),
        f"{WINDOWS_CLI_BINARY}.exe",
    )
    _smoke_windows_cli(cli_source, app_version)
    _stage_windows_cli(cli_source, cli_destination)
    makensis = shutil.which("makensis")
    if makensis is None:
        raise RuntimeError("makensis is required to build the Windows installer")
    run_checked(
        [
            makensis,
            f"/DVERSIONMAJOR={version_array[0]}",
            f"/DVERSIONMINOR={version_array[1]}",
            f"/DVERSIONPATCH={version_array[2]}",
            f"/DBITS={bits}",
            "setup.nsi",
        ]
    )


def package_macos(args):
    # include locale files if they are built otherwise exit as it will break
    # the localization
    if not check_loc_doc():
        print("Exiting")
        return
    # run pyinstaller from here:
    import PyInstaller.__main__

    pyinstaller_args = [
        "--name=dupeguru-neo",
        "--windowed",
        "--noconfirm",
        "--icon=images/dupeguru.icns",
        "--osx-bundle-identifier=io.github.AiWithYou.dupeguru_neo",
        "--add-data={0}:locale".format(LOCALE_DIR),
        "--add-data={0}:help".format(HELP_DIR),
        *_frozen_notice_pyinstaller_arguments(":"),
        "--collect-data=images",
    ]
    if args.sign_identity:
        pyinstaller_args.append(f"--codesign-identity={args.sign_identity}")
    pyinstaller_args.append(GUI_ENTRY_SCRIPT)
    PyInstaller.__main__.run(pyinstaller_args)


def main():
    args = parse_args()
    if args.src_pkg:
        print("Creating source package for dupeGuru Neo")
        package_source_txz()
        return
    print("Packaging dupeGuru Neo with UI qt")
    if sys.platform == "win32":
        package_windows()
    elif sys.platform == "darwin":
        package_macos(args)
    else:
        if not args.arch_pkg:
            distname = distro.id()
        else:
            distname = "arch"
        if distname == "arch":
            package_arch()
        else:
            package_debian()


if __name__ == "__main__":
    main()
