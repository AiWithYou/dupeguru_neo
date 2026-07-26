# Third-party notices

dupeGuru Neo as a whole is distributed under the GNU General Public License
version 3; see `LICENSE`. Some incorporated source files retain compatible BSD
terms, and release builds depend on separately maintained Python
distributions. This notice does not replace the license text shipped with any
dependency.

## Incorporated BSD-licensed source

The BSD 3-Clause license reproduced below applies to:

- `hscommon/**` (including code with copyright years from 2011 through 2018);
- `core/pe/modules/block.c`;
- `core/pe/modules/cache.c`;
- `core/pe/modules/common.c`; and
- `qt/pe/modules/block.c`.

The C files listed above carry a 2014 Hardcoded Software copyright notice.
`core/pe/modules/common.h` and `core/pe/modules/block_osx.m` carry GPLv3
headers and are therefore not included in this BSD-path list.

```text
Copyright 2014, Hardcoded Software Inc., http://www.hardcoded.net
All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
    * Neither the name of Hardcoded Software Inc. nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Release runtime dependencies

Tagged builds use the exact versions in `requirements-release.txt`. Wheels and
source distributions declare or constrain these dependencies but do not
redistribute their code. Local portable builds do redistribute the active
platform's installed runtime dependencies and therefore include a generated
`THIRD-PARTY-LICENSES/` directory containing:

- a machine-readable `index.json`;
- a human-readable `index.txt`; and
- the license or notice files discovered from each installed distribution.

The generated index records the exact installed name and version, available
`License-Expression`, legacy `License`, license classifiers, declared
`License-File` entries, copied-file SHA-256 digests, metadata warnings, and the
SHA-256 of `requirements-release.txt`. A local portable build fails if an active
pinned dependency is absent, has a different version, or has no discoverable
license text.

That distribution-level inventory does not identify every native codec or
library that may be compiled into a binary wheel. The current project cannot
prove the complete native source, license, and SBOM closure of a frozen tree.
For that reason, portable and source-companion archives are not official
release assets and both release validators reject them. The local portable
builder and experimental source-set tool remain available for development;
their output must not be described as complete corresponding source. See
`docs/RELEASE.md` and `docs/SOURCE-COMPANION.md`.

The pinned runtime set and the license designation reported by its distribution
metadata at release-lock creation time are:

| Distribution | Version | Reported license |
| --- | ---: | --- |
| distro | 1.9.0 | Apache License 2.0 |
| mutagen | 1.48.1 | GPL-2.0-or-later |
| Pillow | 12.3.0 | MIT-CMU |
| PyQt6 | 6.11.0 | GPL-3.0-only |
| PyQt6-Qt6 | 6.11.1 | LGPL v3 |
| PyQt6_sip | 13.11.1 | BSD-2-Clause |
| semantic-version | 2.10.0 | BSD (its included text is the 2-Clause form) |
| xxhash | 3.8.1 | BSD-2-Clause |
| pywin32 (Windows only) | 312 | PSF and component-specific included terms |

This summary is informational. The generated per-build license inventory and
the copied upstream texts are authoritative for the dependency files actually
redistributed in a portable bundle.

## Frozen interpreter and bootloader notices

Portable bundles also contain CPython interpreter files and PyInstaller
bootloader/loader files that are not runtime requirements in
`requirements-release.txt`. Each bundle therefore includes a separately
verified `FROZEN-RUNTIME-LICENSES/` directory:

- the CPython `LICENSE.txt` copied from the exact frozen CPython 3.12.13
  installation, including the Python Software Foundation License Version 2
  and the historical license terms collected in that file; and
- PyInstaller 6.21.0 `COPYING.txt`, including its GPL-2.0-or-later terms, the
  PyInstaller Bootloader Exception, and the component-specific terms described
  by that upstream notice.

`FROZEN-RUNTIME-LICENSES/index.json` binds both copied texts to their byte
size and SHA-256, the exact component version, the matching source archive in
`release-sources.json`, and the build platform. Its verifier rejects a missing
PSF license marker, a missing PyInstaller Bootloader Exception, a version or
source-lock mismatch, an altered text, a symlink, or an unexpected file. This
inventory reports the upstream terms shipped with the frozen runtime; it is
not a legal conclusion about every possible downstream use.
