# Contributing to BananaChat

Open an issue to discuss a substantial change before writing it, or send a
pull request with a focused fix. Report security vulnerabilities privately as
described in [SECURITY.md](SECURITY.md). Discussions and reviews follow the
[code of conduct](CODE_OF_CONDUCT.md).

## Development setup

You need Python 3.12 or newer and Git. Node.js runs the Markdown renderer
tests, Git and [age](https://age-encryption.org/) the backup tests, and
`ssh-keygen` the signed-update tests (`apt install age openssh-client`).

```sh
git clone https://github.com/BananaSuite/BananaChat.git
cd BananaChat
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt pytest ruff==0.17.1   # the ruff version CI uses
./dev.sh                          # http://127.0.0.1:8000, prints the setup token
```

The tests need no Ollama or GPU. The [development guide](docs/development.md)
covers the optional Docker and browser cases, and
[architecture](docs/architecture.md) the conventions the code follows.

## Tests and linting

```sh
python -m pytest -q
python -m ruff check .
python scripts/sync_lifecycle.py --check
python scripts/sync_backups.py --check
```

CI runs these on Python 3.12 to 3.14 with Node.js required
(`BC_REQUIRE_NODE=1`), plus `pip-audit`, the sandbox tests under Docker and
Podman, and the browser check below. Ruff checks correctness and security
rules only. Fix a security warning (for example, check a URL's scheme before
urllib opens it), or mark a reviewed false positive with
`# noqa: Sxxx - reason` on its line; `ruff.toml` explains the few rules that
are off.

Tests should exercise behaviour, not reproduce implementation details.

### Browser check

```sh
python -m pip install -r requirements-browser.txt
python -m playwright install --with-deps chromium
python scripts/check_browser.py
```

This starts a temporary local instance with an imitation model server and
checks sign-in with CSRF on, chatting and streaming, Markdown escaping, the
model picker, and the desktop and mobile layouts. It needs no real model
server or microphone. Screenshots and results are saved under
`.browser-artifacts/`.

## Rules

* Keep access checks, CSRF protection, resource limits and third-party
  notices intact.
* User-facing text needs an English, an Italian and a German translation.
  The administrator interface is English-only.
* Web and compute servers update independently: keep their protocol backward
  compatible, or document the upgrade order.
* Automatic updates stay an explicit operator choice.
* `banana_ops/` and `banana_backup/` are pinned by manifests. After a reviewed
  change, run `python scripts/sync_lifecycle.py --record` (or
  `sync_backups.py --record`) and commit the updated manifest with the code.
  Product identity stays in `banana_ops/product.py`.
* The top bar mascot is a port of BananaWiki's: BananaWiki's
  `scripts/make_icons.py --check` fails while the version named in
  `static/js/mascot.js` is behind that tree's.
* Document new configuration in `docs/configuration.md` and migration steps in
  `UPGRADING.md` and `CHANGELOG.md`.

## Commits and pull requests

* One topic per pull request; keep it small enough to review.
* Write commit subjects in the imperative ("Fix quota reset for invited
  users"), with a body that explains why when it is not obvious.
* Describe what changed, how you tested it (commands and results), and include
  screenshots for visible changes.
* Do not commit runtime data, credentials or generated build output.
* A maintainer reviews every change before it is merged.

## Licence

Contributions are made under the project's licence, the GNU Affero General
Public License version 3 only (`AGPL-3.0-only`). Contributors keep the
copyright in their contributions.
