AI image dataset mode
=====================

Dataset mode treats each primary image and its supported ``.txt``,
``.caption``, or ``.json`` sidecars as one bundle. Moving or quarantining an
image therefore cannot silently orphan its caption.

The preparation stage groups exact, near-duplicate, transformed, and related
images into leakage clusters. A complete cluster is assigned to one of the
train, validation, or test splits, so visually related inputs do not cross a
split boundary. Assignments are deterministic for a given seed and preserve
prior assignments when possible.

Keeper selection is explainable. The report records contributions from
protected folders, resolution, bit depth, metadata, lossless formats, file
size, filename hints, temporary locations, and JPEG artifact estimates.

Safety rules
------------

Near-duplicate, transformed, and related clusters are review-only. Automatic
quarantine is available only for a verified byte-exact image cluster whose
corresponding sidecars are also byte-identical. Missing, ambiguous, invalid,
oversized, or conflicting sidecars make the plan incomplete.

Sidecars are streamed under a 16 MiB per-file ceiling. Text and caption
payloads are not retained in aggregate. JSON sidecars are validated once and
then discarded; validation also limits nesting to 128 levels, total nodes to
250,000, items in one container to 100,000, and one string to 4 MiB before a
Python object graph is constructed.

Dataset execution is transactional and recoverable. A dry run is the default;
``--execute`` is required for apply, restore, or finalization. Cross-volume
copies are hashed and byte-compared before publication, and source files are
quarantined rather than permanently removed. Finalization is a separate
operation.

Executor documents, journals, temporary copies, and quarantine payloads are
private program state. Dataset discovery prunes
``.dupeguru-neo-quarantine``, ``.dupeguru-neo-dataset-quarantine``, and
``.dupeguru-neo-dataset-executor`` subtrees, and explicit primary, sidecar,
keeper, source, or destination paths inside them are rejected. Split names
cannot use these names or unsafe Windows path aliases.

When ``--state-root STATE_BASE`` is supplied, it names a base directory, not
the operation directory itself. State is always stored below
``STATE_BASE/.dupeguru-neo-dataset-executor``. Without that option, state is
stored below ``DESTINATION/.dupeguru-neo-dataset-executor``. The reserved
child keeps recovery metadata out of later dataset scans. A persisted
operation document is bound to this physical state namespace; copying it to a
different state base does not make it valid there.

The versioned plan and cluster export can be consumed as JSON or CSV by
training pipelines.

Command-line workflow
---------------------

Discover image bundles directly from roots and preview the resulting plan on
standard output:

.. code-block:: console

    dupeguru dataset prepare-root Incoming Library --destination-root Organized --protected-root Library

To create a reviewed plan file that is eligible for a later explicit apply,
request that capability and authorize only the plan-file write:

.. code-block:: console

    dupeguru dataset prepare-root Incoming Library --destination-root Organized --protected-root Library --allow-apply --plan-out dataset-plan.json --write
    dupeguru dataset validate dataset-plan.json
    dupeguru dataset apply dataset-plan.json
    dupeguru dataset apply dataset-plan.json --execute

The first ``apply`` is read-only. The second may stage bundle changes into the
recoverable dataset operation area. Inspect persisted operations before
restoring or permanently finalizing one:

.. code-block:: console

    dupeguru dataset list --destination-root Organized
    dupeguru dataset restore PLAN_ID --destination-root Organized
    dupeguru dataset restore PLAN_ID --destination-root Organized --execute
    dupeguru dataset finalize PLAN_ID --destination-root Organized
    dupeguru dataset finalize PLAN_ID --destination-root Organized --execute

Restore and finalize also default to previews. A dry-run-only plan remains
read-only even if ``--execute`` is supplied.

Prepare-input documents and plans accept exactly one strict JSON document;
JSONL and concatenated JSON documents are rejected. File input and standard
input use the same seek-free reader and the same 128 MiB UTF-8 byte limit.
Plans are limited to 250,000 actions and 250,000 file records. JSON and CSV
exports have a 128 MiB limit and are published only after the complete
bounded stream succeeds. Limit failures leave an existing destination
unchanged and occur before plan publication or dataset mutation.

Those are plan interchange limits. One crash-recoverable apply transaction is
limited to 10,000 file records. Split a larger plan before applying it.
Before the first mutation, the executor reserves the complete worst-case
recovery journal using the plan's actual UTF-8 paths, so unusually long paths
can make a smaller transaction the safe maximum.

CSV safety
----------

The default CSV export is a machine-readable, lossless representation. Paths,
IDs, and split names are untrusted data and are intentionally not rewritten,
so a value beginning with ``=``, ``+``, ``-``, ``@``, a tab, or a carriage
return can be interpreted as a formula by spreadsheet software. Do not open
the raw export directly in Excel or another spreadsheet.

API callers that need a display-oriented spreadsheet file must explicitly
pass ``spreadsheet_safe=True`` to ``export_plan_csv``. That mode prefixes
formula-like string cells with an apostrophe and labels the receipt
``csv-spreadsheet-safe``. It is not lossless and must not be used where exact
machine paths or IDs need to round-trip.
