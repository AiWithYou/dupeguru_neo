The scanning process
====================

.. contents::

dupeGuru has 3 basic ways of scanning: :ref:`worded-scan`,
:ref:`contents-scan`, and :ref:`picture blocks <picture-blocks-scan>`.
Standard and Music expose the first two families. Picture exposes both its own
visual search and the shared byte-exact contents engine. The scanning process
is configured through the :doc:`Preference pane <preferences>`.

.. _worded-scan:

Worded scans
------------

Worded scans extract a string from each file and split it into words. The string can come from two
different sources: **Filename** or **Tags** (Music Edition only).

When our source is music tags, we have to choose which tags to use. If, for example, we choose to
analyse *artist* and *title* tags, we'd end up with strings like
"The White Stripes - Seven Nation Army".

Words are split by space characters, with all punctuation removed (some are replaced by spaces, some
by nothing) and all words lowercased. For example, the string "This guy's song(remix)" yields
*this*, *guys*, *song* and *remix*.

Once this is done, the scanning dance begins. Finding duplicates is only a matter of finding how
many words in common two given strings have. If the :ref:`filter hardness <filter-hardness>` is,
for example, ``80``, it means that 80% of the words of two strings must match. To determine the
matching percentage, dupeGuru first counts the total number of words in **both** strings, then count
the number of words matching (every word matching count as 2), and then divide the number of words
matching by the total number of words. If the result is higher or equal than the filter hardness,
we have a duplicate match. For example, "a b c d" and "c d e" have a matching percentage of 57
(4 words matching, 7 total words).

Fields
^^^^^^

Song filenames often come with multiple and distinct parts and this can cause problems. For example,
let's take these two songs: "Dolly Parton - I Will Always Love You" and
"Whitney Houston - I Will Always Love You". They are clearly not the same song (they come from
different artists), but they still still have a matching score of 71%! This means that, with a naive
scanning method, we would get these songs as a false positive as soon as we try to dig a bit deeper
in our dupe hunt by lowering the threshold a bit.

This is why we have the "Fields" concept. Fields are separated by dashes (``-``). When the
"Filename - Fields" scan type is chosen, each field is compared separately. Our final matching score
will only be the lowest of all the fields. In our example, the title has a 100% match, but the
artist has a 0% match, making our final match score 0.

Sometimes, our song filename policy isn't completely homogenous, which means that we can end up with
"The White Stripes - Seven Nation Army" and "Seven Nation Army - The White Stripes". This is why
we have the "Filename - Fields (No Order)" scan type. With this scan type, all fields are compared
with each other, and the highest score is kept. Then, the final matching score is the lowest of them
all. In our case, the final matching score is 100.

Note: Each field is used once. Thus, "The White Stripes - The White Stripes" and
"The White Stripes - Seven Nation Army" have a match score of 0 because the second
"The White Stripes" can't be compared with the first field of the other name because it has already
been "used up" by the first field. Our final match score would be 0.

*Tags* scanning method is always "fielded". When choosing this scan method, we also choose which
tags are going to be compared, each being a field.

.. _word-weighting:

Word weighting
^^^^^^^^^^^^^^

When enabled, this option slightly changes how matching percentage is calculated by making bigger
words worth more. With word weighting, instead of having a value of 1 in the duplicate count and
total word count, every word have a value equal to the number of characters they have. With word
weighting, "ab cde fghi" and "ab cde fghij" would have a matching percentage of 53% (19 total
characters, 10 characters matching (4 for "ab" and 6 for "cde")).

.. _similarity-matching:

Similarity matching
^^^^^^^^^^^^^^^^^^^

When enabled, similar words will be counted as matches. For example "The White Stripes" and
"The White Stripe" would have a match score of 100 instead of 66 with that option turned on.

Two words are considered similar if they can be made equal with only a few edit operations (removing
a letter, adding one etc.). The process used is not unlike the
`Levenshtein distance`_. For the technically inclined, the actual function used is
Python's `get_close_matches`_ with a ``0.8`` cutoff.

**Warning:** Use this option with caution. It is likely that you will get a lot of false positives
in your results when turning it on. However, it will help you to find duplicates that you wouldn't
have found otherwise. The scan process also is significantly slower with this option turned on.

.. _contents-scan:

Contents scans
--------------

Contents scans establish byte equality. Files are first grouped by size. Fast
partial and multi-position sample hashes may reject non-matches, but they are
never reported as exact evidence. Surviving candidates receive a full streaming
digest and are finally compared byte for byte through stable file handles.

An exact group is accepted only if the files remain the same physical objects
and content generations throughout those reads. A hash collision therefore
cannot turn different payloads into a verified group. The result window labels
this state **Byte-verified exact**. A later duplicate-removal quarantine action
performs a new SHA-256 and byte comparison rather than trusting the scan result
indefinitely.

In a direct Contents scan, each file carries an in-memory baseline snapshot
captured at scan start. Strict digest reads and each
representative-to-member byte comparison also validate snapshots from their
opened handles. These in-memory snapshots last only for the current result set.
A Persistent Catalog uses a different contract: its complete ``scan_id``
snapshot proves enumeration coverage, while a positive persisted
``verification_id`` identifies a byte comparison between two cataloged content
versions. Neither kind of scan evidence is action authority by itself;
quarantine always creates a new live proof.

The :ref:`filter hardness <filter-hardness>` preference is ignored in this scan.

Choose **Contents** in Standard or Music mode, or **Byte-exact contents** in
Picture mode, to use this engine. Picture exact scanning does not decode image
pixels; it compares the complete files and keeps Picture's thumbnail review UI.

Folders
^^^^^^^

This is a special Contents scan type. It works like a normal contents scan, but
instead of trying to find duplicate files, it tries to find duplicate folders.
A folder candidate matches another when its recursively aggregated manifest
digest matches. The digest represents the folder tree; it is not a streaming
byte comparison of one file object and does not establish
**Byte-verified exact** evidence.

This scan is, of course, recursive and subfolders are checked. dupeGuru keeps only the biggest
fishes. Therefore, if two folders that are considered as matching contain subfolders, these
subfolders will not be included in the final results.

With this mode, we end up with folders as aggregate, review-only results
instead of files. Folder results are gray aggregate evidence: they authorize
neither duplicate-removal quarantine nor the program-managed organizer Copy or
Move path. A deliberately configured External Command is a separate,
explicitly confirmed trust boundary and does not inherit dupeGuru's evidence,
no-overwrite, or recovery guarantees.

Exact hash cache
----------------

Version 5 writes exact-file hash evidence to ``hash_cache_v3.sqlite3`` in the
application-data directory. The older ``hash_cache.db`` is never opened,
imported, renamed, deleted, or overwritten. It is left in place, and the first
Version 5 contents scan recalculates hashes into the new marker-owned cache.
This one-time rehash is intentional: unmarked SQLite files are not treated as
application-owned state.

.. _picture-blocks-scan:

Visual similarity
-----------------

Picture mode's **Visual similarity** scan finds images that may look alike even
when their file bytes differ. It is distinct from Picture mode's
**Byte-exact contents** scan.

We decode the first image frame, apply EXIF orientation, convert a valid embedded
ICC profile to sRGB, composite transparency onto a defined white background,
and calculate a deterministic perceptual hash and 15x15 color grid. These
features are cached against the file's current content generation.

The perceptual-hash MultiIndex retrieves all fingerprints within the configured
Hamming radius. Detailed tile comparison then runs only for those candidates.
Each corresponding average color produces a difference, and the accumulated
difference becomes the visual score.

If that score is smaller or equal to ``100 - threshold``, we have a match.

Visual scores are capped below the exact label. Even a perfect decoded-pixel
score is an **Approximate similarity** result because encoding, metadata, and
non-image payloads may differ. Select **Byte-exact contents** in Picture mode
when you need byte-exact evidence and the Picture review gallery. Approximate
results cannot authorize duplicate-removal quarantine. A complete, current
scan may still support an explicit organizer Copy or Move for an Incoming
Files item.

Candidate indexing usually removes nearly all pair comparisons in a diverse
library. A pathological set in which many images share nearly identical
fingerprints can still approach quadratic candidate volume; the scanner bounds
worker queues and reports cancellation, decoder errors, or resource limits as
incomplete instead of silently returning a complete scan.

EXIF Timestamp
--------------

This one is easy. We read the EXIF information of every picture and extract the ``DateTimeOriginal``
tag. If the tag is the same for two pictures, they're considered duplicates.

**Warning:** Modified pictures often keep the same EXIF timestamp, so watch out for false positives
when you use that scan type.

.. _Levenshtein distance: https://en.wikipedia.org/wiki/Levenshtein_distance
.. _get_close_matches: https://docs.python.org/3/library/difflib.html#difflib.get_close_matches
