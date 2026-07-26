# Security policy

dupeGuru Neo performs destructive filesystem operations. Security and data-loss
reports are treated as safety-critical.

## Supported versions

Only the latest tagged dupeGuru Neo release receives security fixes. Historical
upstream dupeGuru releases and untagged third-party builds are outside this
policy.

Before the first tagged release exists, repository commits and local artifacts
are development builds and there is no supported security-release line.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could cause arbitrary file
deletion, command execution, privilege escalation, or disclosure of private
paths or media.

Use GitHub's private vulnerability reporting flow:

https://github.com/AiWithYou/dupeguru_neo/security/advisories/new

If that page does not offer a private report form, private vulnerability
reporting has not been enabled and the repository is not ready to publish a
supported release. Do not put vulnerability details in a public issue,
discussion, pull request, log, or commit. Repository maintainers must enable
the private channel before publishing; an unavailable private channel is not
permission to disclose the report publicly.

Include, when available:

- the exact dupeGuru Neo version and build commit;
- operating system and filesystem type;
- whether the affected path is local, removable, networked, or cloud-synced;
- the smallest safe reproducer;
- whether a file was only scanned, quarantined, restored, or permanently
  removed;
- a redacted operation journal or scan receipt.

Do not attach private media, credentials, raw catalog databases, or unredacted
home-directory paths unless explicitly requested through the private report.

## Supply-chain identity

Canonical source:

https://github.com/AiWithYou/dupeguru_neo

Release artifacts must identify the source commit and publish checksums,
corresponding GPLv3 source, an SBOM, and build provenance. A website or download
that cannot be traced back to the canonical repository is not an official
dupeGuru Neo distribution.

## Safety boundary

The detailed guarantee and threat model are documented in
`docs/SAFETY_MODEL.md`. In particular, a similarity score or cached hash never
authorizes deletion. Destructive actions require current, typed verification
and must fail closed when identity, content, coverage, or policy cannot be
verified.
