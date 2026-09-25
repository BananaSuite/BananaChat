# Working on BananaChat

Use Python 3.12 or newer. Install `requirements.txt` and `pytest` in a virtual
environment, then run `python -m pytest -q`. Tests use temporary databases and
local inference fixtures; they do not require an Ollama server.
Encrypted backup tests also need Git and age available on PATH.

`make help` lists the shortcuts. `make dev` runs `./dev.sh`, which creates
`.venv` if it is missing and starts the development server on port 8000.
`make start` runs Gunicorn with `gunicorn.conf.py`. `make test` installs
`pytest` and runs the suite. `make clean` removes `.venv`, the caches and the
compiled files. The managed installer, `./banana`, is covered in
[deployment](deployment.md).

| Area | Start here |
| --- | --- |
| Chat HTTP routes and access checks | `routes/chat.py` |
| Generation, streaming, and cancellation | `services/chat_execution.py` |
| Model access and selection | `services/model_access.py`, `services/ollama.py` |
| Durable conversations | `db/_chat.py`, `db/_chat_runs.py` |
| Compute worker | `compute/`, `worker/` |
| Installation, updates, and recovery | `banana_ops/` |
| Chat markup | `app/templates/chat/session.html`, `_sidebar.html`, `_composer.html` |
| Browser behavior | `app/static/js/chat.js`, `models.js`, `chat-controls.js` |
| Shared dialogs, rendering, and customization | `app/static/js/main.js`, `markdown.js`, `customization.js` |

The base template loads browser scripts in order. They share the existing
global functions used by page scripts; `init.js` registers startup handlers.
Keep English and Italian labels together in `i18n.py`. Desktop and mobile
navigation use the same `_navigation_links.html` partial.

When changing chat controls, run the Chromium check described in
[CONTRIBUTING.md](../CONTRIBUTING.md). It exercises login, streaming recovery,
model fallback, speech controls, and history paging at desktop and phone widths.

Web and compute roles update independently. Keep their API compatible when
changing worker messages, or document the required upgrade order. Shared
lifecycle and backup files have manifests; follow the synchronization steps
in the contributor guide when editing them.
