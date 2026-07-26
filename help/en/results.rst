Results
=======

.. contents::

When dupeGuru is finished scanning for duplicates, it will show its results in the form of duplicate group list.

About duplicate groups
----------------------

A duplicate group is a group of files that all match together. Every group has a **reference file** and one or more **duplicate files**. The reference file is the first file of the group. Its mark box is disabled. Below it, and indented, are the duplicate files.

You can mark duplicate files, but the reference row itself is not markable.
This is one part of the program-managed action policy; live evidence and folder
pool checks are still required immediately before an action.

Folder policy is evaluated first. A Protected Library or Compare Only member is
immutable and takes the keeper position. Other members are ranked by an
explainable weighted policy: resolution, bit depth, retained metadata, bitrate,
lossless or RAW format, file size, copy-style names, temporary/download
locations, and JPEG recompression artifacts. The Details panel shows why the
current keeper was preferred and why another member ranked lower. This quality
ranking is review guidance; it is not duplicate-equality evidence.

You can change the reference file of a group manually. To do so, select the duplicate file you want
to promote to reference, and click on **Actions-->Make Selected into Reference**.

Reviewing results
-----------------

Only green, byte-verified exact results can authorize duplicate removal through
dupeGuru-managed quarantine, and the target must still be an Incoming Files
member from a complete, current scan. You should still review the proposed
keeper and marked files before choosing **Actions-->Quarantine Verified Marked
Files**. Yellow visual matches and blue related-media results never authorize
deletion, even at a 100 percent similarity score.

Copy and Move are explicit organizer operations, not duplicate-removal
operations. A yellow or blue item may be copied or moved only when it belongs
to Incoming Files and comes from a complete, current scan. A saved report,
incomplete or stale scan, Protected Library item, Compare Only item, or gray
unknown result cannot be used for an organizer operation. Copy preserves the
source; Move preserves its payload at the destination while removing the
source path. The selected item and keeper must still match their scan-time
physical identities and content generations, including a final check inside
the file operation. Neither operation upgrades the relationship to
byte-exact.

To help you reviewing the results, you can bring up the **Details panel**. This panel shows all the details of the currently selected file as well as its reference's details. This is very handy to quickly determine if a duplicate really is a duplicate. You can also double-click on a file to open it with its associated application.

If a similarity scan has more false matches than true matches, review it and
remove false matches from the result list. Similarity scores are never promoted
to byte-exact evidence merely because they pass a high threshold.

Marking and Selecting
---------------------

A **marked** duplicate is a duplicate with the little box next to it having a check-mark. A **selected** duplicate is a duplicate being highlighted. The multiple selection actions can be performed in dupeGuru in the standard way (Shift/Command/Control click). You can toggle all selected duplicates' mark state by pressing **space**.

Show Dupes Only
---------------

When this mode is enabled, the duplicates are shown without their respective reference file. You can select, mark and sort this list, just like in normal mode.

The dupeGuru results, when in normal mode, are sorted according to duplicate groups' **reference file**. This means that if you want, for example, to mark all duplicates with the "exe" extension, you cannot just sort the results by "Kind" to have all exe duplicates together because a group can be composed of more than one kind of files. That is where Dupes Only mode comes into play. To mark all your "exe" duplicates, you just have to:

* Enable the Dupes Only mode.
* Add the "Kind" column with the "Columns" menu.
* Click on that "Kind" column to sort the list by kind.
* Locate the first duplicate with a "exe" kind.
* Select it.
* Scroll down the list to locate the last duplicate with a "exe" kind.
* Hold Shift and click on it.
* Press Space to mark all selected duplicates.

.. _deltavalues:

Delta Values
------------

If you turn this switch on, numerical columns will display the value relative to the duplicate's
reference instead of the absolute values. These delta values will also be displayed in a different
color, orange,  so you can spot them easily. For example, if a duplicate is 1.2 MB and its reference
is 1.4 MB, the Size column will display -0.2 MB.

Moreover, non-numerical values will also be in orange if their value is different from their
reference, and stay black if their value is the same. Combined with column sorting in Dupes Only
mode, this allows for very powerful post-scan filtering.

Dupes Only and Delta Values
---------------------------

The Dupes Only mode unveil its true power when you use it with the Delta Values switch turned on.
When you turn it on, relative values will be displayed instead of absolute ones. So if, for example,
you want to remove from your results all duplicates that are more than 300 KB away from their
reference, you could sort the dupes only results by Size, select all duplicates under -300 in the
Size column, remove those rows from the result view, and then do the same for duplicates over 300
at the bottom of the list.

Same thing for non-numerical values: When Dupes Only and Delta Values are enabled at the same time,
column sorting groups rows depending on whether they're orange or not. Example: You ran a contents
scan and want to review byte-exact duplicates that also have the same filename. Sort by filename
and all dupes with their filename attribute being the same as the reference will be grouped
together, their value being in black. Quarantine eligibility still comes from the green
byte-verification state, not from the filename column.

You can also use this view to override the automatic keeper ranking. For
example, to prefer the latest modification time, sort the duplicates by
modification time in **descending** order, select rows whose delta is greater
than zero, and choose **Make Selected into Reference**. If two files from one
group are selected, only the first displayed member is promoted. An immutable
Protected Library or Compare Only keeper cannot be demoted by this command.

Filtering
---------

dupeGuru supports post-scan filtering. With it, you can narrow down your results so you can perform
actions on a subset of it. For example, you could easily mark all duplicates with their filename
containing "copy" from your results using the filter.

To use the filtering feature, type your filter in the "Filter" search field at the top-right corner
of the results window. What you type in that box will be applied to the *whole path* of every
duplicate in the results. Only duplicate *groups* having at least one duplicate matching the filter
will be shown.

When having groups where not all duplicates match the filter, we still show all duplicates of
the group. However, non-matching duplicates are in "reference mode". Therefore, you can perform
actions like "Mark All" and be sure to only mark filtered duplicates.

To go back to unfiltered result, blank out the field or click on the "X".

In simple mode (the default mode), whatever you type as the filter is the string used to perform the
actual filtering, with the exception of one wildcard: **\***. Thus, if you type "[*]" as your
filter, it will match anything with [] brackets in it, whatever is in between those brackets.

For more advanced filtering, you can turn "Use regular expressions when filtering" on. The filtering
feature will then use **regular expressions**. A regular expression is a language for matching text.
Explaining them is beyond the scope of this document. A good place to start learning it is
`regular-expressions.info`_.

Matches are case insensitive in both simple and regexp mode.

For the filter to match, your regular expression don't have to match the whole filename, it just
have to contain a string matching the expression.

Action Menu
-----------

**Clear Ignore List:**
    Remove all ignored matches you added. You have to start a new scan for the
    newly cleared ignore list to be effective.
**Export Results to XHTML:**
    Take the current results, and create an XHTML file out of it. The
    columns that are visible when you click on this button will be the columns present in the XHTML
    file. The file will automatically be opened in your default browser.
    Filenames and metadata are encoded as text and cannot inject markup or
    scripts into the generated page. CSV exports are written atomically and
    text cells that resemble spreadsheet formulas are neutralized by default.
**Quarantine Verified Marked Files:**
    Revalidate each marked target and its keeper, then move the target to a
    recoverable same-volume quarantine. The operation is journaled. Any stale,
    incomplete, related, or merely similar result is refused.
**Restore Last Quarantine Batch:**
    Restore the most recent batch to its original paths. Restoration never
    overwrites an object that appeared after the action.
**Move Marked to...:**
    Prompt you for a destination, and then relocate marked Incoming Files from
    a complete, current scan. This organizer operation is available to green,
    yellow, and blue relationships. Protected Library, Compare Only,
    saved-report, incomplete-scan, stale-scan, and gray rows are refused. Move
    preserves the selected payload at the destination; it is not a
    duplicate-equality proof or a substitute for the journaled quarantine
    workflow.
**Copy Marked to...:**
    Prompt you for a destination, and then copy marked Incoming Files from a
    complete, current scan. The same green/yellow/blue and pool rules as Move
    apply, but the source remains in place. The source file's path might be
    re-created in the destination, depending on the "Copy and Move"
    preference. Copy is an organizer operation and never authorizes later
    deletion.
**Remove Marked from Results:**
    Remove all marked duplicates from results. The actual files will
    not be touched and will stay where they are.
**Remove Selected from Results:**
    Remove all selected duplicates from results. Note that all
    selected reference files will be ignored, only duplicates can be removed with this action.
**Make Selected into Reference:**
    Promote selected duplicates to the review keeper position. A group whose
    keeper belongs to Protected Library or Compare Only is immutable and will
    not be changed. If more than one member of the same group is selected, only
    the first displayed member is promoted.
**Add Selected to Ignore List:**
    This first removes all selected duplicates from results, and
    then add the match of that duplicate and the current reference in the ignore list. This match
    will not come up again in further scan. The duplicate itself might come back, but it will be
    matched with another reference file. You can clear the ignore list with the Clear Ignore List
    command.
**Open Selected with Default Application:**
    Open the file with the application associated with selected file's type.
**Reveal Selected in Finder:**
    Open the folder containing selected file.
**Invoke Custom Command:**
    Invokes the external application you've set up in your preferences using the current selection
    as arguments in the invocation. The confirmation is a security boundary:
    an external command runs outside dupeGuru's evidence, protected-folder,
    organizer, quarantine, and undo rules. It is not Copy, Move, or quarantine
    and does not inherit their eligibility checks. It may modify or permanently
    delete any path passed to it, including a yellow/blue result or a protected
    file.
**Rename Selected:**
    Prompts you for a new name, and then rename the selected file.

Quarantine and finalization
---------------------------

Recoverable quarantine is the default. The dialog shows how many byte-verified
files will be processed and reminds you that both each target and its keeper are
checked again immediately before anything moves.

The results dialog only stages files in recoverable quarantine. It never
permanently removes the quarantine payload as part of the same action. Inspect
the persisted operation first; permanent finalization is a separate explicit
``quarantine finalize --execute`` command that performs another live
revalidation. Restore and finalize commands are read-only preflights unless
``--execute`` is present.

dupeGuru Neo does not replace removed paths with symbolic links or hard links.
It also does not silently fall back from a failed quarantine operation to a
direct deletion. Filesystems without a trustworthy action capability are
reported and refused.

.. _regular-expressions.info: https://www.regular-expressions.info
