# dupeGuru Neo repository instructions

## Windows EXE after every update

- After changing any tracked repository file, finish the relevant tests, commit
  only the task's files, and integrate through the protected `main` pull-request
  workflow.
- Wait for `Desktop package / windows` on the resulting `main` commit. Confirm
  that the build, `--version`, `--self-test`, PE, dependency, license, and
  artifact generation all succeeded.
- The full `scripts/desktop_bundle.py verify` checks installed-distribution
  provenance and is intentionally bound to the Python environment that built
  the artifact. Treat its successful exact-commit CI step as the evidence for
  that check; do not re-run it against a downloaded artifact from a different
  Python installation.
- Download the Windows artifact for that exact commit when a local handoff is
  required. Confirm the exact source commit in `README-WINDOWS.txt`, calculate
  the EXE's SHA-256, fail if it differs from the `.exe.sha256` sidecar, and
  re-run the downloaded EXE's `--version` and offscreen `--self-test`. Report
  the commit, EXE path or artifact link, actual SHA-256, and verification
  result.
- Treat the current `origin/main` commit as the only latest development
  artifact generation. After its complete CI succeeds, delete Actions
  artifacts from every other commit. Keep workflow-run history as provenance,
  let concurrency cancel superseded runs, and automatically delete merged
  topic branches. After verifying a replacement local handoff, retain only its
  exact-main directory and remove older generated outputs.
- When `core.__version__` changes, wait for `Permanent desktop release / Publish
  verified Windows and macOS desktop release` on that exact `main` commit.
  Confirm that the public `desktop-<version>` pre-release tag resolves to the
  commit and contains exactly the Windows EXE, macOS APP ZIP, their SHA-256
  sidecars, and both source receipts. A same-version source change remains a
  development build; publishing changed bytes requires another version bump.
- After independently verifying a replacement permanent desktop pre-release,
  keep only that latest public desktop Release asset set. Remove older
  `desktop-*` GitHub Releases without deleting their lightweight source tags,
  workflow history, or Git history.
- A local desktop build must use CPython 3.13.14, the pinned tools in
  `.github/workflows/default.yml`, a clean committed worktree, and a
  `SOURCE_DATE_EPOCH` equal to the commit timestamp. Build and verify the
  portable bundle before `scripts/desktop_bundle.py build`, then run
  `scripts/desktop_bundle.py verify` on the resulting EXE.
- `portable-build/`, `portable-dist/`, `desktop-build/`, `desktop-dist/`, the
  EXE, and its sidecars are generated outputs. Never commit them. Never claim
  that an EXE was produced or verified without the successful command output.
