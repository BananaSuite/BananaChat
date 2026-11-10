# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private
vulnerability reporting:
<https://github.com/BananaSuite/BananaChat/security/advisories/new>

Include the affected version or commit, the steps to reproduce, the impact,
and a suggested fix if you have one. Don't include real credentials or other
people's data.

If private reporting isn't available, contact
[Luca Zani (OverloadedTech)](https://github.com/OverloadedTech) using the
contact details on their GitHub profile. Only use a public issue to ask for a
private channel, and never post exploit details there.

We'll acknowledge your report as soon as we can, keep you updated while we
work on a fix, and credit you in the release notes unless you'd rather not be
credited.

## Supported versions

| Version | Supported |
|---|---|
| 1.8.x | Yes |
| 1.7 and earlier | No. See [UPGRADING.md](UPGRADING.md) to upgrade to 1.8. |

Security fixes are released as new 1.8 versions with signed tags (`vX.Y.Z`).
Managed servers install them with `sudo bananachat update`, or automatically
if automatic updates are on. We also recommend keeping the operating system
and Python dependencies up to date, and testing your backups before you
upgrade.

## Scope

These components are in scope:

* the web application (`bananachat/`)
* the compute server, including its inference proxy and sandbox runner
  (`compute/`)
* the worker daemon (`worker/`)
* the installation, update and backup tools (`banana`, `banana_ops/`,
  `banana_backup/`)

Administrators are trusted with accounts, models, providers and settings.

We consider these to be vulnerabilities:

* anyone without the administrator role gaining administrator powers
* reading or changing another user's chats, files, API keys or sessions
* code in a cloud session reaching the host, other sessions or the network
  beyond what the [threat model](docs/agents.md#threat-model) allows
* a worker's token reaching anything on the web server other than the jobs
  sent to that worker
* bypassing CSRF protection, sessions, two-step verification, rate limits,
  quotas or the Content Security Policy
* anything that exposes secrets such as API keys, provider keys, pairing
  codes or password hashes

These are out of scope: what a model writes, a worker's owner reading the
requests sent to their worker (see [docs/workers.md](docs/workers.md)), denial
of service through sheer volume, missing hardening headers that don't lead to
an exploit, self-XSS, social engineering, and the limits listed under "What is
not protected" in [docs/agents.md](docs/agents.md#what-is-not-protected).
