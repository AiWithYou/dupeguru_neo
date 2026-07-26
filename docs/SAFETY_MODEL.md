# dupeGuru Neo safety model

## Scope of the guarantee

dupeGuru Neo distinguishes three independent claims:

1. **Scan coverage**: which roots and directory subtrees were enumerated
   completely, partially, or not at all.
2. **Content evidence**: what relationship was established between two stable
   file snapshots.
3. **Action evidence**: whether the objects being changed are still the objects
   that were verified, and whether a verified survivor or recovery copy remains.

The product may call an item `VERIFIED_PAYLOAD_EXACT` only when:

- both inputs were opened without following a link;
- their identities and metadata were stable before and after reading;
- a full-file digest bucketed them as candidates;
- a streaming byte comparison reached EOF on both inputs without a mismatch;
- no read, stat, cache, cancellation, or resource-limit error occurred.

Sample hashes only reject candidates. A percentage, perceptual score, decoded
pixel digest, metadata match, or semantic embedding is not byte-exact evidence.
If members of one full-digest bucket disagree during final byte comparison, the
entire bucket is withheld and the scan is incomplete. This bounds adversarial
collision work without presenting a partial equivalence class as complete.

## Action classes and review colors

The review color is an evidence label, not a generic permission bit:

- **green** is live byte-verified exact evidence and is the only relationship
  that can authorize duplicate-removal quarantine;
- **yellow** is approximate/perceptual evidence and has no deletion authority;
- **blue** is related/transformed evidence and has no deletion authority;
- **gray** is unknown, stale, or incomplete and authorizes neither quarantine
  nor organizer mutation.

Copy and Move use a separate organizer contract. An explicit organizer
operation may accept a green, yellow, or blue item only when the scan is
complete and current and the target is still in Incoming Files. Protected
Library, Compare Only, saved-report, stale, incomplete, and gray inputs are
refused. Both the selected item and its review keeper must still have the
physical identity and content-generation token captured for the scan. This is
checked at the command boundary, again in the worker, and the selected
source's snapshot is checked once more by the no-replace file operation. Copy
preserves the source. Move removes the source name only after preserving the
payload at the destination. Neither operation creates byte-equality evidence
or permission for a later deletion.

A Folder scan compares recursive aggregate manifests. It is review-only for
file-action purposes: an equal folder digest is not `VERIFIED_PAYLOAD_EXACT`
or a typed similarity relation. It therefore remains gray and authorizes
neither quarantine nor the program-managed organizer Copy/Move path. Use
review output or the separately confirmed External Command boundary if a
whole-tree operation is intentionally required.

## Duplicate-removal invariant

Before duplicate-removal quarantine is committed, the executor must establish
all of the following:

- the target has the planned identity, type, size, and current content;
- the survivor has the planned identity and the same current content;
- target and survivor are distinct physical objects unless the operation is an
  explicit alias/hard-link operation;
- both objects are within their allowed real filesystem roots;
- protected, incomplete, stale, or unverified inputs have no destructive
  capability;
- at least one verified survivor or recoverable quarantine copy remains;
- the operation state has been durably journaled.

Any unknown result is a refusal, not a warning that can silently fall through.

## Recovery model

The default destructive workflow is:

`PLAN -> VERIFIED -> PREPARED -> QUARANTINED -> COMMITTED`

Permanent removal is a separate finalization step and requires an explicit
`quarantine finalize --execute`; restore and finalize are read-only preflights
without `--execute`. A crash or cancellation at any state must leave enough
journal information to determine whether the object is at its original path,
in quarantine, restored, stale, or in conflict.
Replaying an operation must be idempotent and must never delete an object whose
identity or content is newer than the plan.

Dataset bundle execution uses the same rule for a whole image-plus-sidecar
transaction. Before a permanent dataset finalization, every surviving keeper
and every quarantine payload is re-opened and byte-compared. Each payload is
then moved with a native atomic no-replace rename to an unpredictable
same-directory tombstone. The journal durably records, per file:

`TOMBSTONE_PREPARED -> TOMBSTONED -> PURGED`

The preparation binds the random name to the plan ordinal and physical file
identity. A restart can therefore distinguish an unstarted rename, a completed
rename, and an unlink whose final journal record was interrupted. If both the
old and tombstone names exist, either name has a different identity, a keeper
was replaced, or the tombstone has no matching journal record, recovery stops
without unlinking either path.

Rollback applies the same tombstone protocol to transaction-created
cross-volume destinations and temporary copies. Their physical identities are
journaled immediately after exclusive creation and before publication. A
partial copy may be removed by identity even though its content is incomplete;
an unjournaled or replaced path is preserved. Same-volume rollback and restore
use atomic no-replace rename and never resolve a two-name conflict by unlinking
one of the names.

Execution documents and journals have byte, line, file-record, and event
limits. Journals are parsed one bounded line at a time. An oversized or
malformed state file is rejected before a restore, rollback, or finalize
mutation. Plan interchange permits up to 250,000 file records, but one
crash-recoverable execution transaction permits at most 10,000. The executor
projects and reserves every apply/retry/rollback/restore-or-finalize event
using the concrete UTF-8 paths before mutation, so the journal byte limit can
lower that per-transaction ceiling. On POSIX, an existing executor state directory or
`.dupeguru-neo-dataset-quarantine` hierarchy must be owned by the current user
and must not be group/world writable. Windows applies no-link/reparse checks
and relies on the user's ACLs because POSIX owner/mode bits do not describe
Windows access control.

Dataset executor metadata always lives in the reserved
`.dupeguru-neo-dataset-executor` child of either the destination or an
explicit state base. The state-base option never makes an arbitrary directory
itself into executable state. Dataset discovery prunes all program-owned
quarantine and executor namespaces, while explicit assets, sidecars, keepers,
destinations, forged plans, and mutation-time records that enter those
namespaces fail closed. Windows-equivalent spellings, including case, trailing
dot or space, alternate-stream aliases, invalid components, and reserved
device components, are rejected at the applicable path-component boundary.
Each operation document embeds its physical state namespace, and list,
restore, and finalize reject a document copied under a different namespace.

## Bounded persistent preferences

Directory exclusions use the same strict contract before mutation, before
save, and during load: at most 4,096 regular expressions, 4,096 characters per
expression, 256 Ki characters of expression text, and a 4 MiB XML document.
Adding, renaming, or marking an expression is transactional; a candidate that
cannot be loaded again is rejected without changing the active list. If a
persisted exclusion or ignore list fails validation during startup, the UI
reports the failure and preserves both the active in-memory state and the
invalid source file. The source is replaced only after the user changes the
corresponding in-memory list and the replacement passes the same validation.

Folder selections use one loader/writer contract: at most 4,096 roots, 65,536
explicit state overrides, 32,767 characters per path, 48 Mi characters in the
XML tree, and a 64 MiB document. Save validation completes before the atomic
replacement, so an over-limit active selection cannot replace an existing
file. A startup validation failure is reported and its source is preserved
until the user changes the selection and the replacement validates.
Temporarily unavailable removable or network roots remain in the persistent
selection; the bounded walker reports their unavailability during a scan
instead of deleting them from the next saved session.

Qt preferences reject unsafe settings files above 8 MiB and bound individual
variant trees before recursive conversion. Schema-aware conversion preserves
string preferences such as custom commands even when their complete text is
`true`, `false`, or numeric. Dock areas are stored as bounded integer enum
values, recent lists retain at most ten validated paths, and the live table
header width ceiling matches the persisted one-million-pixel ceiling.

GUI scan types other than Standard-mode Contents use one bounded direct
filesystem-discovery pass. The configurable values may only be lowered from
hard ceilings of 1,000,000 retained files, 250,000 traversed/retained folders,
100,000 filesystem issues, and 14,400 seconds. Time is checked around every
walker event. Ordinary file and directory events are not copied into a second
audit list; only coverage-reducing events are retained within the issue budget,
with final per-root coverage stored separately. Folder scan post-order buffers
are bounded by the folder ceiling.

If any direct-discovery ceiling is exceeded or allocation raises
`MemoryError`, the GUI does not pass its partial input list to a matcher. It
publishes no groups and records a `RESOURCE_LIMIT` receipt, which disables
duplicate-removal quarantine and organizer Copy/Move. Cancellation remains a
distinct cooperative job outcome.
Very large exact-match libraries should use the bounded, resumable Persistent
Catalog behind the Standard-mode Contents scan.

## Direct-scan and catalog evidence

A direct Contents scan keeps each file's scan-start `FileSnapshot` in memory.
Strict digest reads and final byte comparisons also validate snapshots from
their opened handles, and each comparison retains its two stable snapshots.
That evidence is scoped to the current result set.

A catalog `scan_snapshots` row has a different purpose: it binds a complete
`scan_id` to enumeration coverage and observed paths. It is not an exact
content proof. Catalog exact projection reopens the files, validates the
cataloged content generations, and byte-compares every member with its
representative. Each successful edge has one persisted `verification_id`
binding the two content-version IDs, digest algorithm/version, full digest,
comparison time, and verification state. A read-only projection still compares
live bytes but may only reuse an already-persisted positive ID. The ID is a
historical comparison record, not an opened-handle snapshot and not mutation
authority. Quarantine always creates a fresh live SHA-256 and byte proof.

## Filesystem capability levels

- **stable**: the platform supplies a volume-scoped native file identifier and
  stable opened-handle metadata.
- **session-only**: identity is useful only during the current scan or action.
- **path-only**: no trustworthy native identity is available. Rename reuse and
  automatic destructive actions are disabled.

Native identity is an optimization and a race-detection input, not proof that
content is unchanged. Network filesystems, cloud placeholders, reparse points,
mount aliases, and concurrent writers can reduce the capability level.

Linux requires `renameat2(RENAME_NOREPLACE)` and macOS requires
`renameatx_np(RENAME_EXCL)` for dataset namespace mutations. Unsupported POSIX
platforms or filesystems fail closed; there is no link-then-unlink or
check-then-rename fallback. Windows uses its native non-overwriting rename
contract.

Catalog databases are local. A network share may contain scanned files but must
not host the live SQLite catalog.

## Payload versus filesystem-object equality

`VERIFIED_PAYLOAD_EXACT` covers the ordinary file data stream. Files may still
differ in ACLs, ownership, extended attributes, NTFS alternate streams, resource
forks, sparse/compression state, or sidecar assets. Such differences are
reported separately and can block automatic action according to policy.

Byte equality also does not prove that removing a redundant copy satisfies a
backup, retention, legal-hold, or dataset-diversity policy. Those are explicit
keeper constraints evaluated after content evidence.

## Out of scope

The strict guarantee does not claim protection against a privileged process or
kernel that can alter open handles, falsify filesystem metadata, tamper with the
running process, or corrupt storage below the filesystem interface. Hardware
failure after successful durable writes is also outside the content-equality
proof. The application reports degraded capabilities instead of presenting
such environments as fully verified.

An actively malicious process running as the same OS user and racing namespace
operations is also outside the absolute guarantee. Random tombstone names,
private state directories, open-handle checks, and an identity check
immediately before unlink make ordinary collision/replacement races detectable.
However, the portable POSIX/Windows abstraction still performs the final
unlink by tombstone path; it is not an inode-conditional delete primitive. A
same-user adversary that learns the random name and swaps it in the final
name-lookup interval may exceed this model. dupeGuru therefore describes this
as race hardening and fail-closed recovery, not as protection from a hostile
peer with equal account privileges.

User-configured custom commands are external programs and are not file actions
performed by either the quarantine or organizer executor. They may change or
permanently delete any supplied path, including protected or perceptually
similar results. The UI requires a separate confirmation before invoking one.
That confirmation does not apply green/yellow/blue eligibility or pool rules,
and no quarantine, organizer, identity-proof, no-overwrite, or recovery
guarantee applies to the external program's effects.
