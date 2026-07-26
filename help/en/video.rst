Similar video detection
=======================

The desktop application does not currently run the video scanner inside its
Qt review window. Choose **Help > Similar Video CLI Workflow…** to open this
page, then run the commands below in a terminal. The menu item opens
documentation only; it does not claim to start a hidden or unfinished scan.

Video mode uses local ``ffprobe`` and ``ffmpeg`` executables. It samples
normalized timeline positions and scene changes instead of relying on one
fixed opening frame. Per-frame perceptual hashes are aligned as a sequence;
when ``fpcalc`` is available, a Chromaprint audio fingerprint contributes a
second signal.

The report distinguishes related, near-duplicate, transcoded, and trimmed
videos. Missing tools, timeouts, cancellation, decode errors, and configured
resource limits are explicit partial states.

Video similarity is never byte-exact proof. Even a perfect perceptual score
cannot authorize deletion. Use the Verified Exact engine when two video files
must be proven byte-for-byte equal.

Inspect local support with:

.. code-block:: console

    dupeguru video capabilities

Analyze or compare files with:

.. code-block:: console

    dupeguru video analyze movie.mp4
    dupeguru video compare first.mp4 second.mkv

Scan a library with explicit resource bounds and a cache stored outside the
input roots:

.. code-block:: console

    dupeguru video scan Videos --cache video-fingerprints.sqlite3 --max-files 10000 --max-comparisons 2000 --format jsonl > video-groups.jsonl

The library report is review-only and may be partial when a configured bound,
timeout, cancellation, unreadable file, or missing local tool prevents
complete coverage. A cache path inside ``Videos`` is rejected.

Fingerprint artifacts may be saved and reused only when their recorded source
identity and content generation still match the live file. An artifact is
limited to 16 MiB, 32 frame fingerprints, 16,384 audio words, 128 issues, and
16 tool-version records. Nested collections and text fields are rejected at
their schema limits before being copied into the analysis model.
