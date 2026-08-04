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
- A local desktop build must use CPython 3.13.14, the pinned tools in
  `.github/workflows/default.yml`, a clean committed worktree, and a
  `SOURCE_DATE_EPOCH` equal to the commit timestamp. Build and verify the
  portable bundle before `scripts/desktop_bundle.py build`, then run
  `scripts/desktop_bundle.py verify` on the resulting EXE.
- `portable-build/`, `portable-dist/`, `desktop-build/`, `desktop-dist/`, the
  EXE, and its sidecars are generated outputs. Never commit them. Never claim
  that an EXE was produced or verified without the successful command output.
