# Building dupeGuru Neo on Windows

These instructions target Windows 11, PowerShell 7, and a non-administrator
account. The desktop application does not require administrator privileges.

## Prerequisites

- 64-bit CPython 3.10–3.14
- Visual Studio 2022 Build Tools with the Desktop C++ workload and Windows SDK
- Git
- NSIS 3 only when building the native installer

PyQt6 and Pillow are installed into the project virtual environment. Do not
install PyQt globally.

## Development build

```powershell
git clone https://github.com/AiWithYou/dupeguru_neo.git
Set-Location dupeguru_neo
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[test,build]"
python build.py --modules
python run.py --self-test
python run.py
```

`--self-test` uses Qt's offscreen platform to verify packaged images and the Qt
runtime without opening a user window or catalog.

## Tests

```powershell
python -m pytest core hscommon qt\tests
python -m black --check .
python -m flake8 .
```

## Package

```powershell
python build.py --clean
python package.py
```

The PyInstaller portable bundle can be built locally without a signing
certificate. That bundle is not Authenticode-signed and may trigger
SmartScreen. CI smoke-tests it but does not upload it or publish it as an
official release asset. Publishing a native release additionally requires the
complete native source/license/SBOM gate described in `docs/RELEASE.md`, plus
the project's Windows code-signing certificate and post-signature
verification.

The local NSIS installer and its frozen application both retain `LICENSE`,
`THIRD_PARTY_NOTICES.md`, the hscommon BSD text, `requirements-release.txt`,
`release-sources.json`, and `PORTABLE-NOTICE.txt`. These files must remain with
any redistributed binary. A local installer is still not a supported release;
the tagged workflow publishes only the Python packages and exact tagged source
allowed by `docs/RELEASE.md`.

The generated catalog and quarantine journals are user data. Do not bundle,
publish, or attach them to bug reports. Crash/support bundles are opt-in and
must be reviewed before sharing.
