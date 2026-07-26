CLI and machine-readable reports
================================

The ``dupeguru`` command does not import Qt. It emits versioned JSON or JSONL
for exact scans, catalog operations, visual search, dataset preparation, video
analysis, quarantine recovery, and diagnostics.

Run ``dupeguru --help`` for the installed command list and
``dupeguru schema --help`` for the bundled JSON Schema names.

Safe execution model
--------------------

Commands that can change files are validation-only by default. Supplying a
plan to ``apply`` without ``--execute`` performs complete preflight checks and
does not create a quarantine directory or alter a source file. Dataset apply,
restore, and finalization use the same explicit execution boundary.

Keep standard output for machine-readable documents. Progress and errors are
written to standard error so piping a report does not corrupt it.

Exact automation follows this sequence:

.. code-block:: console

    dupeguru scan Pictures Archive --format jsonl > scan.jsonl
    dupeguru plan scan.jsonl --operation quarantine > plan.jsonl
    dupeguru apply plan.jsonl --dry-run
    dupeguru apply plan.jsonl --execute
    dupeguru quarantine list Pictures Archive
    dupeguru quarantine finalize path/to/operation-plan.json --dry-run
    dupeguru quarantine finalize path/to/operation-plan.json --execute

The first ``apply`` explicitly requests the default dry run. Review its result
before running the execution command. Exact plans accept only
``operation=quarantine`` and ``apply --execute`` only stages files in the
recoverable quarantine. Permanent removal requires the separate
``quarantine finalize --execute`` command for a persisted operation plan.
Both ``quarantine restore`` and ``quarantine finalize`` are read-only
preflights unless ``--execute`` is supplied. Legacy ``operation=delete`` plans
are rejected.

Visual automation
-----------------

Visual search is always read-only and reports ``similar`` or ``related``
evidence, never byte-exact proof:

.. code-block:: console

    dupeguru visual scan Pictures --cache /var/cache/dupeguru/visual.sqlite3 --max-images 250000
    dupeguru visual query reference.png Pictures --max-candidate-pairs 250000

The cache path must be outside every input root. File, candidate, match,
decode-pixel, and total-time caps are explicit options. Reaching one produces
a schema-valid partial report and a nonzero partial-result exit status.

Direct exact scan limits
------------------------

The direct ``dupeguru scan`` path is bounded while it discovers files,
records issues, hashes content, byte-verifies groups, and constructs the
report. Its defaults are 1,000,000 retained files, 100,000 issue records,
250,000 verified groups, and 14,400 seconds (four hours). Override them with
``--max-files``, ``--max-issues``, ``--max-groups``, and ``--max-seconds``.

The limits apply during production rather than after an unbounded result has
been materialized. In particular, verified groups are processed one full
digest bucket at a time, and hashing and final byte comparison check the time
budget between bounded chunks. If another file, issue, or group would exceed
its cap, or the time budget expires, the command stops and emits a
schema-valid report with ``summary.complete=false`` and an explicit
``resource-limit-*`` issue. The command returns the partial-scan exit status,
and ``dupeguru plan`` refuses that report even when it contains individually
verified groups.

Programmatic callers use the same contract through ``ScanRequest``. The
defaults are exported by ``core.services`` as
``DEFAULT_SCAN_MAX_FILES``, ``DEFAULT_SCAN_MAX_ISSUES``,
``DEFAULT_SCAN_MAX_GROUPS``, and ``DEFAULT_SCAN_MAX_SECONDS``. Every limit
must be positive; the time limit must also be finite.

Input resource limits
---------------------

Exact scan reports and deletion plans are read through seek-free bounded
loaders. A single JSON document is limited to 64 MiB. JSONL is recommended for
large inputs and is limited to 8 MiB per physical line, 2 GiB in total,
1,100,000 physical lines, and 1,000,000 nonblank records. Scan reports are
also limited to 100,000 roots, 250,000 groups, 500,000 issues, and 100,000
coverage records. Across all scan groups, at most 1,000,000 reference and
duplicate file records are accepted. Deletion plans are limited to 100,000
roots and 250,000 actions; plan creation checks the action cap before
appending each action.

Exact scans and plan creation emit JSONL by default. Before writing the first
byte, both JSON and JSONL writers validate the complete generated output
against the same byte, line, record, structure, and collection limits as their
loaders. An over-limit command therefore returns a nonzero input-error status
without leaving a partial machine-readable document on standard output. Use
``--format json`` only when the complete document fits the 64 MiB
single-document and structure limits.

Video-library reports use the same bounded JSON and JSONL writers. Other
single-JSON service output, including apply, query, doctor, quarantine, and
schema reports, is also rendered and checked against the 64 MiB and structural
limits before its first byte is written. Those commands leave standard output
empty if their generated report cannot satisfy the machine-readable contract.

Dataset prepare-input documents and dataset plans accept one strict JSON
document, not JSONL, and are limited to 128 MiB whether read from a file or
standard input. A dataset plan may contain at most 250,000 bundle actions and
250,000 file records. JSON and CSV plan exports are also limited to 128 MiB
and are generated incrementally into an unpublished temporary file. An
over-limit export is discarded without creating or replacing its destination.
These are interchange limits. The recoverable executor accepts at most 10,000
file records in one apply transaction; split a larger plan before apply.
Its pre-mutation reservation uses the concrete UTF-8 path lengths, so the
journal byte cap can reject a smaller transaction with unusually long paths.
All byte limits count the UTF-8 representation. An over-limit input is
rejected with an input-error status before an apply or plan-file write can
mutate the filesystem.

The programmatic defaults are exported by ``core.services.jsonio`` as
``MAX_JSON_DOCUMENT_BYTES``, ``MAX_JSONL_LINE_BYTES``,
``MAX_JSONL_TOTAL_BYTES``, ``MAX_JSONL_LINES``, ``MAX_JSONL_RECORDS``,
``MAX_SCAN_GROUPS``, ``MAX_SCAN_ISSUES``,
``MAX_SCAN_COVERAGE_RECORDS``, ``MAX_SCAN_FILE_RECORDS``,
``MAX_DOCUMENT_ROOTS``, and ``MAX_PLAN_ACTIONS``. Dataset JSON uses
``core.dataset_io.DEFAULT_JSON_SIZE_LIMIT``; dataset plan and export limits
are exported by ``core.dataset_service`` as
``MAX_DATASET_PLAN_ACTIONS``, ``MAX_DATASET_PLAN_FILE_RECORDS``,
``MAX_DATASET_PLAN_DOCUMENT_BYTES``, and ``MAX_DATASET_EXPORT_BYTES``.
The per-transaction execution ceiling is exported by
``core.dataset_executor.MAX_EXECUTION_TRANSACTION_FILES``.

Exit status
-----------

Zero means the requested complete operation succeeded. Dedicated nonzero
statuses distinguish invalid input, partial coverage, failed verification,
capability-limited video analysis, and failed mutation. A partial command can
still emit a valid schema-versioned report; callers must inspect both its exit
status and its document state.
