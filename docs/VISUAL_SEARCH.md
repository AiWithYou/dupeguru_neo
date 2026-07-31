# Visual search evidence and limits

dupeGuru Neo's visual search is a bounded, review-only candidate system. It
does not prove byte identity, and none of its relations can enable deletion,
quarantine, or another destructive action. Only the separate Verified Exact
engine can produce destructive eligibility, after live revalidation.

## Implemented feature pipeline

Every supported image is decoded through one versioned Pillow normalization
policy:

1. read the first frame only;
2. apply EXIF orientation;
3. convert an embedded ICC profile to sRGB, or explicitly assume sRGB;
4. composite alpha onto opaque white;
5. compute a 64-bit DCT pHash for bounded MultiIndex candidate lookup;
6. compute a 64-bit dHash and a fixed 4×4×4 color histogram for conservative
   post-index filtering and ranking;
7. compute at most four center/content tile fingerprints, each with a
   normalized source rectangle, for crop and letterbox candidates; and
8. load cached 15×15 RGB blocks only for the bounded candidate batch selected
   for refinement.

The SQLite schema is version 6. Every row is bound to physical file identity,
size, nanosecond mtime, and a versioned content-generation token (Windows
volume USN-journal identifier plus file USN, or POSIX ctime). Windows has no
timestamp fallback: an unavailable/disabled journal or a filesystem/share
without the required USN controls prevents cache reuse and feature
publication. The existing `phashes` BLOB stores a strict, size-bounded,
versioned JSON feature payload. SQLite
`typeof()`/`length()` probes reject oversized values before payload retrieval;
the JSON lexer then enforces depth, node, scalar, and string budgets before
decoding. Old raw pHash blobs, unknown payload versions, duplicate keys,
non-canonical fingerprints, incompatible decoder policies, non-finite values,
and malformed fields are rejected and regenerated; they are never silently
reused. The matching cache stores thumbnail dimensions and a bounded
pixel-derived identity key, but it neither PNG-encodes nor persists thumbnail
image bytes.

On-disk picture caches also carry the dedicated SQLite application ID `DGPE`.
It is checked directly in the 100-byte SQLite header before SQLite connects,
then checked again through the read-only connection. A shape-compatible
foreign database, an unmarked legacy v3/v4 cache, or a modified marker is
rejected without migration or writes. New GUI caches use the versioned
`cached_pictures_v5.db` filename so an existing owned version-5 cache can be
found. A strictly validated writable version-5 cache is discarded, rebuilt as
version 6, and compacted; this reclaims its old thumbnail BLOB pages instead of
copying obsolete payloads forward. Unmarked version-3/version-4 caches and
modified version-5 databases remain untouched and are rejected. “Clear picture
cache” removes rows only from an owned database and then compacts it to return
the freed disk capacity; it never removes the database file and refuses
pre-existing SQLite journal/WAL/shared-memory sidecars rather than recovering
or deleting unknown data.

Portable visual artifacts use schema version 4 and are capped at 512 KiB per
artifact. Their UTF-8 JSON is structurally preflighted before object allocation,
rejects duplicate keys and non-finite numbers, and requires exact nested
fields, scalar types, collection shapes, and fixed-width lowercase
fingerprints. Artifact schema version and cache schema version are independent.

## Relation meanings

- `similar`: whole-image fingerprints and the 15×15 comparison met the
  configured threshold.
- `transformed`: the evidence indicates an orientation or size transform. It
  remains approximate and review-only.
- `crop_candidate`: a whole/tile fingerprint pair passed the bounded pHash,
  dHash, and color checks. The report includes both normalized rectangles and
  declares `crop_verification: bounded_fingerprint_candidate`.
- `related`: a bounded candidate remained below the whole-image block
  threshold.

`crop_candidate` intentionally does **not** claim spatially verified crop
alignment. The current refinement cache contains whole-image RGB blocks, not
tile-to-source feature correspondences. A future crop verifier would need to
store or recompute bounded tile descriptors and prove an alignment before a
stronger label could be introduced. Even that stronger visual label would not
be byte-exact deletion proof.

## Resource and filesystem boundaries

Image count, candidate pairs, refined pairs, emitted evidence, total runtime,
per-image decoded pixels, tile count, cache payload size, and refinement batch
size all have finite limits. Hitting a limit produces a partial
`resource_limit` receipt with destructive actions disabled.

The Qt thumbnail gallery uses a dedicated pool of three workers. Each worker
opens sources through the no-follow, stable-generation filesystem adapter,
limits encoded input to 64 MiB, rejects images above 64 million source pixels,
sets a 128 MiB Qt image allocation limit, and requests decoder-side scaled
output. Both source reads and persistent PNG cache reads compare the generation
observed from the same open handle before and after use. The queue retains at
most 64 requests, and clearing a gallery invalidates all in-flight
publications. Cache, decode, generation, or cancellation failures still
publish a terminal null result so pending rows cannot remain stuck.

The full-size comparison surface applies an independent display boundary. It
reads at most 128 MiB of encoded data through the same no-follow,
stable-generation adapter, rejects source dimensions above 32,768 pixels per
axis or 64 million pixels total, and temporarily lowers Qt's process-wide
decoder allocation limit to 64 MiB under a lock. Each side is requested at no
more than 1,600×1,600 and two million display pixels. A decoder that ignores
the scaled-size request is rejected; its full-size result is never accepted and
scaled after the fact.

Persistent thumbnail directories are private application state. On POSIX, the
application-created hierarchy must be owned by the effective user and must not
be group- or world-writable; load, store, and cleanup all fail closed when that
contract is violated. Entries remain single-link, no-follow regular files, and
the PNG is fully encoded in memory before the final path is touched. Publication
uses create-only `O_EXCL`/`NewOnly` semantics: an existing target or a target
that wins a last-moment creation race is reused or rejected and is never
replaced. The opened output and published path are then bound by file identity,
size, and stable generation checks. On Windows, the cache uses Qt's per-user
cache location and rejects UNC/remote-or-unknown roots, reparse points, hard
links, and identity or generation races. It does **not** parse or repair Windows
DACLs: administrators and custom-cache users must provision the selected
directory with an ACL that does not grant write access to untrusted principals.

## Deliberately out of scope

This implementation adds no OpenCV, ORB, neural model, Torch, CLIP, DINO, or
HNSW dependency. Semantic embedding similarity is a different relation from
duplication and must never become an automatic deletion criterion. Verified
spatial crop alignment and model-based semantic search remain future,
separately reviewed work.
