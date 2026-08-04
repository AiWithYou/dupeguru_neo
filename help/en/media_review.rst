Picture review and comparison
=============================

Picture mode first normalizes EXIF orientation, color management, and alpha.
An indexed perceptual fingerprint narrows the candidate set. A dHash and fixed
color histogram conservatively filter and rank that set before the existing 15
by 15 block comparison performs the detailed whole-image visual test.
``Find Similar Image`` uses this same pHash candidate search and block
comparison against the selected Picture roots. A blue ``related`` result is a
visual candidate below the configured similarity threshold; it does not claim
semantic embedding evidence.

A ``crop candidate`` means that a bounded center/content tile fingerprint
matched a whole or tile fingerprint. The normalized candidate rectangles are
recorded, but spatial crop alignment is not yet verified. ``Transformed`` and
``crop candidate`` are always review-only for duplicate-removal purposes, like
every visual relation, and can never enable deletion.

The review panel presents one match group as a virtualized thumbnail gallery.
Thumbnails are loaded lazily, and only visible rows are painted. Hover or
select an image to preview it at full viewer resolution. Selecting another
member of an unchanged group refreshes only that card; normal next-group
navigation does not copy or walk the complete result table.

Comparison modes
----------------

* **Side by side** keeps the two synchronized viewers visible.
* **Alpha overlay** blends normalized images to reveal displacement.
* **Blink** alternates the images for visual registration checks.
* **Difference heatmap** colors absolute per-pixel differences.

The toolbar keeps zoom and pan synchronized. Image metadata and the
explainable keeper score are shown with the review item.

Safety colors
-------------

* Green means a live byte-verified exact group.
* Yellow means perceptually similar media.
* Blue means related or transformed media.
* Gray means evidence or scan coverage is incomplete.

Only a green result from a complete, current scan can authorize
duplicate-removal quarantine, and its target must still be in Incoming Files.
Visual similarity never becomes deletion authority, even when its score is 100
percent.

Yellow and blue items from Incoming Files may still be explicitly copied or
moved as organizer operations after a complete, current scan. Protected
Library, Compare Only, saved-report, stale, incomplete, and gray items are
refused. Copy and Move preserve the selected payload and do not turn visual
evidence into an exact proof. Custom commands are external programs under a
separate confirmation contract; they do not inherit these organizer or
quarantine protections.

Keyboard review
---------------

Use ``1`` to choose the selected image as keeper, ``2`` to toggle a green item
that is eligible for quarantine, and ``Space`` to advance to the next group.
Choose **Byte-exact contents** to produce green groups in Picture mode. The
fast review action **“Singularity”** means only pressing ``Enter`` on such a
group to accept its keeper and continue. It rechecks whether every non-keeper
is eligible to be marked from the current complete result, marks the whole
batch, and advances to the next group. If any member is no longer eligible,
the batch is left unchanged. This changes review marks only; quarantine is a
separate explicit operation that performs fresh content verification. It is
never available to yellow, blue, gray, saved-report, or visual-query results.
Use the explicit Copy or Move command—not the quarantine shortcut—to organize
an eligible yellow or blue Incoming Files item.
