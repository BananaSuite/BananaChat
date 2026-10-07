# Working on BananaChat

Use Python 3.12 or newer.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: --no-deps --upgrade 'pip>=26.2.1'
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt pytest ruff
.venv/bin/python -m pytest -q      # the whole suite
.venv/bin/python -m ruff check .   # lint
./dev.sh                           # development server on http://127.0.0.1:8000
```

The tests need no Ollama or GPU: an imitation Ollama server
(`tests/app/fake_ollama.py`) answers during the tests. The encrypted-backup
tests also need Git and [age](https://age-encryption.org/) on `PATH`, and the
signed-update tests need `ssh-keygen`.
Node.js is needed for the JavaScript and Markdown regressions. Browser cases
need `requirements-browser.txt` and an installed Playwright Chromium browser.
The complete release check also runs the optional real Docker cases:

```sh
python -m pip install -r requirements-browser.txt
python -m playwright install --with-deps chromium
docker pull mirror.gcr.io/library/python:3.12-slim
docker build -t bananachat-agent:1 compute/sandbox-image
BC_SANDBOX_REQUIRE_DOCKER=1 BC_SANDBOX_REQUIRE_AGENT_IMAGE=1 python -m pytest -q -ra
```

Review skipped cases in that output: Docker requirements are enforced by the
two flags, and browser cases still need a working Chromium installation. The
token-file ownership regression needs root and can run in a disposable root
container. Keep the normal development environment unprivileged.

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
| Compute node and worker daemon | `compute/`, `worker/` |
| Installation, updates, recovery | `banana`, `banana_ops/`, `banana_backup/` |

## Rules of thumb

* Existing installations update themselves with the previous release's
  lifecycle code. Keep `wsgi:app`, `gunicorn.conf.py`, `GET /health`,
  `python -m compute.inference_proxy`, the `BC_*` variable names and the data
  files compatible (`tests/app/test_deployment_contract.py` checks this).
* Database migrations are appended, additive and tested against
  `tests/fixtures/legacy_v4.sql`, a database written by the previous release.
* Add a translation in both `en` and `it`; the test suite fails when the
  catalogs differ. The administrator interface stays English-only.
* Web and compute servers update independently: keep their protocol backward
  compatible or document the upgrade order. The same holds for the worker
  daemon protocol.
* The lifecycle and backup tools are shared with sibling products and pinned
  by manifests; see [CONTRIBUTING](../CONTRIBUTING.md) before editing them.

## Browser check

Keep ordinary pages and settings on the shared 1120 px `--content-width` frame.
Use responsive form grids within it; avoid squeezing settings into a sidebar
or a capped column. Use `page-narrow` only inside an existing page for a
conversation transcript. Authentication uses its dedicated auth layout.
Conversation columns use `--reading-width`. The API playground keeps its
working panes within the shared page frame; agent workspaces use that frame
for their timeline and files too.
Use `section-stack`, `settings-section` and `section-header` for settings and
lists. Reserve enclosed surfaces for previews, results, dialogs and other
distinct work areas. Semantic fieldsets can use `settings-section` too.

Control sizes come from `--control-height` and `--control-compact` in
`app.css`, with `--touch-target` on coarse pointers. Feature styles should
describe their layout instead of overriding shared cards to remove their
borders. Check new layouts in both languages, both themes and at phone widths,
including increased font size and spacing. Preserve palette, contrast and
reduced-motion preferences.

Keep enabled destinations as direct header links. The browser measures the
labels and opens a compact navigation menu only when they no longer fit.
Without JavaScript the links wrap. Use crisp borders and keyboard outlines
for focus; avoid glowing input shadows or animated service notices.

Use native disclosures for optional controls. Validation must open their
enclosing disclosure before focusing an invalid field. Keep primary actions
and everyday controls visible without expanding optional settings.

`scripts/check_browser.py` drives Chromium through sign-in, chatting,
streaming, the model picker and the mobile layout against a temporary
instance (see CONTRIBUTING for how to install Playwright).

## Upgrade check

`scripts/check_upgrade.py` checks out the previous release in a temporary Git
worktree, runs it under Gunicorn and uses it through its pages (setup, a
chat, a share link, an API token, a personality, preferences), then starts
the current code on the same data and checks that everything still works
and that the old "BananaAI" site name is gone. It needs the full Git history
and Playwright; the old release gets its own virtual environment unless
`--old-python` names an interpreter with its requirements. Run it before a
release that changes the database, sessions or configuration.

## Load test

`scripts/load_test.py` starts the real Gunicorn service with an imitation
model server and prints page, API, overload and health-probe measurements;
[capacity](capacity.md) shows a run.
