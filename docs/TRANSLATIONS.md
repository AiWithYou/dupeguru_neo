# Translation source synchronization

Transifex source upload is an optional repository integration. The
`Transifex Sync` workflow runs only after a push to `main` changes a
`locale/*.pot` source catalog; it does not run for pull requests.

When the repository secret `TX_TOKEN` is absent, the job succeeds with an
explicit GitHub notice and step-summary entry. It does not check out the
repository, download the Transifex client, or upload data.

When `TX_TOKEN` is configured, the workflow downloads the fixed Transifex
client release over HTTPS, verifies the pinned archive and executable SHA-256
digests, checks the executable type, path, and version, and only then supplies
the token to the final source-upload step. The configuration gate receives only
a boolean indicating whether the secret exists; the checkout, gate shell, and
client-verification steps never receive the token itself.

## Local catalog updates

Run `python build.py --updatepot` after adding or removing `tr()` messages.
`python build.py --mergepot` merges every source template into every language,
so a change intentionally limited to one language must merge only that
language's PO files. Japanese catalogs are a CI-complete locale: their active
message set must exactly match each POT, every entry must be translated and
non-fuzzy, and all format placeholders must be preserved. Verify catalog work
with `python build.py --loc` and
`python -m pytest qt/tests/japanese_localization_test.py`. Compiled `.mo` files
are generated artifacts and are not edited or committed.
