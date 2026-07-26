Persistent library catalog
==========================

The catalog stores file identities, paths, content generations, hashes, media
features, scan coverage, and resumable work in a local SQLite database. The
library itself may be on another volume or a network share, but the live
``catalog.sqlite3`` database must be on a local filesystem.

An initial scan enumerates every selected root and analyzes new content.
Later scans still reconcile directory metadata, but only new or changed
content is analyzed again. Native file identifiers preserve path history and
allow unambiguous moves to be reported. A rename or move still refreshes the
content artifact because both Windows ChangeTime and POSIX ctime can change on
rename; without a trusted filesystem journal, reusing the old hash would make a
simultaneous same-size edit with restored mtime indistinguishable from a pure
rename.

Interrupted scans remain explicitly incomplete. Resume the same scan rather
than treating an old complete scan as current. Results from an incomplete
scan can be reviewed, but they cannot authorize duplicate-removal quarantine
or organizer Copy/Move.

Backups use SQLite's online backup operation and are integrity-checked before
they are accepted. An existing backup destination is never overwritten.

Catalog evidence is an accelerator, not permanent authority. A catalog
``scan_id`` and its ``scan_snapshots`` row describe complete enumeration
coverage and the observed paths; they are not byte-equality evidence. When an
exact group is projected, the worker reopens the current files, validates their
cataloged content generations, and compares each member byte for byte with the
representative.

That projection stores one positive ``verification_id`` for each
representative-to-member comparison. The identifier refers to a persisted
record binding two content-version IDs, the SHA-256 digest and algorithm
version, the comparison time, and its verification state. It is not the
in-memory opened-handle snapshot used by a direct Contents scan, and it is not
permission to change either path. Duplicate-removal quarantine always reopens
both the candidate and its keeper and creates a new live SHA-256 and byte proof.

Command-line workflow
---------------------

Create the database on a local filesystem and scan one or more library roots:

.. code-block:: console

    dupeguru catalog scan catalog.sqlite3 Pictures Archive

The result contains a durable integer ``scan_id``. A scan stopped by a worker
bound remains resumable; inspect and resume that same identifier:

.. code-block:: console

    dupeguru catalog status catalog.sqlite3 12
    dupeguru catalog resume catalog.sqlite3 12

Verified exact groups are streamed as bounded JSONL records from the latest
complete, currently projectable scan:

.. code-block:: console

    dupeguru catalog groups catalog.sqlite3 --page-size 500 > exact-groups.jsonl

The group stream uses ``dupeguru.catalog-group-record-v2``. It starts with one
``header`` record, then emits ``group_header``, one or more ``member_chunk``
records, and ``group_end`` for each group, followed by one global ``summary``.
Join those records by ``group_id`` and order chunks by ``chunk_index``;
``first_member_index`` makes omissions and reordered chunks detectable. The
first member has a null ``verification_id`` and every later member has one
positive verification identifier, so a group of *k* files carries exactly
*k* - 1 persisted representative-to-member comparison records. A normal
read-write projection records or refreshes those records only after its current
live byte comparisons succeed. A read-only projection still performs the live
comparisons, but it can return a member only when the matching positive record
was already persisted; it cannot create or invalidate catalog state.
``group_id`` binds each path to its path, physical-file, content-version, and
verification identifiers. Neither ``group_id`` nor ``verification_id``
authorizes deletion; a fresh action proof is still required.

Large groups are split dynamically by encoded byte length. Catalog machine
output is strict UTF-8 JSONL and is bounded to 8 MiB per physical line
(including the newline), 40,000 members per ``member_chunk``, 2 GiB total, and
4,000,000 records. The member-count boundary keeps every short-path chunk
inside the JSON node and scalar budgets even when the byte boundary would not
split it. A group report accepts at most 1,000,000 groups and 1,000,000 members
in one group; a maximum-size group therefore uses at least 25 member chunks.
These independent limits are consistent with the global record limit: every
minimal group needs three records, so 1,000,000 minimal groups use 3,000,002
records including the global header and summary. A change report accepts at
most 3,999,998 changes, leaving room for its header and summary.

Before loading candidate rows or byte-comparing a group, ``catalog groups``
queries aggregate projection counts. It rejects an over-limit group or group
count immediately. Accepted projection pages pass explicit SQL bounds of
1,000,000 total file rows and 1,000,000 members in any one group; the
``--page-size`` option continues to bound the number of groups. The row bounds
are never left at SQLite's unbounded sentinel.

Every catalog response—including scan, resume, status, backup, normal error,
group, and change output—is first written to a private temporary spool. The
complete spool is then re-read and checked for strict UTF-8, JSON structure,
schema consistency, record order, chunk continuity, and matching counts before
standard output receives its first byte. An encoding, size, record-count,
temporary-storage, or validation failure therefore leaves standard output
empty and returns a nonzero status. For a normal process stream, validated
bytes are copied directly to its binary buffer, preserving UTF-8 and LF even
when the Windows text wrapper uses another encoding or CRLF translation.
In-memory text streams without a binary buffer use a strict UTF-8 decoder. A
normal command error is published as one validated
``dupeguru.catalog-error`` record. Only a failure while copying an already
validated spool to standard output can leave a prefix; that condition returns
the failed status and is reported separately on standard error.

Compare two complete immutable snapshots without guessing renames:

.. code-block:: console

    dupeguru catalog changes catalog.sqlite3 --from 12 --to 13 --page-size 500 > changes.jsonl

Only an unambiguous one-to-one stable native file identity is reported as
``moved``. Ambiguous hard links and path-only identities remain separate
``added`` and ``missing`` changes. These reports never authorize
duplicate-removal quarantine or organizer Copy/Move.

Create an online, integrity-checked backup at a new path:

.. code-block:: console

    dupeguru catalog backup catalog.sqlite3 catalog-backup.sqlite3

The destination must not already exist. Catalog commands use distinct nonzero
exit statuses for incomplete, cancelled, resource-limited, invalid, and failed
states; always inspect both the status and the versioned document.
