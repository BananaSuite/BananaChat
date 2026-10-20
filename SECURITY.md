# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's private vulnerability
reporting:
<https://github.com/BananaSuite/BananaChat/security/advisories/new>

Include the affected version or commit, the steps to reproduce, the impact,
and a suggested fix if you have one. Do not include real credentials or other
people's data.

If private reporting is unavailable, contact
[Luca Zani (OverloadedTech)](https://github.com/OverloadedTech) through the
contact details on that profile. Use a public issue only to ask for a private
channel; never publish exploit details there.

We acknowledge reports as soon as we can, keep you informed while we work on
a fix, and credit you in the release notes unless you prefer otherwise.

## Supported versions

| Version | Supported |
|---|---|
| 1.7.x | Yes |
| 1.6 and earlier | No: upgrade to 1.7 ([UPGRADING.md](UPGRADING.md)) |

Security fixes ship as a new 1.7 release with a signed tag (`vX.Y.Z`).
Managed servers install it with `sudo bananachat update`, or by themselves
when automatic updates are on. Keep the operating system and Python
dependencies up to date, and test your backups before upgrading.

## Scope

In scope: the web application (`bananachat/`), the compute server and its
inference proxy and sandbox runner (`compute/`), the worker daemon
(`worker/`), and the installation, update and backup tools (`banana`,
`banana_ops/`, `banana_backup/`).

Administrators are trusted with accounts, models, providers and settings.
These are vulnerabilities:

* anyone without the administrator role reaching administrator powers;
* reading or changing another user's chats, files, API keys or sessions;
* code in a cloud session reaching the host, other sessions or the network
  beyond what the [threat model](docs/agents.md#threat-model) allows;
* a worker's token reaching anything on the web server other than the jobs
  sent to that worker;
* bypassing CSRF, sessions, two-step verification, rate limits, quotas or the
  Content Security Policy;
* anything that exposes secrets (API keys, provider keys, pairing codes,
  password hashes).

Out of scope: what a model writes, a worker's owner reading the requests
sent to that worker ([docs/workers.md](docs/workers.md)), denial of service by
volume, missing hardening headers that do not lead to an exploit, self-XSS,
social engineering, and the limits listed under "What is not protected" in
[docs/agents.md](docs/agents.md#what-is-not-protected).
