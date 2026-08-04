Frequently Asked Questions
==========================

.. contents::

What is dupeGuru?
-----------------

dupeGuru is a tool to find duplicate files on your computer. It has three operational modes:
Standard, Music and Picture. Each mode has its own specialized preferences.

Each mode has multiple scan types, such as filename, contents, tags. Some scan types feature
advanced fuzzy matching algorithm, allowing you to find duplicates that other more rigid duplicate
scanners can't.

What makes it special?
----------------------

It's mostly about customizability. There's a lot of scanning options that allow you to get the
type of results you're really looking for.

How safe is it to use dupeGuru?
-------------------------------

Safety depends on the evidence and action type. Only a green Verified Exact
group has reached a full-file hash and final streaming byte comparison. Before
a program-managed quarantine, dupeGuru reopens the target and keeper, verifies
their current SHA-256 digests and bytes, checks the scan and folder policy, and
then journals a recoverable same-volume move. Similar, transformed, related,
saved-report, incomplete, or stale results cannot authorize that action.

Protected Library and Compare Only folders are immutable inputs to
dupeGuru-managed actions. Permanent removal is a separate explicit finalize
step; the default workflow is quarantine and restore. These guarantees do not
apply to a user-configured external command, a privileged or same-account
adversary, a hostile filesystem, hardware failure, or filesystem-object
metadata outside the ordinary payload stream. The exact guarantee and
non-goals are defined in the `safety model`_.

How can I report a bug or suggest a feature?
--------------------------------------------

dupeGuru is hosted on `GitHub`_ and it's also where issues are tracked. The best way to report a
bug or suggest a feature is to sign up on GitHub and `open an issue`_.

The mark box of a file I want to delete is disabled. What must I do?
--------------------------------------------------------------------

You cannot mark the reference (The first file) of a duplicate group. However, what you can do is to
promote a duplicate file to reference. Thus, if a file you want to mark is reference, select a
duplicate file in the group that you want to promote to reference, and click on
**Actions-->Make Selected into Reference**. If the keeper belongs to Protected
Library or Compare Only, it is immutable and cannot be removed from the keeper
position.

I have a folder from which I really don't want to delete files.
---------------------------------------------------------------

Set the folder to **Protected Library** at :doc:`folders`. dupeGuru-managed
quarantine, move, rename, and dataset actions treat files in that pool as
immutable keepers. **Compare Only** is also immutable but does not claim that
its files are authoritative originals. External custom commands run outside
this policy and must be reviewed separately.

What is this '(X discarded)' notice in the status bar?
------------------------------------------------------

In some cases, some matches are not included in the final results for security reasons. Let me use
an example. We have 3 file: A, B and C. We scan them using a low filter hardness. The scanner
determines that A matches with B, A matches with C, but B does **not** match with C. Here, dupeGuru
has kind of a problem. It cannot create a duplicate group with A, B and C in it because not all
files in the group would match together. It could create 2 groups: one A-B group and then one A-C
group, but it will not, for security reasons. Lets think about it: If B doesn't match with C, it
probably means that either B, C or both are not actually duplicates. If there would be 2 groups (A-B
and A-C), you would end up delete both B and C. And if one of them is not a duplicate, that is
really not what you want to do, right? So what dupeGuru does in a case like this is to discard the
A-C match (and adds a notice in the status bar). Thus, if you delete B and re-run a scan, you will
have a A-C match in your next results.

I want to mark all files from a specific folder. What can I do?
---------------------------------------------------------------

Enable the :doc:`Dupes Only <results>` mode and click on the Folder column to sort your duplicates
by folder. It will then be easy for you to select all duplicates from the same folder, and then
press Space to mark all selected duplicates.

I want to remove all files that are more than 300 KB away from their reference file. What can I do?
---------------------------------------------------------------------------------------------------

* Enable the :doc:`Dupes Only <results>` mode.
* Enable the **Delta Values** mode.
* Click on the "Size" column to sort the results by size.
* Select all duplicates below -300.
* Click on **Remove Selected from Results**.
* Select all duplicates over 300.
* Click on **Remove Selected from Results**.

I want to make my latest modified files reference files. What can I do?
-----------------------------------------------------------------------

* Enable the :doc:`Dupes Only <results>` mode.
* Enable the **Delta Values** mode.
* Click on the "Modification" column to sort the results by modification date.
* Click on the "Modification" column again to reverse the sort order.
* Select all duplicates over 0.
* Click on **Make Selected into Reference**.

I want to mark all duplicates containing the word "copy". How do I do that?
---------------------------------------------------------------------------

* Type "copy" in the "Filter" field in the top-right corner of the result window.
* Click on **Mark --> Mark All**.

I want to remove all songs that are more than 3 seconds away from their reference file. What can I do?
------------------------------------------------------------------------------------------------------

* Enable the :doc:`Dupes Only <results>` mode.
* Enable the **Delta Values** mode.
* Click on the "Time" column to sort the results by time.
* Select all duplicates below -00:03.
* Click on **Remove Selected from Results**.
* Select all duplicates over 00:03.
* Click on **Remove Selected from Results**.

I want to make my highest bitrate songs reference files. What can I do?
-----------------------------------------------------------------------

* Enable the :doc:`Dupes Only <results>` mode.
* Enable the **Delta Values** mode.
* Click on the "Bitrate" column to sort the results by bitrate.
* Click on the "Bitrate" column again to reverse the sort order.
* Select all duplicates over 0.
* Click on **Make Selected into Reference**.

I don't want [live] and [remix] versions of my songs counted as duplicates. How do I do that?
---------------------------------------------------------------------------------------------

If your comparison threshold is low enough, you will probably end up with live and remix
versions of your songs in your results. There's nothing you can do to prevent that, but there's
something you can do to easily remove them from your results after the scan: post-scan
filtering. If, for example, you want to remove every song with anything inside square brackets
[]:

* Type "[*]" in the "Filter" field in the top-right corner of the result window.
* Click on **Mark --> Mark All**.
* Click on **Actions --> Remove Selected from Results**.

The "Filter Hardness" slider in the preferences won't move!
-----------------------------------------------------------

This slider is only relevant for scan types that support "fuzziness". Many scan types, such as the
"Contents" type, only support exact matches. When these types are selected, the slider is disabled.

On some OS, the fact that it's disabled is harder to see than on others, but if you can't move the
slider, it means that this preference is irrelevant in your current scan type.

dupeGuru refused to quarantine a verified duplicate. Why?
---------------------------------------------------------

Neo refuses the whole action when a target or keeper changed after the scan,
cannot be reopened without following a link, is outside the approved roots, is
on an unsupported filesystem boundary, or no recoverable same-volume
quarantine can be established. Correct the reported permission or filesystem
problem and run a new scan. There is no automatic fallback to direct deletion.

For NAS libraries, keep the catalog database on a local disk. The NAS may also
provide weaker identity or durability guarantees; ``dupeguru doctor`` and the
scan receipt report the detected capability.

Why is Picture mode's contents scan so slow?
--------------------------------------------

Picture mode decodes and color-normalizes every new or changed image. It then
uses a perceptual-hash index to retrieve nearby candidates and runs the detailed
15×15 block comparison only for those candidates. A warm scan can reuse cached
features, while a cold scan must still decode the library. Libraries containing
many nearly identical images can also produce a large candidate set.

If all you need to find is exact duplicates, just use the standard mode of dupeGuru with the
Contents scan method. Exact mode avoids image decoding and gives the stronger
byte-equality evidence required for a file action.

Where are user files located?
-----------------------------

dupeGuru Neo asks Qt for the platform's application-data directory after
setting the organization to ``AiWithYou`` and the application name to
``dupeGuru Neo``. Typical locations are:

* Windows: ``%APPDATA%\AiWithYou\dupeGuru Neo``
* Linux: ``$XDG_DATA_HOME/AiWithYou/dupeGuru Neo`` (normally
  ``~/.local/share/AiWithYou/dupeGuru Neo``)
* macOS: ``~/Library/Application Support/AiWithYou/dupeGuru Neo``

The Windows preferences file is ``settings.ini`` in that directory. Linux and
macOS preferences use Qt's native ``QSettings`` location for the same
organization and application names. Image thumbnails use Qt's cache location,
which can be different from the application-data location.

Portable mode is explicit: a ``settings.ini`` beside the executable selects
portable preferences, and runtime data is stored in the adjacent ``data``
directory. The Advanced preferences page displays the log directory selected
for the current installation. Check that displayed path rather than assuming a
location before deleting anything.

.. _GitHub: https://github.com/AiWithYou/dupeguru_neo
.. _open an issue: https://github.com/AiWithYou/dupeguru_neo
.. _safety model: https://github.com/AiWithYou/dupeguru_neo/blob/main/docs/SAFETY_MODEL.md
