# Working on BananaChat

Set up a virtual environment as described in
[CONTRIBUTING](../CONTRIBUTING.md#development-setup), then:

```sh
python -m pytest -q      # the whole suite
python -m ruff check .   # lint
./dev.sh                 # development server on http://127.0.0.1:8000
```

The tests need no Ollama or GPU: an imitation Ollama server
(`tests/app/fake_ollama.py`) answers during the tests. The encrypted-backup
tests also need Git and [age](https://age-encryption.org/) on `PATH`, and the
signed-update tests need `ssh-keygen`.
Node.js runs the Markdown renderer tests. Browser cases need
`requirements-browser.txt` and an installed Playwright Chromium browser.
The complete release check also runs the optional real Docker cases:

```sh
python -m pip install -r requirements-browser.txt
python -m playwright install --with-deps chromium
docker pull mirror.gcr.io/library/python:3.12-slim
docker build -t bananachat-agent:1 compute/sandbox-image
BC_SANDBOX_REQUIRE_DOCKER=1 BC_SANDBOX_REQUIRE_AGENT_IMAGE=1 python -m pytest -q -ra
```

Review skipped cases in that output: Docker requirements are enforced by the
two flags, and browser cases still need a working Chromium installation. One
token-file ownership test needs root; it is skipped otherwise. Keep the normal
development environment unprivileged.

`make help` lists shortcuts (`make dev`, `make start`, `make test`,
`make lint`, `make clean`).

Read [architecture](architecture.md) before changing code: it describes the
layout, the storage and request conventions, the Content-Security-Policy
rules for templates and scripts, translations and the inference pipeline.

| Area | Start here |
| --- | --- |
| Application factory, security headers, error pages | `bananachat/app.py` |
| Sessions, CSRF, throttling, passwords | `bananachat/security.py` |
| Configuration | `bananachat/config.py`, [configuration](configuration.md) |
| Database and migrations | `bananachat/db/`, `bananachat/db/migrations/` |
| Inference pipeline, queue, model access | `bananachat/services/inference.py`, `queue.py`, `access.py` |
| Chat | `bananachat/web/chat.py`, `bananachat/services/chat.py`, `templates/chat/`, `static/js/chat.js`, `static/js/chat-pickers.js`, `static/js/chat-personas.js` |
| API and playground | `bananachat/web/api_v1.py`, `bananachat/web/developer.py` |
| Administration | `bananachat/web/admin/`, `templates/admin/` |
| Styles and shared browser helpers | `static/css/app.css`, `static/js/core.js` |
| Translations | `bananachat/i18n/*.json` |
| Compute server and worker daemon | `compute/`, `worker/` |
| Installation, updates, recovery | `banana`, `banana_ops/`, `banana_backup/` |

## Rules of thumb

* An installation updates itself with the lifecycle code of the release it
  runs. Keep `wsgi:app`, `gunicorn.conf.py`, `GET /health`,
  `python -m compute.inference_proxy`, the `BC_*` variable names and the data
  files compatible (`tests/app/test_deployment_contract.py` checks this).
* Database migrations are appended, additive and tested against
  `tests/fixtures/legacy_v4.sql`, a database written by the code from before
  the rewrite.
* Add a translation in `en`, `it` and `de`; the test suite fails when the
  catalogues differ. The administrator interface stays English-only.
* Web and compute servers update independently: keep their protocol backward
  compatible or document the upgrade order. The same holds for the worker
  daemon protocol.
* The lifecycle and backup tools are shared with sibling products and pinned
  by manifests; see [CONTRIBUTING](../CONTRIBUTING.md) before editing them.

## Interface conventions

* Pages and settings use the shared 1120 px `--content-width` frame with
  responsive form grids inside it; conversation columns use `--reading-width`.
* Settings and lists are built from `section-stack`, `settings-section` and
  `section-header`.
* Control sizes come from `--control-height` and `--control-compact` in
  `app.css` (`--touch-target` on coarse pointers); do not override shared
  components to resize them.
* Optional controls go in native disclosures; validation opens the enclosing
  disclosure before focusing an invalid field.
* Check new layouts in every language, both themes and at phone width, also
  with larger text.

## Browser check

`scripts/check_browser.py` drives Chromium through sign-in, chatting,
streaming, the model picker and the mobile layout against a temporary
instance (see CONTRIBUTING for how to install Playwright).

## Upgrade check

`scripts/check_upgrade.py` checks out each release a server can upgrade from
(1.7.0 and the last 1.6 commit, listed by commit in `PREVIOUS_RELEASES`) in a
temporary Git worktree, runs it under Gunicorn and uses it through its pages
(setup, a chat, a share link, an API token, a personality, preferences), then
starts the current code on the same data and checks that everything still
works. `--from REVISION` (repeatable) checks other revisions instead; the code
from before the rewrite is set up with its old "BananaAI" site name, which the
upgrade must replace. It needs Playwright and a clone with the full history.
Each old release gets its own virtual environment unless `--old-python` names
an interpreter with its requirements. Run it before a release that changes the
database, sessions or configuration, and add the new release to
`PREVIOUS_RELEASES` once it is published.

## Load test

`scripts/load_test.py` starts the real Gunicorn service with an imitation
model server and prints page, API, overload and health-probe measurements;
[capacity](capacity.md) shows a run.
