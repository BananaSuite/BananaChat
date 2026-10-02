# Contributing to BananaChat

The [development guide](docs/development.md) explains how to run and test the
project, and [architecture](docs/architecture.md) the conventions the code follows.

Open an issue to discuss a substantial change, or send a pull request with a focused fix. Describe the problem, the resulting behavior, and the checks you ran. Include screenshots when a visible change needs them. Do not include runtime data, credentials, or generated build output.

Use Python 3.12 or newer, create a virtual environment, and install `requirements.txt` plus `pytest` and `ruff`. Run `python -m pytest -q` and `python -m ruff check .` before sending a change. Tests should exercise behavior and regressions rather than reproduce implementation details. User-facing text needs both an English and an Italian translation.

Keep changes small enough to review. Preserve access checks, CSRF protection, resource limits, and third-party notices. Document new configuration and migration steps. Report security vulnerabilities using [SECURITY.md](SECURITY.md).

BananaVibe may write the first draft of a change a maintainer asked for; a maintainer still reviews, tests and merges it. Keep private prompts out of source and public PR text. Web and compute servers update independently, so preserve API compatibility or document a deliberate migration order. Automatic updates must remain an explicit operator choice.

The lifecycle code, launcher, maintenance tool, and regressions retain a shared-file manifest from the earlier BananaSuite layout. Product identity stays in `banana_ops/product.py`. Run `python scripts/sync_lifecycle.py --check` locally. After reviewing a lifecycle change, run `--record` and include the manifest with the code; CI verifies it. The tool can compare or synchronize another compatible checkout that also contains `banana_ops` and this manifest. Current BananaWiki uses its own `bananawiki/ops` implementation; review equivalent fixes there separately instead of copying the older layout over it. Synchronization refuses unrecorded destination edits and preserves unrelated files.

Encrypted backup tests require Git and age (`apt install age` on Debian/Ubuntu); they use disposable local repositories and dummy credentials. Run `python scripts/sync_backups.py --check` for the recorded `banana_backup` code. After reviewing a change, use `--record` and include the manifest. Use `--write ../OTHER_CHECKOUT` only for a compatible checkout with the same package and manifest, then review each diff. Current BananaWiki keeps its backup implementation in `bananawiki/ops/backups`. The copy refuses unrecorded changes in the destination.

Run the browser regression with a separate optional test dependency:

```sh
python -m pip install -r requirements-browser.txt
python -m playwright install --with-deps chromium
python scripts/check_browser.py
```

This starts a temporary local instance with an imitation model server and checks sign-in with CSRF enabled, chatting and streaming, Markdown escaping, the model picker, and desktop and mobile layouts. It needs no real model server or microphone. The instance is removed afterwards; screenshots and results are saved under `.browser-artifacts/`. CI runs this check and Ruff's correctness rules alongside the Python suite.

Contributions are made under the project's GNU AGPL version 3 license. Contributors retain copyright in their contributions.

Discussions and reviews follow the [code of conduct](CODE_OF_CONDUCT.md).
