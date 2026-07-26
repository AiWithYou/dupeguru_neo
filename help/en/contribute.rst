Contribute to dupeGuru Neo
==========================

dupeGuru Neo is GPLv3 software. Contributions to the engine, Qt application,
tests, documentation, translations, packaging, and safety analysis are
welcome. The canonical repository is the `source code repository`_.

Before starting work, read ``README.md``, ``docs/ARCHITECTURE.md``, and
``docs/SAFETY_MODEL.md`` from the repository. A change that can move,
quarantine, restore, or permanently remove a file must preserve the
fail-closed evidence and live-revalidation rules described by the safety
model.

Development process
-------------------

The ``master`` branch is the current integration branch. Build and test a fresh
checkout using the commands in ``README.md``. Keep changes focused, add a
regression test for behavior changes, and run the formatter, linter, relevant
tests, and an appropriate build or smoke test before proposing a change.

Use the collaboration channels currently enabled on the repository page for
ordinary bug reports and feature proposals. Include the dupeGuru Neo version,
operating system, filesystem type, scan mode, completion status, and the
smallest safe reproducer. Redact home-directory names and media paths from logs
and scan receipts.

Do not disclose a vulnerability or possible data-loss primitive through a
public collaboration channel. Follow the repository's ``SECURITY.md`` policy
instead, and never attach private media, credentials, catalog databases, or
unredacted operation journals to a public report.

Documentation and translations
------------------------------

The user and developer documentation is written in reStructuredText and built
with `Sphinx`_. Keep examples consistent with the safety labels: only
``verified_exact`` evidence can become eligible for a file action, and it still
requires live revalidation.

Translations live under ``locale/<language>/LC_MESSAGES``. Source strings are
generated from the current code and English documentation. Do not hand-edit
compiled ``.mo`` files; the build creates them from the tracked ``.po`` files.

Release and supply-chain changes
--------------------------------

Packaging or dependency changes must update the exact release lock, license
inventory, corresponding-source lock, SBOM expectations, and release tests as
applicable. The tagged workflow is the only supported publication path.
Portable archives are local smoke-build outputs only and must not be uploaded
or published until their complete native source, license, and SBOM closure is
machine-proven and the release policy is intentionally changed.

.. _source code repository: https://github.com/AiWithYou/dupeguru_neo
.. _Sphinx: https://www.sphinx-doc.org/
