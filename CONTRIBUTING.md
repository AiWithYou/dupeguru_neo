# Contributing to dupeGuru Neo

Thank you for helping improve dupeGuru Neo. This document describes the
project's contribution and public-reporting expectations.

## Things to know before starting

- The canonical repository is
  [AiWithYou/dupeguru_neo](https://github.com/AiWithYou/dupeguru_neo).
- Contributions are distributed under GPLv3. Do not submit code, media, or
  translations that you do not have the right to license on those terms.
- File removal is safety-critical. Read
  [the safety model](docs/SAFETY_MODEL.md) before changing scan evidence,
  folder pools, Copy/Move, quarantine, restore, finalization, or custom-command
  behavior.
- Green Verified Exact evidence is the only relationship that can authorize
  duplicate-removal quarantine, with a complete current scan and an Incoming
  Files target still required. Yellow and blue evidence may support an explicit
  organizer Copy/Move under those scan and pool conditions. External custom
  commands have a separate, unprotected contract.
- Keep generated artifacts, private media, credentials, raw catalogs, action
  journals, and unredacted logs out of commits and public issues.

## Ways to contribute

### Reporting bugs

Search [existing issues](https://github.com/AiWithYou/dupeguru_neo/issues)
before filing a report. Use the bug-report template and include the version or
commit, operating system, filesystem type, scan mode, folder-pool setup, the
smallest reproducible steps, expected behavior, and actual behavior.

Do not attach a raw debug log, crash dump, results file, catalog database,
quarantine journal, action plan, settings directory, private media, or
screenshot containing private paths or metadata. Prefer the built-in redacted
structured diagnostics when available, but still inspect every line before
posting it. Replace user names, host names, volume labels, network locations,
and file paths with stable placeholders while preserving their relationships.

A vulnerability or possible data-loss path must follow
[SECURITY.md](SECURITY.md), not a public issue. In particular, do not publicly
disclose arbitrary deletion, command execution, privilege escalation, or
private-data exposure.

### Suggesting enhancements

Open an [enhancement issue](https://github.com/AiWithYou/dupeguru_neo/issues/new)
that describes the user problem, representative library size, expected
workflow, and safety implications. Distinguish candidate generation, review
evidence, organizer operations, and duplicate-removal authority. A similarity
score alone cannot be proposed as deletion authority.

### Localization

Keep message identifiers and formatting placeholders intact. Build the
localization catalogs and English help after changing translatable source.
Avoid unrelated automatic rewrites of existing translations.

### Code contributions

Follow the development setup in [README.en.md](README.en.md). Build the native
modules, run the focused tests for the affected component, then run:

```console
python -m pytest core hscommon qt/tests
pre-commit run --all-files
python build.py --doc
```

Add regression tests for bug fixes. Safety gates require negative tests that
prove stale, incomplete, protected, approximate, malformed, and
resource-limited inputs fail closed.

### Pull requests

Please follow these steps:

1. Keep the pull request focused on one feature or bug.
2. Explain the user-visible change, safety impact, compatibility impact, and
   tests performed.
3. Follow the [style guides](#style-guides).
4. Confirm that all
   [status checks](https://docs.github.com/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/troubleshooting-required-status-checks)
   pass. If a failure appears unrelated, document the failing job and your
   evidence instead of ignoring it.

Maintainers may request additional design work, tests, documentation, or
compatibility changes before acceptance.

## Style Guides

### Git Commit Messages

- Use the present tense ("Add feature" not "Added feature")
- Use the imperative mood ("Move cursor to..." not "Moves cursor to...")
- Limit the first line to 72 characters or less
- Reference issues and pull requests liberally after the first line

### Python Style Guide

- All files are formatted with [Black](https://github.com/psf/black)
- Follow [PEP 8](https://peps.python.org/pep-0008/) as much as practical
- Pass [flake8](https://flake8.pycqa.org/en/latest/) linting
- Include [PEP 484](https://peps.python.org/pep-0484/) type hints (new code)

### Documentation Style Guide

- Use plain, testable language and distinguish guarantees from limitations.
- Use the product terms **Incoming Files**, **Protected Library**,
  **Compare Only**, **Excluded**, **Verified Exact**, **Copy/Move**, and
  **quarantine** consistently.
- Do not call a hash, percentage, visual relation, folder aggregate, scan
  snapshot, or persisted verification ID deletion authority. Folder aggregates
  also cannot authorize program-managed organizer Copy/Move.
- Keep examples non-destructive and avoid private paths or real personal data.
- Use HTTPS links to current, authoritative sources and run the English Sphinx
  build before submitting.

## Additional Notes

### Issue and Pull Request Labels

This section lists and describes the labels used with issues and pull requests.

#### Issue Type and Status

| Label name | Search | Description |
|------------|--------|-------------|
| `enhancement` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3Aenhancement) | Feature requests and enhancements. |
| `bug` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3Abug) | Bug reports. |
| `duplicate` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3Aduplicate) | Issue is a duplicate of an existing issue. |
| `needs-reproduction` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3Aneeds-reproduction) | A report that has not yet been reproduced. |
| `needs-information` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3Aneeds-information) | More information is required. |
| `blocked` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3Ablocked) | Work is blocked by another issue or dependency. |
| `beginner` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3Abeginner) | A smaller issue suitable for a first contribution. |

#### Category Labels

| Label name | Search | Description |
|------------|--------|-------------|
| `3rd party` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3A%223rd%20party%22) | Related to a third-party dependency. |
| `crash` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3Acrash) | Related to an unexpected process termination or unhandled exception. |
| `documentation` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3Adocumentation) | Related to documentation. |
| `linux` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3Alinux) | Related to running on Linux. |
| `mac` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3Amac) | Related to running on macOS. |
| `performance` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3Aperformance) | Related to performance or resource use. |
| `ui` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3Aui) | Related to visual or interaction design. |
| `windows` | [search](https://github.com/AiWithYou/dupeguru_neo/issues?q=is%3Aopen+is%3Aissue+label%3Awindows) | Related to running on Windows. |

#### Pull Request Labels

Pull-request labels may be added as the contribution volume grows.
