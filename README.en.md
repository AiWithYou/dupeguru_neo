# dupeGuru Neo

[**日本語版 README（GitHub既定）**](README.md) | English

[GitHub Releases](https://github.com/AiWithYou/dupeguru_neo/releases) |
[Latest Windows / macOS development build](https://github.com/AiWithYou/dupeguru_neo/actions/workflows/default.yml?query=branch%3Amaster+event%3Apush)

dupeGuru Neo is a safety-first duplicate detector and large media-library
organizer for Windows, macOS, and Linux. It retains dupeGuru's mature Python
core and Qt desktop workflow while making the evidence behind every result and
every file action explicit.

The central rule is simple: a fast or perceptual fingerprint may find
candidates, but it can never authorize deletion.

## What is different in Neo

- **Verified Exact engine.** Files are bucketed by size and optional sample
  hashes, streamed through a full-content hash, and finally compared byte for
  byte. Exact groups are represented in linear space instead of materializing
  every pair.
- **Recoverable actions by default.** The desktop app and CLI re-open both the
  target and keeper without following links, validate identity and SHA-256
  proofs, then move the target into a same-volume quarantine with a durable
  journal. Restore and permanent finalization are separate operations.
- **Coverage-aware scans.** Unreadable, changing, skipped, cancelled, or
  resource-limited input produces an incomplete receipt. Incomplete evidence
  does not silently acquire destructive capability.
- **Persistent library catalog.** A local SQLite catalog tracks stable file
  identities, paths, content generations, derived exact artifacts, immutable
  scan history, and resumable work. Image features, thumbnails, video
  fingerprints, and file-action recovery journals are separate durable stores
  with independent validation and lifecycle rules. Native identities preserve
  move history, but a renamed file is analyzed again unless a trusted
  filesystem event journal proves that its content did not change.
- **Indexed image similarity.** EXIF orientation, ICC color, and alpha are
  normalized before a deterministic perceptual hash narrows candidates.
  Existing 15×15 block comparison remains the final visual test. Visual
  similarity is always reported separately from byte equality.
- **Media and dataset foundations.** The core includes explainable keeper
  scoring, image-plus-sidecar dataset plans, leakage-safe cluster splits, and
  FFmpeg/Chromaprint-based video fingerprinting with explicit capability and
  partial-result reporting.
- **Automation surface.** `dupeguru` is a Qt-free, versioned JSON/JSONL CLI.
  Source-library action plans require an explicit `--execute`; validation is
  the default. Read-only analyzers may still write an explicitly selected
  cache or report destination.

The detailed guarantee and its limits are documented in
[docs/SAFETY_MODEL.md](docs/SAFETY_MODEL.md).

## Safety labels

| Label | Meaning | Duplicate removal / quarantine | Organizer copy / move |
| --- | --- | --- | --- |
| Green / `verified_exact` | Stable full-file proof plus final byte comparison | Eligible after live revalidation | Eligible under the destination and pool policy |
| Yellow / `similar` | Decoded media is perceptually similar | Never eligible | Explicit operation only after a complete, current scan; Copy and Move are limited to Incoming Files |
| Blue / `related` | Visually related below the similarity threshold, or temporally related media; not semantic without embedding evidence | Never eligible | Explicit operation only after a complete, current scan; Copy and Move are limited to Incoming Files |
| Gray / incomplete | Coverage or evidence is missing, stale, or failed | Refused | Refused |

An organizer move changes a source path, but it does not claim that the source
is a duplicate. Copy and Move both require a scan-bound source generation,
current Incoming Files policy, and no-replace destination publication; neither
silently becomes quarantine, finalization, or deletion. Gray aggregate Folder
results cannot enter this path. External commands form another explicit trust
boundary and receive no duplicate-removal guarantee.

Byte equality describes the ordinary file payload. ACLs, extended attributes,
alternate streams, resource forks, backup-retention rules, and legal-hold
policy are separate concerns; see the safety model before automating removal.

## Requirements

- CPython 3.10–3.14
- PyQt6 6.11 for the desktop application
- Pillow 12 for image analysis
- A supported C compiler when installing from source; the image comparison
  modules are native extensions
- FFmpeg/ffprobe for video analysis; `fpcalc` for audio fingerprints

The release workflow tests the source tree on every supported Python version
and operating system. A tagged release builds one canonical sdist, then uses
that same sdist to build and byte-for-byte reproduce three native wheels with
CPython 3.13.14: Linux x86_64, Windows x86_64, and macOS arm64. Those are the
only published wheel targets. Other supported Python versions or architectures
install from the sdist and therefore need a compiler.

The packaged Windows desktop application supports 64-bit Windows 10 and
Windows 11.

The live catalog must be stored on a local filesystem. Libraries on a NAS are
supported only at the capability level reported by that filesystem, and
SQLite WAL files must not be placed on the share. On Windows, every
evidence-producing file observation additionally requires the source volume
to expose a usable USN change journal. Filesystems or shares that do not
support the required USN controls fail closed as incomplete; there is no
timestamp fallback.

Version 5 stores its exact-hash cache in `hash_cache_v3.sqlite3`. The older
`hash_cache.db` is deliberately not opened, imported, renamed, deleted, or
overwritten. It remains in the application-data directory for manual recovery
or removal, and the first Version 5 scan recalculates exact hashes into the new
owned cache.

## Easy-launch desktop builds

The current source version is **5.2.0**. Permanently published packages are
listed on [GitHub Releases](https://github.com/AiWithYou/dupeguru_neo/releases).
That list also retains older development builds, so check the version shown on
the release page before downloading. The latest development build containing
the current source changes is generated after each successful `master` CI run
and retained as checked desktop artifacts for seven days:

- **Windows:** download `dupeguru-neo-windows-exe-<commit>`, expand the GitHub
  artifact once, and double-click the versioned `.exe`. It is a single GUI file;
  Python and a separate support folder are not required.
- **macOS:** download `dupeguru-neo-macos-app-<commit>`, expand the GitHub
  artifact, then expand the included `.app.zip`. Move `dupeguru-neo.app` to
  Applications and open it. The inner ZIP preserves executable permissions,
  framework symlinks, and the application bundle.

Use the Artifacts section of the
[latest successful master push CI run](https://github.com/AiWithYou/dupeguru_neo/actions/workflows/default.yml?query=branch%3Amaster+event%3Apush).
Each CI artifact includes the same kind of checksum, source link, and trust
warning. A GitHub login is required to download Actions artifacts. This is a
short-retention developer convenience rather than a stable release channel.
The EXE is not Authenticode-signed; the APP is ad-hoc signed and is not
Apple-notarized, so SmartScreen or Gatekeeper may show a warning.

## Install and run from source

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[test,build]"
python build.py --modules
dupeguru-gui
```

macOS or Linux:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test,build]'
python build.py --modules
dupeguru-gui
```

Packaged image resources are ordinary Python package data; Qt 5, `pyrcc5`, and
system-wide PyQt installations are not used.

## CLI quick start

Scan two roots and keep the versioned JSONL report:

```sh
dupeguru scan Pictures Archive --format jsonl > scan.jsonl
```

Direct exact scans are bounded during discovery and verification. Defaults are
1,000,000 files, 100,000 issues, 250,000 verified groups, and four hours;
`--max-files`, `--max-issues`, `--max-groups`, and `--max-seconds`
make each budget explicit. Reaching a limit emits a valid incomplete report
and a partial-result exit status. Such a report can be reviewed, but it cannot
be converted into a file-action plan.

Create a recoverable plan:

```sh
dupeguru plan scan.jsonl --operation quarantine > plan.jsonl
```

Validate it without changing files:

```sh
dupeguru apply plan.jsonl --dry-run
```

Execute the already reviewed plan:

```sh
dupeguru apply plan.jsonl --execute
```

Inspect and recover staged operations:

```sh
dupeguru quarantine list Pictures Archive
dupeguru quarantine restore path/to/operation-plan.json --dry-run
dupeguru quarantine restore path/to/operation-plan.json --execute
```

Permanent removal is never part of an exact plan or `apply --execute`.
After reviewing the staged file, finalize that one persisted operation
explicitly:

```sh
dupeguru quarantine finalize path/to/operation-plan.json --dry-run
dupeguru quarantine finalize path/to/operation-plan.json --execute
```

Maintain and query a durable local catalog:

```sh
dupeguru catalog scan catalog.sqlite3 Pictures Archive
dupeguru catalog groups catalog.sqlite3 --page-size 500 > exact-groups.jsonl
dupeguru catalog changes catalog.sqlite3 --from 12 --to 13 > changes.jsonl
dupeguru catalog backup catalog.sqlite3 catalog-backup.sqlite3
```

The two scan IDs for `catalog changes` must name complete immutable snapshots
with the same root set. Catalog reports are evidence and never execute file
actions. Change records use the `dupeguru.catalog-change-record` schema at
version 2. Without trusted event-journal evidence, a one-to-one stable native
identity observed at two paths is reported as a `relocation_candidate`, not a
proven `moved` event. The candidate classification states whether continuity
comes from one catalog content generation or matching canonical full SHA-256
artifacts; neither grants destructive authority.

Catalog exact groups use the reconstructable
`dupeguru.catalog-group-record-v2` JSONL contract: `header`, then
`group_header` + one or more byte-bounded `member_chunk` records + `group_end`
per group, and a final `summary`. Catalog output is staged and fully validated
before publication. Its limits are 8 MiB per strict UTF-8 physical line
(newline included), 40,000 members per structural chunk, 2 GiB total, and
4,000,000 records; group output allows at most 1,000,000 groups and 1,000,000
members per group, while change output allows at most 3,999,998 changes.
Aggregate projection counts reject oversize groups before their rows are
loaded, and accepted SQL pages have explicit 1,000,000-row and
1,000,000-member caps. A pre-publication limit, encoding, temporary storage,
schema, ordering, or count failure leaves standard output empty. A failure in
the final standard-output copy is the sole case that can leave a validated
prefix and returns a failed status. Normal process output is copied through
the binary stream so Windows code pages and newline translation cannot alter
the validated UTF-8/LF bytes.

`catalog groups` is a read-only live projection. If its byte comparison finds
that a stored digest bucket no longer describes the current bytes, it returns a
structured repair-required error and publishes no partial group stream. Run
`dupeguru catalog scan` again with the same database and roots: the writable
scan command retires every derived artifact and old work lease for that content
generation, creates fresh generations, reruns all configured analysis stages,
and performs one bounded verification retry. A second mismatch still fails
closed instead of looping.

Inspect the read-only video workflow or prepare an image dataset plan:

```sh
dupeguru visual scan Pictures --cache ~/.cache/dupeguru/visual.sqlite3 --max-images 250000
dupeguru visual query reference.png Pictures --max-candidate-pairs 250000
dupeguru video capabilities
dupeguru video scan Videos --max-files 10000 --format jsonl > video-groups.jsonl
dupeguru dataset prepare-root Incoming --destination-root Organized
```

Dataset recovery metadata is always isolated below the reserved
`.dupeguru-neo-dataset-executor` directory and is pruned from later scans.
When supplied, `--state-root` names a base directory; the executor uses its
reserved child rather than treating arbitrary files in that base as state.

Visual reports contain only `similar` and `related` evidence and never grant a
destructive capability. Their file, candidate, match, decode-pixel, and time
limits are explicit CLI options; a reached limit produces a valid partial
report and nonzero partial-result exit status. A persistent visual cache must
be outside every scanned root.

Every machine-readable document carries a schema name and version. Use
`dupeguru schema --help` and `dupeguru doctor` to inspect the installed
contracts and local capabilities.

Exact report and plan input is bounded: one JSON document is at most 64 MiB,
while JSONL permits at most 8 MiB per physical line, 2 GiB total, 1,100,000
physical lines, and 1,000,000 records. Scan reports additionally permit at
most 250,000 groups and 1,000,000 total group file records; deletion plans
permit at most 250,000 actions. Exact scans and plan creation therefore emit
JSONL by default. Both JSON and JSONL output are fully preflighted against
these same loader limits before the first byte is written, so an over-limit
failure cannot leave a partial report on standard output. Use
`--format json` only when the complete document fits the single-document
limit. Video-library reports use the same JSON/JSONL output limits. Other
single-JSON service outputs—including apply, query, doctor, quarantine, and
schema reports—use the same 64 MiB and structural preflight before their first
byte is written. Dataset prepare-input and plan files accept
strict JSON only and are capped at 128 MiB for both files and standard input,
with at most 250,000 actions and 250,000 file records. JSON/CSV plan exports
are streamed with a 128 MiB publication cap. These are interchange limits:
one crash-recoverable dataset apply transaction accepts at most 10,000 file
records. Plans above that execution limit must be split; unusually long paths
can lower the practical transaction size because the full recovery journal is
reserved before mutation. Limits count UTF-8 bytes, and
over-limit input or output fails without
publishing or replacing a destination and before any dataset mutation. Raw
CSV preserves untrusted paths and IDs and must not be opened as a spreadsheet;
API callers can explicitly request the non-lossless `spreadsheet_safe=True`
view. Large exact reports should use JSONL. See
[the automation guide](help/en/automation.rst) for every default cap and its
exported API constant.

## Development and verification

```sh
python -m pytest core hscommon qt/tests
python -m black --check .
python -m flake8 .
python build.py --modules
python run.py --self-test
```

Release artifacts are built from immutable tags, installed in clean
environments, checked for dependency consistency, inventoried with SHA-256, and
accompanied by GitHub attestations plus per-file Sigstore bundles bound to the
tagged workflow identity and GitHub Actions OIDC issuer. The aggregate
CycloneDX SBOM unions installed runtime dependency snapshots from Linux,
Windows, and macOS, so Windows-only `pywin32` is not lost when metadata is
assembled on Linux.

The release is verified by `SHA256SUMS`. `requirements-release.txt` pins exact
versions but is not a pip `--require-hashes` lock: installed `RECORD` files and
independent installed-file manifests provide post-install provenance, not
hash-before-install authentication of package-index downloads.

Portable builds remain available for local development and are smoke-tested on
all three operating systems. The official `v*` tagged-release workflow uploads
the checked Windows EXE and macOS APP only as short-retention CI artifacts and
excludes them from its signed official payload. For easier installation, one
exact, already-verified pair may also be mirrored in a separately named
`desktop-*` development pre-release. That mirror remains unsigned/ad-hoc and
does not become an official release asset of the signed stable `v*` channel.
Binary wheels can embed native codec, rendering, and runtime libraries below
the Python-distribution level; the current source lock, license inventory, and
SBOM do not yet prove that complete native component closure. The exact
official-release allowlist rejects non-contract assets, and an independent
bounded archive scanner rejects portable or source-companion content even when
it is renamed or nested inside an otherwise allowed archive. See
[docs/RELEASE.md](docs/RELEASE.md).

## Source layout

- `core/`: evidence, scanners, catalog, quarantine, services, dataset, and video
- `qt/`: PyQt6 desktop interface
- `images/`: packaged UI assets
- `help/`: Sphinx user manual
- `scripts/`: CI, artifact smoke tests, and release metadata
- `pkg/`: native packaging skeletons

## License and provenance

dupeGuru Neo is distributed under GPLv3. If you distribute a modified binary,
the corresponding source and GPL notices must be available under the license
terms. The project preserves the history and attribution of the original
dupeGuru contributors; Neo-specific maintenance is hosted at
[AiWithYou/dupeguru_neo](https://github.com/AiWithYou/dupeguru_neo).
