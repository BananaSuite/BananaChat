# Working on BananaChat

Use Python 3.12 or newer.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest ruff
.venv/bin/python -m pytest -q      # the whole suite
.venv/bin/python -m ruff check .   # lint
./dev.sh                           # development server on http://127.0.0.1:8000
```

The tests need no Ollama or GPU: an imitation Ollama server
(`tests/app/fake_ollama.py`) answers during the tests. The encrypted-backup
tests also need Git and [age](https://age-encryption.org/) on `PATH`, and the
signed-update tests need `ssh-keygen`.

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
| Chat | `bananachat/web/chat.py`, `bananachat/services/chat.py`, `templates/chat/`, `static/js/chat.js` |
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
