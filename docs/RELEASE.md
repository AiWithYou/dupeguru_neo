# Release policy

The tagged-release workflow is the only supported way to publish dupeGuru Neo
artifacts. A local build is useful for testing, but is not a releasable build.

## Release identity

- A release tag must be exactly `v<core.__version__>`.
- `vMAJOR.MINOR.PATCH` is the stable channel. Tags containing a PEP 440
  pre-release or development suffix are pre-releases.
- The tag must resolve to the workflow's original `GITHUB_SHA`.
- The tagged commit must be reachable from `master`.
- Successful `CI` and `CodeQL` push runs must exist for that same SHA on
  `master`.
- `SOURCE_DATE_EPOCH` is mandatory and is read from the commit timestamp.
- Build metadata records the repository, full commit, tag ref, version,
  timestamp, exact Python implementation/version used to assemble the public
  payload, artifact sizes and SHA-256 digests, and the SHA-256 of
  `requirements-release.txt`.

The gate rejects mismatched versions, local or epoch versions, unsafe tag
values, missing commit timestamps, tags that move to a different commit, and
release commits that are not on `master`.

## Published payload

The public release allowlist contains:

1. one canonical pip sdist, built once with the pinned PEP 517 backend;
2. one CPython 3.13.14 wheel for each supported release target:
   Linux x86_64, Windows x86_64, and macOS arm64;
3. `dupeguru-neo-<version>-source.tar.gz`, generated from every tracked object
   in the exact tagged commit;
4. `LICENSE`, `THIRD_PARTY_NOTICES.md`, and
   `HSCOMMON-BSD-3-CLAUSE.txt`;
5. `requirements-release.txt` and `release-sources.json`;
6. the aggregate CycloneDX SBOM and deterministic build metadata;
7. `SHA256SUMS`; and
8. one Sigstore bundle for every checksummed subject.

The allowlist is exact and case-collision safe. An additional log, debug dump,
credential, arbitrary flat file, portable archive, source-companion proof, or
source-companion archive fails the release. A canonical sdist name is required;
another spelling that happens to parse as the same version is rejected.

The workflow performs strict `twine check`, deterministic wheel rebuild, clean
wheel and sdist installs, `dupeguru doctor`, verified-exact scan smoke tests,
and a PyQt6 offscreen import. Each target records its installed runtime closure.
The CycloneDX SBOM is the validated union of the Linux, Windows, and macOS
snapshots, so Windows-only `pywin32` remains visible when metadata is assembled
on Linux.

For macOS native extensions, the build passes `-reproducible` to Apple's
linker, which zeros build-time object modification values in the `N_OSO` debug
map. It also passes `-oso_prefix .`, which strips the build working directory
from those object-file debug-map paths. Both are required because release
wheels and their verification rebuild use different temporary source roots.
It deliberately does not pass `-no_uuid`. Apple documents that the linker's
default `LC_UUID` is hash-based to support reproducible builds and that
removing the build UUID is a bad idea in
[TN3178](https://developer.apple.com/documentation/technotes/tn3178-checking-for-and-resolving-build-uuid-problems).
This keeps each Mach-O image loadable and identifiable without introducing a
random UUID. The rebuild gate still compares the complete wheel byte for byte
and never excludes or normalizes a member after the build. Its UUID gate allows
reuse only for a byte-identical Mach-O copy with the same architecture and
complete member SHA-256; reuse by a different payload or architecture fails.

The tagged source archive is generated from Git objects rather than the mutable
working tree. Its paths, executable modes, symlink targets, sizes, and contents
are revalidated against the tagged tree before publication. The ordinary sdist
is independently checked for its declared rebuild inputs.

## Portable-build publication boundary

CI still builds and smoke-tests the PyInstaller GUI on Ubuntu 24.04,
Windows Server 2022, and the macOS 15 arm64 runner. This preserves local build
coverage. The resulting archives are deliberately not uploaded as workflow
artifacts and are not copied into the release payload.

This is a source-completeness boundary, not a naming preference. Binary Python
wheels may contain native libraries below the Python-distribution level. For
example, a Pillow wheel can contain codecs and rendering libraries such as
libjpeg-turbo, zlib-ng, libtiff, libwebp, OpenJPEG, FreeType, and Little CMS.
Hashing Pillow's installed `RECORD` proves which bytes were observed; mapping
the Pillow distribution to the Pillow sdist does not by itself prove the
version, license, and corresponding source of every library compiled into
those bytes.

The current installed-distribution inventory and `release-sources.json` do not
yet provide that complete component-level native closure for every operating
system. The project therefore makes no claim that a frozen portable has
complete corresponding source or a complete native SBOM.

Two independent gates enforce the boundary:

- `scripts/release_metadata.py verify-payload --payload-kind release` rejects
  portable names, source-companion assets, and every file outside the exact
  public allowlist.
- `scripts/portable_bundle.py enforce-release-policy` rejects portable or
  native payloads by filename, executable magic, and archive contents,
  including renamed or nested ZIP/TAR bundles.

`scripts/portable_bundle.py build` and `verify` remain available for local
testing. `scripts/source_companion.py` remains an experimental local research
tool for the Python-distribution-level source set. Its local proof is not
signed by the release workflow, is not an official release asset, and must not
be described as complete corresponding source for a frozen executable.

Portable verification is fail-closed before accepting archive contents. The
input archive is limited to 512 MiB, 100,000 members, 4,096 UTF-8 bytes per
member name, 256 MiB per regular member, 1 GiB total declared uncompressed
payload, and a 200:1 per-member and whole-archive compression ratio. ZIP central
directories are limited to 16 MiB before `ZipInfo` objects are materialized,
and the complete decompressed TAR stream, including PAX metadata and padding,
is limited to 2 GiB. Individual PAX and GNU long-name metadata records are
limited to 16 MiB, and physical TAR metadata records count toward the same
100,000-member ceiling. These limits leave substantial headroom over the
locally smoke-tested PyInstaller bundles while bounding malformed archives and
decompression bombs. Every logical member is included in the count, including
zero-size directories and links.

Member names are also checked against the extraction semantics of the named
target platform. Windows archives reject case-insensitive collisions, trailing
dots or spaces, device names, alternate-data-stream syntax, control characters,
and other Win32-forbidden characters. macOS archives reject collisions after
Unicode canonical decomposition and case folding. Linux archives retain
case-sensitive POSIX names. All targets reject exact duplicates, unsafe POSIX
paths, and file-versus-directory topology conflicts; ZIP special-file entries
and escaping TAR links fail verification.

Publishing a portable in the future requires all of the following:

- a bounded inventory of every bundled native executable and shared library;
- an unambiguous provider, version, license, source URL, byte size, and SHA-256
  for each component;
- complete license texts and corresponding source in a proof-bound source set;
- component-level CycloneDX entries tied to the inventoried bytes;
- reproducible rebuild evidence or an equally strong documented provenance
  chain;
- Windows Authenticode and macOS Developer ID/notarization gates if the asset
  is presented as platform-trusted; and
- an intentional update to both independent release allowlists.

Missing evidence fails closed. A missing signing credential or native-source
mapping never silently downgrades into a publicly downloadable "unsigned
portable."

## Dependency and provenance limits

`requirements-release.txt` is an exact version constraint and is hashed into
the release metadata. It is not a pip `--require-hashes` lock and does not
authenticate a package-index wheel before installation.

After installation, dependency snapshots record installed versions. The local
portable builder additionally records each distribution's `RECORD` digest and
an independent SHA-256 manifest of installed files. These are post-install
provenance controls, not hash-before-install authentication and not
component-level analysis of native code embedded inside a wheel.

`release-sources.json` size- and hash-pins the upstream source archives it
names. It does not imply that an unlisted transitive native component is
covered. A future pre-install guarantee requires a complete per-platform
hashed wheelhouse or an equivalent `--require-hashes` input.

## Publication protection and TOCTOU control

Stable releases use the `stable-release` GitHub environment. Pre-releases use
`prerelease`. Each environment must:

- have at least one required reviewer;
- prevent self-review; and
- disallow administrator bypass.

Repository immutable releases, Issues, and private vulnerability reporting
must also be enabled.

`scripts/release_publication_gate.py` reads the GitHub API with strict UTF-8
JSON parsing, duplicate-key rejection, bounded responses, and exact Boolean
checks. It verifies the environment, repository settings, private
vulnerability reporting, tag target, `master` ancestry, and successful `CI`
and `CodeQL` push runs for the release SHA. Its final-only mode also reads the
draft release by tag, binds the separately fetched asset list to that release's
numeric identity, and requires the expected draft/pre-release state. Every
remote asset must be completely uploaded, uniquely named without
case-insensitive collisions, and have exactly the same name, byte size, and
GitHub-reported `sha256:` digest as every regular non-symlink file in the local
`dist` directory. Missing, extra, still-uploading, or changed assets fail
closed.

The publish job executes the same gate:

1. after protected-environment approval, before it downloads release inputs;
2. inside the stable publication shell step immediately before
   `gh release edit --draft=false`; or
3. inside the pre-release publication shell step immediately before the
   equivalent edit.

The second invocation inventories the local payload, re-fetches every mutable
prerequisite, and then fetches the exact remote draft and its asset set last.
It is not a reduced ancestry-only check. Putting the two draft API reads after
the other remote checks keeps the most directly publishable state closest to
the edit. If an administrator weakens an environment, turns off immutable
releases, Issues, or private vulnerability reporting, moves the tag, changes
`master` ancestry, invalidates prerequisite CI, or mutates the observed draft
while the gate is running, a detected mismatch stops publication and leaves
the release as a non-public draft.

The release is first created as a draft after another direct tag-resolution
check. Only the final edit makes it public. Immutable releases then prevent
the published tag and assets from being changed.

GitHub does not expose an atomic "publish only if this earlier release and
repository snapshot is unchanged" operation. Each API response is a
point-in-time observation and can become stale immediately after its individual
read. This includes the interval between the release-by-tag response and the
separate assets-by-release-ID response, as well as the interval from each
earlier repository, tag, ancestry, environment, or workflow response to
`gh release edit --draft=false`. Under healthy API conditions these intervals
are normally short, but they are not atomically bounded; an individual API call
also has a 60-second timeout. Keeping all checks and the edit in one fail-fast
shell step, and reading the draft assets last, minimizes exposure but does not
claim an absolute concurrency guarantee. Protected-environment access control
and immutable releases remain required defense in depth.

All intermediate release artifacts are retained for 30 days so a legitimate
protected-environment approval wait does not outlive its inputs. Pull-request
workflows have read-only repository permissions and receive no release secret.
Write, OIDC, and attestation permissions are limited to signing and publishing
jobs.

## Signatures and attestations

`SHA256SUMS` covers the complete ordinary release payload before signature
sidecars are added. GitHub SLSA provenance and a CycloneDX SBOM attestation are
generated for every checksummed subject. A keyless Sigstore bundle is generated
for every published file and immediately verified against:

- `https://github.com/AiWithYou/dupeguru_neo/.github/workflows/release.yml@refs/tags/<tag>`;
  and
- the GitHub Actions OIDC issuer
  `https://token.actions.githubusercontent.com`.

The payload validator requires a one-to-one mapping between subjects and
`<subject>.sigstore.json` bundles. Missing or orphaned bundles,
case-insensitive filename collisions, pre-signing bundles, top-level private
key or credential suffixes, and altered checksums are rejected. The signed
payload is downloaded and verified again in the protected publish job.

## Consumer verification

Download one release's files into an otherwise empty `dist` directory. Verify
the exact checksum domain:

```console
python scripts/release_metadata.py verify-checksums \
  --directory dist \
  --checksums dist/SHA256SUMS
```

Verify GitHub build provenance for each checksummed artifact:

```console
gh attestation verify dist/ARTIFACT_NAME \
  --repo AiWithYou/dupeguru_neo \
  --signer-workflow AiWithYou/dupeguru_neo/.github/workflows/release.yml
```

Verify the corresponding `<artifact>.sigstore.json` against the exact workflow
identity and GitHub Actions OIDC issuer above. Do not trust a similarly named
portable or source-companion file from another download location: neither is
part of the official release contract.

## Maintainer checklist

1. Confirm the version in `core/__init__.py`.
2. Confirm `CI` and `CodeQL` succeeded for the intended SHA on `master`.
3. Confirm both protected environments, immutable releases, Issues, and
   private vulnerability reporting are configured.
4. Create and push the matching tag.
5. Approve the protected environment.
6. Confirm the draft contains exactly the three CPython 3.13 wheels, canonical
   sdist, tagged source archive, notices, locks, CycloneDX SBOM,
   `BUILD-METADATA.json`, `SHA256SUMS`, and one Sigstore bundle per subject.
7. Confirm that no portable, installer, source-companion file, log, or debug
   dump is present.
8. Independently verify SHA-256, GitHub attestations, and Sigstore identities
   after publication.
