Preferences
===========

**Tags to scan:**
    When using the **Tags** scan type, you can select the tags that will be used for comparison.

**Word weighting:**
    See :ref:`word-weighting`.

**Match similar words:**
    See :ref:`similarity-matching`.

**Match pictures of different dimensions:**
    If you check this box, pictures of different dimensions will be allowed in the same
    duplicate group.

**Match pictures of different rotations:**
    If you check this box, pictures of different rotations will be allowed in the same
    duplicate group.

.. _filter-hardness:

**Filter Hardness:**
    The threshold needed for two files to be considered duplicates. A lower value means more
    duplicates. The meaning of the threshold depends on the scanning type (see :doc:`scan`).
    Only works for :ref:`worded <worded-scan>` and :ref:`picture blocks <picture-blocks-scan>`
    scans.

**Can mix file kind:**
    If you check this box, duplicate groups are allowed to have files with different extensions. If
    you don't check it, well, they aren't!

**Ignore duplicates hardlinking to the same file:**
    If this option is enabled, dupeGuru uses the platform's volume-scoped
    physical file identity to avoid reporting two directory entries for the
    same underlying file. A path is not used as an identity fallback.

**Compare files only between different folder pools:**
    Suppress a group when all of its members belong to one comparison pool.
    This is useful when checking Incoming Files against a Protected Library
    while ignoring internal duplicates in either collection.

**Direct scan resource limits:**
    Contents, filename, folder, tag, and picture scans enumerate the selected
    filesystem directly before matching. The Advanced preferences cap one pass
    at 1,000,000 files, 250,000 folders, 100,000 filesystem issues, and 14,400
    seconds; each value may be lowered but not raised above that hard ceiling.
    Reaching a limit or running out of memory discards the entire partial input
    list before matching and publishes no duplicate groups. The result receipt
    is marked resource-limited and bulk file actions remain disabled.

    A byte-exact **Contents** scan first partitions files by size, applies
    staged hashes only to candidates, and byte-compares the survivors. It does
    not create Persistent Catalog history. The resumable catalog is a separate,
    explicitly invoked ``dupeguru catalog`` CLI workflow.

**Use regular expressions when filtering:**
    If you check this box, the filtering feature will treat your filter query as a
    **regular expression**. Explaining them is beyond the scope of this document. A good place to
    start learning it is `regular-expressions.info`_.

**Remove empty folders after move:**
    When this option is enabled, source folders are removed if a move leaves
    them empty. Quarantining a duplicate deliberately leaves its former folder
    in place; this option never broadens a quarantine operation into directory
    deletion.

**Copy and Move:**
    Determines how the Copy and Move operations (in the Action menu) will behave.

* **Right in destination:** All files will be sent directly in the selected destination, without
  trying to recreate the source path at all.
* **Recreate relative path:** The source file's path will be re-created in the destination folder up
  to the root selection in the Directories panel. For example, if you added
  ``/Users/foobar/SomeFolder`` to your Directories panel and you move
  ``/Users/foobar/SomeFolder/SubFolder/SomeFile.ext`` to the destination
  ``/Users/foobar/MyDestination``, the final destination for the file will be
  ``/Users/foobar/MyDestination/SubFolder`` (``SomeFolder`` has been trimmed from source's path in
  the final destination.).
* **Recreate absolute path:** The source file's path will be re-created in the destination folder in
  its entirety. For example, if you move ``/Users/foobar/SomeFolder/SubFolder/SomeFile.ext`` to the
  destination ``/Users/foobar/MyDestination``, the final destination for the file will be
  ``/Users/foobar/MyDestination/Users/foobar/SomeFolder/SubFolder``.

In all cases, dupeGuru nicely handles naming conflicts by prepending a number to the destination
filename if the filename already exists in the destination.

**Custom Command:**
    This preference determines the command that will be invoked by the "Invoke Custom Command"
    action. You can invoke any external application through this action. This can be useful if,
    for example, you have a nice diffing application installed. A custom
    command is not covered by dupeGuru's verification, protected-folder,
    quarantine, or restore guarantees. You must confirm that boundary before
    each invocation.

The format of the command is the same as what you would write in the command line, except that there
are 2 placeholders: **%d** and **%r**. These placeholders will be replaced by the path of the
selected dupe (%d) and the path of the selected dupe's reference file (%r).

If the path to your executable contains space characters, you should enclose it in "" quotes. You
should also enclose placeholders in quotes because it's very possible that paths to dupes and refs
will contain spaces. Here's an example custom command::

    "C:\Program Files\SuperDiffProg\SuperDiffProg.exe" "%d" "%r"

.. _regular-expressions.info: http://www.regular-expressions.info
