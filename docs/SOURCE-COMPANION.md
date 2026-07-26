# Experimental portable source-set tooling

`scripts/source_companion.py` can build a deterministic local source set for
research and license review. It is not used by the tagged-release workflow,
its proof is not signed by that workflow, and its archive is not an official
release asset.

The distinction is deliberate. The current tool can collect:

- the exact dupeGuru Neo tagged source archive;
- source archives named in `release-sources.json`;
- the release dependency and upstream-source locks;
- CPython, PyInstaller, Qt, GPL, BSD, and project notices known to those locks;
- the installed Python-distribution license inventories extracted from local
  Linux, Windows, and macOS portable archives; and
- a deterministic manifest and content-addressed proof for those files.

Each portable bundle's `THIRD-PARTY-LICENSES/index.json` binds an installed
Python distribution to one provider in `release-sources.json`. It records the
distribution name and version, `.dist-info/RECORD` digest, and a bounded
manifest over the installed files named by that distribution. Local build and
verification reject missing files, path escapes, links or junctions, size and
count overruns, content changes, ambiguous `RECORD` files, and source-provider
mapping errors.

This is useful post-install provenance at the Python-distribution level. It is
not proof of the complete native component closure of a frozen executable.
A binary wheel can compile or bundle native libraries that do not appear as
independent Python distributions. Pillow codecs and rendering dependencies are
one concrete example. Mapping a Pillow wheel's files to the Pillow sdist does
not establish the version, license, or source of every library compiled into
those files.

Consequently, this local archive must not be called "Complete Corresponding
Source" for a portable executable. It must not be uploaded beside an official
release. `scripts/release_metadata.py` and
`scripts/portable_bundle.py enforce-release-policy` provide independent
boundaries: the first enforces the exact top-level release allowlist, while the
second rejects a source-companion identity or content even when its archive is
renamed or nested inside an otherwise allowed artifact.

## What a publishable companion still requires

Before this tool can support a public portable release, it must additionally:

1. enumerate every PE, ELF, Mach-O, shared library, static component, and
   embedded native codec in each frozen tree under explicit resource limits;
2. identify each component without relying only on its containing Python
   distribution;
3. bind its exact version and license to the observed bytes;
4. pin the corresponding source URL, size, and SHA-256;
5. include all required source and license texts;
6. add component-level CycloneDX entries tied to the byte inventory;
7. prove that the source set covers every native component on all three target
   operating systems; and
8. fail before publication when any provider is unknown or ambiguous.

No partial native inventory may be treated as complete.

## Remaining provenance limit

`requirements-release.txt` pins versions but is not a complete,
platform-specific hash lock for every transitive wheel. An installed-file
manifest cannot prove that a download was authenticated before installation or
that a wheel was built from a particular source archive.

Closing that separate gap requires either a fully enumerated hash-locked
wheelhouse for every supported Python, OS, and architecture combination or
reproducible builds from verified source archives. Partial or inferred wheel
hashes must not be presented as a pre-install guarantee.

## Local rebuild outline

1. Install CPython 3.12.13.
2. Install dependencies under `requirements-release.txt` and the pinned build
   tools.
3. Run `python build.py --clean`.
4. Run `python scripts/portable_bundle.py build ...` independently on Linux,
   Windows, and macOS.
5. Verify each local archive with
   `python scripts/portable_bundle.py verify --archive ...`.
6. Use `scripts/source_companion.py` only as an experimental inspection tool.

The source-set builder verifies exactly one local portable archive for each
platform before reading its license inventory. That local validation does not
override the official publication ban.
