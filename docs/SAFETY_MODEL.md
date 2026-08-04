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
physical identity, content-generation token, and scan-bound SHA-256 proof.
Non-folder direct scans other than the byte-exact **Contents** path capture the
physical identity and content-generation of every input before matching without
reading its payload, then revalidate every generation after matching. Only the
unique members of groups eligible for publication are streamed once to bind a
SHA-256 proof to that original generation. A changed input or a failed result
seal withholds every group. The byte-exact **Contents** path instead streams
SHA-256 during the final byte comparison and binds that proof to members of a
byte-verified exact candidate group. Later policy filtering may omit some of
those members from the published result. Its stable snapshots from opened
handles and final generation validation provide the corresponding freshness
boundary without an additional result-member read. Both designs detect
equal-length rewrites instead of trusting one observable timestamp tick. The
proof is checked at the command boundary, again in
the worker, and the selected source's full proof is consumed once more by the
no-replace file operation. Copy computes that terminal SHA-256 while performing
its already-required source-to-staging byte comparison, so it adds no third
full-file read. Move hashes the live publication descriptor immediately before
the no-replace rename. Immediately before either action, the review keeper is
also reread and matched to its scan-bound SHA-256. That pass checks job
cancellation at every bounded chunk and reports streamed bytes before the
action is counted as processed. The selected source is not redundantly read at
that gate because the executor owns its later, stronger terminal proof. Every
marked Copy/Move gets a fresh keeper read immediately before its own action.
Consequently, `k` actions which share one keeper intentionally read that keeper
`k` times: caching it across action boundaries would weaken freshness unless a
platform capability froze the keeper for the whole batch. Copy preserves the
source. Move removes the source name only after preserving the reviewed payload
at the destination. Rename only changes one incoming path with no replacement,
does not remove or copy payload bytes, and immediately invalidates the result
receipt; its UI gate therefore checks identity/generation and directory policy
but does not synchronously reread the selected item or keeper. Neither
operation creates byte-equality evidence or permission for a later deletion.

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
physical identity changed or whose ordinary data stream differs from the
payload authorized by the plan.

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

On Windows, verified regular-file rename consumes a DELETE-capable handle that
has been matched to the still-live content-verification handle. Permanent purge
sets delete disposition on an identity-checked handle to that same object; it
never falls back to unlinking the tombstone name. A last-moment replacement at
the tombstone path is therefore preserved and the operation fails closed.

Rollback applies the same tombstone protocol to transaction-created
cross-volume destinations and temporary copies. Their physical identities are
journaled immediately after exclusive creation and before publication. A
partial copy may be removed by identity even though its content is incomplete;
an unjournaled or replaced path is preserved. Same-volume rollback and restore
use atomic no-replace rename and never resolve a two-name conflict by unlinking
one of the names.

Finalize payloads and published cross-volume destinations are re-hashed and
byte-compared during replay. A partial-copy temporary is an executor-created,
private artifact and is cleaned by its durable physical identity even when its
bytes are incomplete; another same-user process reusing or rewriting that
private inode is outside this ownership contract. Tombstone replay proves the
current authorized payload, not that an identical-byte rewrite or a
metadata-only update never occurred after the rename.

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

Every GUI scan type, including byte-exact **Contents**, uses one bounded
direct filesystem-discovery pass. The configurable values may only be lowered
from hard ceilings of 1,000,000 retained files, 250,000 traversed/retained
folders, 100,000 filesystem issues, and 14,400 seconds. Time is checked around
every walker event. Ordinary file and directory events are not copied into a
second audit list; only coverage-reducing events are retained within the issue
budget, with final per-root coverage stored separately. Folder scan post-order
buffers are bounded by the folder ceiling.

If any direct-discovery ceiling is exceeded or allocation raises
`MemoryError`, the GUI does not pass its partial input list to a matcher. It
publishes no groups and records a `RESOURCE_LIMIT` receipt, which disables
duplicate-removal quarantine and organizer Copy/Move. Cancellation remains a
distinct cooperative job outcome.
The GUI does not silently switch to another storage path at those limits;
users must narrow the selected folders or add exclusions. A bounded, resumable
Persistent Catalog remains available as a separate, explicitly invoked
`dupeguru catalog` CLI workflow.

## Direct-scan and catalog evidence

Non-folder direct scans other than the byte-exact **Contents** path keep a
scan-start `FileSnapshot` for every input, but that initial snapshot contains
identity, size, mtime, and a content-generation token rather than a payload
digest. After matching, every input generation is revalidated. The scanner then
streams SHA-256 only for the unique members of groups that may be published and
binds each digest to its unchanged scan-start generation. Unmatched inputs are
never read merely to seed action authority. Generation capture and validation
report completed files at bounded intervals; result sealing reports both files
and streamed bytes and remains cancellation-aware. No result is published if
any input changed or any result proof could not be sealed.

The GUI byte-exact **Contents** scan likewise avoids scan-wide content passes
and does not need a separate result-sealing read. It records an exact-scan
snapshot and partitions by size first.
Only same-size candidates receive a partial hash, an optional sample hash above
the configured threshold, and then a full candidate digest. Every surviving
member is byte-compared with its representative through stable handles. That
final comparison streams SHA-256 at the same time and binds the organizer
review proof to members of a byte-verified exact candidate group. Subsequent
policy filtering may omit some members from the published group. The scan does
not reread every unique file to seed action authority. A final generation check
withholds any group containing a file that changed during the scan. Exact
groups use linear membership storage
instead of materializing every pair. The GUI neither creates catalog snapshots
nor appends scan history for this path.

In both direct paths, the in-memory evidence is scoped to the current result
set. Organizer Copy reuses its mandatory byte-comparison pass for the action
proof instead of performing another digest-only read. Organizer Move has no
copy pass to reuse and therefore performs one terminal full read through its
held publication descriptor.

A content-generation token is also mandatory; size and mtime alone are never
accepted as freshness evidence. On Windows, a regular-file token combines the
current volume USN-journal identifier with the file's current USN. Both values
are observed twice through the same no-follow object handle while a second
handle denies write sharing. `FILE_BASIC_INFO.ChangeTime` is metadata time, not
a file-content counter, and is not accepted as a regular-file fallback.
Directory membership changes do not reliably advance the directory object's
own USN. The typed `windows-usn-journal-directory-tree` token therefore adds a
canonical recursive tree digest to the root handle's journal identifier, object
USN, and `ChangeTime`. Each descendant is opened without following reparses and
is bound to a high-confidence volume plus 128-bit file ID. Its exact UTF-16
name, type, size, mtime, attributes, link count, journal identifier, file USN,
and `ChangeTime` are folded into the parent record; directory records recursively
fold their sorted child-record digests. Reparse children are rejected rather
than followed. Each complete tree pass visits every descendant once, and two
independent passes must produce the same digest. Root and descendant identity,
USN, `ChangeTime`, and link-count observations must also remain stable while
their handles are open.

Each tree pass fails closed above 1,000,000 descendants, 256 MiB of exact-name
bytes, 512 MiB of bounded record metadata, or depth 256. A 300-second deadline
is checked at every entry boundary and directory completion; it cannot preempt
one blocking filesystem call, whose timeout remains a filesystem or transport
property. Allocation, enumeration, identity, USN, reparse, resource-limit, and
mid-observation changes also fail closed. `ChangeTime` is never accepted without
working USN controls or as the only evidence of a tree change. Alternate data
streams are not part of the unnamed-payload equality claim, although their
changes ordinarily advance the descendant file USN and invalidate the tree
token. A failed observation is incomplete and cannot authorize a file action.
POSIX regular-file metadata freshness uses inode ctime bound to device/inode
identity and the opened object. Ctime is not treated as a sufficient content
counter because adjacent equal-length writes can share one observable tick.
Direct-scan organizer authority therefore additionally requires the scan-bound
SHA-256 proof described above; live SHA-256 and byte proofs remain mandatory
before quarantine. Directory Copy/Move uses a separate
`dupeguru-posix-directory-tree-v1` proof. It walks from an opened root
descriptor with no-follow, descriptor-relative opens, folds exact names,
identity, type, size, mode, mtime, ctime, and link count into a canonical
digest, and streams SHA-256 over every regular file's ordinary data stream.
Reading content is required because adjacent writes can share one observable
ctime tick on some POSIX filesystems. Links, special files, and multiply-linked
regular files are refused. One proof is bounded to 100,000 entries and depth
128; file bytes are streamed rather than retained in memory.

Directory Copy/Move validation takes the recursive token at the operation root
and repeats that root proof at the terminal boundary. Per-directory snapshots
inside the already bounded tree walk are intentionally shallow; the final root
proof covers every descendant. This avoids recursively rescanning each subtree
at every depth while preserving terminal detection of descendant content,
identity, name, and hard-link changes.

A catalog `scan_snapshots` row has a different purpose: it binds a complete
`scan_id` to enumeration coverage and observed paths. It is not an exact
content proof. Catalog exact projection reopens the files, validates the
cataloged content generations, and byte-compares every member with its
representative. Each successful edge has one persisted `verification_id`
binding the two content-version IDs, digest algorithm/version, full digest,
comparison time, and verification state. A read-only projection still compares
live bytes and streams SHA-256 during the same comparison pass. The live digest
must equal the persisted full-hash artifact before a positive verification
record or catalog group record can be produced; a stale artifact invalidates
the edge and makes the projection incomplete. In a writable catalog, this
failure atomically invalidates every verification involving the affected
content versions,
deletes all of their content-derived artifacts (not only the SHA-256 which
selected the bucket), fails and releases every old work-item lease, disconnects
those versions from the current physical-file rows, removes affected immutable
scan snapshots, and makes their scans and roots non-projectable. A later scan
must therefore create fresh content versions and enqueue every configured
analysis stage even when size, timestamp, and platform change token still look
unchanged. An expired worker cannot revive the retired work or republish an
artifact for the detached generation.

A read-only projection cannot perform that repair. It fails closed with an
explicit writable-repair requirement and leaves the database unchanged. The
`dupeguru catalog groups` command publishes only its structured error record,
not a partial group stream; running `dupeguru catalog scan` with the same
database and roots performs one bounded writable retirement-and-rescan repair
round before returning. A repeated mismatch remains a hard failure. A
read-only projection may otherwise only reuse an already-persisted positive ID.
The ID is a historical comparison record, not an opened-handle snapshot and
not mutation authority. Catalog groups are exposed by the explicitly invoked
CLI as reports only; the GUI byte-exact **Contents** scan neither consumes them
nor appends to their history. They cannot be converted directly into organizer
or quarantine authority. A current direct GUI scan is required for those
actions, and quarantine always creates a fresh live SHA-256 and byte proof.

## Filesystem capability levels

- **stable**: the platform supplies a volume-scoped native file identifier and
  stable opened-handle metadata.
- **session-only**: identity is useful only during the current scan or action.
- **path-only**: no trustworthy native identity is available. Rename reuse and
  automatic destructive actions are disabled.

Native identity is an optimization and a race-detection input, not proof that
content is unchanged. Network filesystems, cloud placeholders, reparse points,
mount aliases, and concurrent writers can reduce the capability level. On
Windows, a stable identity still does not replace the separate USN-generation
requirement; typical SMB and non-journaled volumes therefore remain
read-incomplete for evidence-producing workflows.

Linux requires `renameat2(RENAME_NOREPLACE)` and macOS requires
`renameatx_np(RENAME_EXCL)` for dataset namespace mutations. Unsupported POSIX
platforms or filesystems fail closed; there is no link-then-unlink or
check-then-rename fallback. For a Windows regular-file copy or move, terminal
verification and the native non-overwriting rename consume the same handle,
opened with read/attribute/delete access and read sharing only. A callback that
drops that preopened capability cannot produce a verified commit. Failed-staging
cleanup likewise applies disposition to the identity-checked DELETE handle
itself and never falls back to a path unlink.

The no-link path rule has one narrow macOS compatibility exception. The fixed
system root aliases `/var`, `/tmp`, and `/etc` may be canonicalized to
`/private/var`, `/private/tmp`, and `/private/etc` respectively only after the
alias, `/`, and physical target are authenticated twice: the alias and target
must be root-owned, the root must not be group/world writable, the target must
be a plain directory with the same physical identity as the followed alias,
and the exact link target and metadata must remain stable. All subsequent
opens use the authenticated physical path. Any other symbolic-link component,
changed alias, or failed authentication is rejected without a fallback.

The adapter's weaker `rename_no_replace()` entry point exists for publishing
immutable application-state files and legacy operations that make no verified
content claim. Safety-critical user-file executors are statically checked not to
call it or a path-based `unlink`; their verified adapter and any subclass are a
trusted internal boundary.

Directory copies use an unpredictable private staging tree and repeat the full
recursive token immediately before every candidate publication. Directory moves
repeat the full source-tree token after preflight and immediately before every
candidate rename. A POSIX copy opens the reviewed source root once, then performs
enumeration, no-follow stat/open, regular-file reads, and recursion relative to
that root and its opened child-directory descriptors; later parent-path
replacement cannot redirect the copy. Windows holds no-delete-sharing leases on
the source root and each directory being traversed, so a root-parent or active
subdirectory rename cannot redirect its path walk. Windows does not expose one
ordinary rename capability that freezes every descendant of an open directory,
so a hostile process which can mutate a descendant in the final
check-to-rename interval remains outside the directory-operation claim.
Automatic verified duplicate removal acts on regular files and does not rely on
that narrower directory guarantee.

POSIX has no portable identity-conditional rename or unlink operand;
descriptor-bound parents, unpredictable private staging names, and immediate
identity checks harden ordinary races, but a hostile same-user process that can
write the staging parent remains outside that POSIX claim. A scan-bound
regular-file Move hashes the held source descriptor and rechecks its bound name
immediately before `renameat2`/`renameatx_np`. Portable POSIX cannot deny writes
through another process's already-open descriptor, however, so an active
same-user writer can still alter that inode in the final digest-to-rename
interval (or after the rename). That adversarial content race is the same
documented boundary; ordinary same-tick rewrites completed before terminal
verification are detected by SHA-256 and fail closed.

Catalog databases are local. A network share may contain scanned files only
when the host platform can provide the required identity and generation
evidence, and it must not host the live SQLite catalog.

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
operations remains outside the absolute cross-platform guarantee. On Windows,
regular-file publication and purge are bound to identity-checked handles, so a
same-name replacement cannot redirect either operation and there is no
path-unlink fallback. POSIX has no portable identity-conditional unlink
primitive: descriptor-bound parents, random private tombstone names, and an
immediate identity check harden ordinary races, but a same-user adversary that
can replace the entry during the final name operation, or write a move source
through another already-open descriptor between its terminal digest and
rename, may exceed the POSIX model. The application therefore claims the
stronger no-write handle-bound guarantee only where the platform provides it.

User-configured custom commands are external programs and are not file actions
performed by either the quarantine or organizer executor. They may change or
permanently delete any supplied path, including protected or perceptually
similar results. The UI requires a separate confirmation before invoking one.
That confirmation does not apply green/yellow/blue eligibility or pool rules,
and no quarantine, organizer, identity-proof, no-overwrite, or recovery
guarantee applies to the external program's effects.
