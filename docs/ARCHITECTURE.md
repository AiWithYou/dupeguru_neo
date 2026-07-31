# dupeGuru Neo architecture

This document describes the trust boundaries between candidate generation,
evidence, review, and file mutation. It is deliberately narrower than a product
roadmap: only shipped source-tree components are described.

## Data flow

```text
safe filesystem walk
  ├─ coverage receipt
  └─ stable file identity + generation
          │
          ├─ bounded GUI/direct scan
          │     ├─ exact: size ─► candidate-only staged hashes ─► byte compare
          │     ├─ image pHash MultiIndex ─► dHash/color filter ─► visual refinement
          │     ├─ dataset bundle planner ─► sidecar/leakage/keeper policy
          │     └─ video metadata/frames/audio ─► bounded sequence alignment
          │                                  │
          │                                  ▼
          │                       typed evidence + scan receipt
          │                                  │
          │                     ┌───────────┴───────────┐
          │                     ▼                       ▼
          │              Qt review surface          JSON/JSONL direct CLI
          │                     │
          │          ┌──────────────┴───────────────────┐
          │          ▼                                  ▼
          │  green exact removal plan          explicit organizer Copy/Move
          │          │                       (complete current Incoming only)
          │  live SHA-256 + byte proof                   │
          │          │                         no-replace publication
          │  quarantine journal/executor
          │          │
          │  restore OR explicit finalization
          │
          └─ explicit `dupeguru catalog` CLI
                └─ local catalog (paths, content versions, resumable work)
                            └─ validated catalog JSONL reports
```

## Boundaries

### Enumeration

`core.safe_walk` does not follow symbolic links or reparse points and records
every skip or error. `core.file_identity` supplies platform-native physical
identity when available. A path is never treated as an identity substitute for
rename reuse or destructive authorization.

`core.directories` maps selected roots to four user-visible pools:

- Incoming Files: normal review candidates and the only possible targets for
  program-managed quarantine or organizer operations.
- Protected Library: may be selected as keepers, never targets.
- Compare Only: participates in matching, never becomes a target.
- Excluded: intentionally pruned and recorded as such.

The optional cross-pool scope ignores matches whose complete membership is
inside one pool.

### Exact evidence

`core.fs` computes bounded candidate hashes through stable file handles.
`core.engine.getgroups_by_contents` computes the full digest for surviving
candidates, byte-compares every group member to a representative, and creates a
linear-space `Group.from_exact_files`. Its compatibility match view generates
pairs lazily and does not store `k(k-1)/2` objects.

The desktop byte-exact **Contents** scan invokes this direct path after bounded
filesystem discovery. It does not route through `core.catalog` and therefore
creates no catalog scan snapshots or resumable-work history. The normal hash
cache remains an accelerator for the candidate files that were actually hashed.

Hash equality alone is not exact evidence. A direct Contents scan binds strict
hash reads to each file's in-memory `FileSnapshot`, then establishes each
equivalence class with stable representative-to-member byte comparisons. The
scanner publishes policy-filtered subsets of those classes as typed
`verified_exact` groups without retaining comparison objects for omitted
members. File-action authority instead comes from each published file's
scan-bound SHA-256 review snapshot plus live proof at execution time; neither
form is persisted as action authority.

Catalog projection has a distinct representation. It reopens paths, validates
the cataloged content generations, performs the same final byte comparisons,
and stores one `verification_records` identifier per
representative-to-member edge. A verification ID names a persisted comparison
between two content versions; it is not an opened-handle snapshot. In both
representations, persisted scan evidence is not mutation authority. The direct
path's action executor builds a new proof immediately before quarantine;
catalog output remains report-only.

### Approximate media evidence

`core.pe.image_features` decodes the first image frame, applies EXIF
orientation, converts a valid embedded ICC profile to sRGB, composites alpha
onto a defined white background, and emits deterministic pHash, dHash, fixed
color histogram, bounded tile fingerprints, bounded thumbnail-identity
metadata, quality metadata, and
block features. `core.pe.candidate_index` retrieves all fingerprints within the
configured Hamming radius; the dHash/color stage conservatively filters and
ranks candidates before `core.pe.matchblock` performs the detailed block
comparison. A tile hit is labeled `crop_candidate`, never a spatially verified
crop. Results remain approximate even at the maximum score.

`core.video` executes ffprobe, FFmpeg, and fpcalc as argument arrays without a
shell and enforces process, time, output, frame, and fingerprint limits.
Normalized-time and scene-change frame hashes are locally aligned; audio
fingerprints are a secondary signal. Perceptual video relations never become
`exact` without an exact-engine `ByteExactProof`.

`core.engine.getgroups_by_folders` groups equal recursive manifest digests in
linear space. A folder manifest is aggregate evidence, not one file's byte
stream, so folder groups remain gray and unverified. They cannot enter either
duplicate-removal quarantine or program-managed organizer Copy/Move. An
External Command is a separate, explicitly confirmed trust boundary.

### Explicit catalog CLI

`core.catalog` is a local SQLite WAL database. It records volumes, roots,
physical files, paths, content generations, artifacts, scans, directory
coverage, resumable work leases, verification records, and action journals.
`core.catalog_indexer` projects no-follow walk events into that state and uses
keyset pages instead of loading an entire library into memory.

This durable state is created or updated only by an explicitly invoked
`dupeguru catalog` CLI workflow. A GUI byte-exact **Contents** scan neither
opens it as its matching path nor appends scan history to it.

Catalog artifacts are accelerators. Before reuse, their content generation and
algorithm policy must match the current file. Catalog exact projection
re-establishes byte equality against live handles before it publishes a report;
that report does not itself grant mutation authority.

Generation is a typed platform primitive rather than an mtime alias. POSIX
binds inode ctime to the opened device/inode. Windows binds the file's current
USN to the current volume USN-journal identifier while a no-follow handle
denies write sharing. A regular file never accepts metadata `ChangeTime` as a
content-generation fallback. Because directory membership changes do not
reliably advance the directory object's USN, directory tokens additionally
bind the root handle's stable `ChangeTime` and a twice-stable recursive tree
digest to the mandatory journal and object USN values. Every descendant record
uses an exact UTF-16 name, high-confidence 128-bit file identity, metadata and
link count, plus handle-stable journal/file USN and `ChangeTime`; reparse entries
are rejected. Each pass walks the subtree once and is bounded to 1,000,000
entries, 256 MiB of names, 512 MiB of record metadata, depth 256, and 300
seconds as checked between entries and at directory boundaries. The deadline
cannot preempt a single blocking filesystem call; the filesystem or transport
owns that lower-level timeout. A volume or share without the required Windows
USN controls, or an unstable, unsupported, or over-budget tree observation,
produces incomplete evidence instead of a timestamp fallback.

Safe directory Copy/Move uses recursive tokens at the operation root and at its
terminal verification boundary. Nested traversal snapshots are shallow because
the terminal root token covers all descendants; this keeps each full proof to
two linear tree passes instead of recursively re-walking every subtree.

The catalog's `scan_snapshots` table records complete enumeration coverage for
a `scan_id`; it does not prove content equality. Exact projection records in
`verification_records` bind two content versions, an algorithm/version, full
digest, comparison time, and state. Read-write projection may record or refresh
an ID after a successful live comparison. Read-only projection performs the
comparison too but requires the corresponding positive ID to exist already.
Neither a scan snapshot nor a verification ID grants mutation capability.

### Dataset plans

`core.dataset_service` treats a primary image and supported `.txt`,
`.caption`, or `.json` files as one bundle. It validates sidecar encoding and
JSON structure, rejects ambiguous/orphan associations, keeps a similarity
cluster in one deterministic train/validation/test split, and records
explainable keeper scores.

Only a verified-exact bundle with byte-equal corresponding sidecars can receive
a quarantine action. Near, transformed, and related clusters are split and
reviewed as a unit but are not deletion-authorized. Their complete, current
Incoming Files members may still be copied or moved by an explicit organizer
plan; that plan does not assert equality.

`core.dataset_executor` commits the complete primary-plus-sidecar plan as one
journaled transaction. Permanent finalization never unlinks a quarantine path
directly: it first atomically isolates the verified inode under a random,
same-directory tombstone, rechecks the held target and keeper handles, records
the tombstoned state, and only then purges. On Windows, both the no-replace
rename and final delete disposition are bound to identity-checked handles for
the verified object, with no path-operation fallback. Cross-volume rollback
uses the same protocol for identities recorded when temporary and destination
files were created. Replay completes an interrupted tombstone sequence;
ambiguous two-name, missing-intent, or replacement states remain untouched for
review.

All executor documents and journals are stored below the canonical reserved
`.dupeguru-neo-dataset-executor` child. A configured state path is a base
directory whose reserved child is derived by the executor; the default base is
the dataset destination. Discovery prunes this namespace, every
user-controlled dataset path is rejected if it enters any program-owned state
namespace, and the embedded state path in an operation document must match the
physical namespace from which it was read.

### Actions

`core.action_plan` and `core.services` serialize immutable, versioned plans.
The executor does not trust the scan digest as an action proof: it reopens the
target and keeper without following links, validates identity and generation,
computes SHA-256, and byte-compares them.

`core.safe_action` stages a target into a same-volume quarantine and appends
fsynced journal records. `core.quarantine` preflights a complete batch before
the first mutation and rolls back a failed batch. Restore never overwrites an
existing path. Permanent deletion is a separate, second revalidation step.

The action boundary has three contracts:

1. Duplicate-removal quarantine accepts only green, live
   `verified_exact` evidence with complete current coverage and an Incoming
   Files target.
2. Explicit organizer Copy/Move may accept green, yellow approximate, or blue
   related evidence, but only from a complete current scan and only for an
   Incoming Files target. Copy retains the source; Move preserves the payload
   at the destination while removing the source name. Neither operation
   creates deletion authority. Gray aggregate Folder-manifest results do not
   enter this executor because they have neither exact nor typed similarity
   evidence. The selected member and keeper are bound to scan-time physical
   identities and generation tokens; the UI boundary, worker boundary, and
   final no-replace source operation all fail closed on drift.
3. A user-configured external command is outside both executors. Its separate
   confirmation does not import evidence, pool, quarantine, or recovery
   guarantees, and the external program may delete its arguments.

## Failure semantics

Unknown, stale, incomplete, cancelled, unsupported, or resource-limited states
are values, not log-only warnings. They propagate into scan receipts and
machine-readable reports. The duplicate-removal eligibility evaluator accepts
only a current, live `verified_exact` result with a valid incoming target and
keeper. The organizer evaluator independently requires a complete current scan
and a current Incoming Files target; it does not require approximate evidence
to become exact.

The implementation cannot guarantee correctness against a compromised kernel,
privileged concurrent writer, falsified storage hardware, or retention policy
that was never supplied to the application. These limits are described in
`SAFETY_MODEL.md`.

## Extension points

- New candidate indexes may reduce work but must preserve a separate final
  evidence stage.
- Semantic embeddings may add a blue `related` provider but cannot acquire
  destructive capability.
- Filesystem change journals may enqueue catalog work, while full no-follow
  reconciliation remains the coverage authority.
- New UI or web clients should consume the same versioned service models and
  action executor instead of implementing file removal independently.
