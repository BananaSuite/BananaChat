# Contributing to BananaChat

For larger changes, please open an issue first so we can discuss the
approach. Small, focused fixes can go straight to a pull request. Report
security vulnerabilities privately as described in [SECURITY.md](SECURITY.md),
and please follow the [code of conduct](CODE_OF_CONDUCT.md) in discussions and
reviews.

## Development setup

You need Python 3.12 or newer and Git. Some tests need more tools: Node.js
for the Markdown renderer tests, Git and [age](https://age-encryption.org/)
for the backup tests, and `ssh-keygen` for the signed-update tests
(`apt install age openssh-client`).

```sh
git clone https://github.com/BananaSuite/BananaChat.git
cd BananaChat
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt pytest ruff==0.17.1   # the ruff version CI uses
./dev.sh                          # http://127.0.0.1:8000, prints the setup token
```

The tests don't need Ollama or a GPU. The
[development guide](docs/development.md) covers the optional Docker and
browser tests, and [architecture](docs/architecture.md) describes the
conventions the code follows.

## Tests and linting

```sh
python -m pytest -q
python -m ruff check .
python scripts/sync_lifecycle.py --check
python scripts/sync_backups.py --check
```

CI runs these on Python 3.12 to 3.14 with Node.js required
(`BC_REQUIRE_NODE=1`). It also runs `pip-audit`, the sandbox tests under
Docker and Podman, and the browser check below. Ruff only checks correctness
and security rules. Fix security warnings (for example, check a URL's scheme
before urllib opens it), or mark a reviewed false positive with
`# noqa: Sxxx - reason` on its line. `ruff.toml` explains the few rules that
are turned off.

Tests should check behaviour rather than reproduce implementation details.

### Browser check

```sh
python -m pip install -r requirements-browser.txt
python -m playwright install --with-deps chromium
python scripts/check_browser.py
```

This starts a temporary local instance with a fake model server and checks
sign-in with CSRF protection, chatting and streaming, Markdown escaping, the
model picker, and the desktop and mobile layouts. It doesn't need a real
model server or a microphone. Screenshots and results are saved in
`.browser-artifacts/`.

## Rules

* Keep access checks, CSRF protection, resource limits and third-party
  notices intact.
* User-facing text needs an English, an Italian and a German translation.
  The administrator interface is English-only.
* Web and compute servers update independently. Keep their protocol backward
  compatible, or document the upgrade order.
* Automatic updates must remain an explicit choice of the operator.
* `banana_ops/` and `banana_backup/` are pinned by manifests. After a reviewed
  change, run `python scripts/sync_lifecycle.py --record` (or
  `sync_backups.py --record`) and commit the updated manifest with the code.
  Product identity stays in `banana_ops/product.py`.
* The top bar mascot is a port of BananaWiki's. BananaWiki's
  `scripts/make_icons.py --check` fails while the version named in
  `static/js/mascot.js` is older than the one in that repository.
* Document new configuration in `docs/configuration.md` and migration steps in
  `UPGRADING.md` and `CHANGELOG.md`.

## Commits and pull requests

* Keep each pull request to one topic, and small enough to review.
* Write commit subjects in the imperative ("Fix quota reset for invited
  users"). Add a body that explains why, if it isn't obvious.
* Describe what changed and how you tested it (commands and results), and
  include screenshots for visible changes.
* Don't commit runtime data, credentials or generated build output.
* A maintainer reviews every change before it's merged.

## Licence

By contributing, you agree that your contributions are licensed under the
project's licence, the GNU Affero General Public License v3.0 only
(`AGPL-3.0-only`). You keep the copyright to your contributions.
