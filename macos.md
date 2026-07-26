# Building dupeGuru Neo on macOS

## Prerequisites

- CPython 3.10–3.14 (a current python.org, Homebrew, or pyenv build)
- Xcode command-line tools
- Git

PyQt6 wheels include the required Qt runtime. A Homebrew Qt 5 installation is
not used.

## Development build

```sh
git clone https://github.com/AiWithYou/dupeguru_neo.git
cd dupeguru_neo
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test,build]'
python build.py --modules
python run.py --self-test
python run.py
```

## Tests

```sh
python -m pytest core hscommon qt/tests
python -m black --check .
python -m flake8 .
```

## Application bundle

```sh
python build.py --clean
python package.py
```

The resulting portable `.app` can be inspected and tested locally. Distribution
as a Gatekeeper-trusted application additionally requires a Developer ID
Application certificate, hardened-runtime signing, Apple notarization, and
stapling. CI smoke-tests the local bundle but does not upload it or publish it
as an official release asset. A future native release also requires the
complete native source, license, and SBOM gate in `docs/RELEASE.md`.

Apple resource forks, extended attributes, ACLs, and Finder metadata are not
part of the ordinary payload-equality proof. Review
`docs/SAFETY_MODEL.md` before using automated actions on archival libraries.
