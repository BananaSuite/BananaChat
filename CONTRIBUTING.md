# Contributing to BananaChat

The [development guide](docs/development.md) maps chat, inference, storage, and
browser changes to their modules.

Open an issue to discuss a substantial change, or send a pull request with a focused fix. Describe the problem, the resulting behavior, and the checks you ran. Include screenshots when a visible change needs them. Do not include runtime data, credentials, or generated build output.

Use Python 3.12 or newer, create a virtual environment, and install `requirements.txt`. Install `pytest` to run the tests with `python -m pytest`. Tests should exercise behavior and regressions rather than reproduce implementation details.

Keep changes small enough to review. Preserve access checks, CSRF protection, resource limits, and third-party notices. Document new configuration and migration steps. Report security vulnerabilities using [SECURITY.md](SECURITY.md).

BananaVibe may write the first draft of a change a maintainer asked for; a maintainer still reviews, tests and merges it. Keep private prompts out of source and public PR text. Web and compute servers update independently, so preserve API compatibility or document a deliberate migration order. Automatic updates must remain an explicit operator choice.

The common lifecycle code, launcher, maintenance tool, and regressions are shared with BananaWiki. Product identity stays in `banana_ops/product.py`. Run `python scripts/sync_lifecycle.py --check` locally, or add `../BananaWiki` to compare both checkouts. After reviewing a common change, run `--record`, then `--write ../BananaWiki`, review both Git diffs, and run lifecycle tests in both repositories. The tool refuses unrecorded destination edits and preserves unrelated files. Commit the shared-file manifest with the code; CI verifies it.

Encrypted backup tests require Git and age (`apt install age` on Debian/Ubuntu); they use disposable local repositories and dummy credentials. Run `python scripts/sync_backups.py --check` for the backup code shared by all three applications. After reviewing a shared change, use `--record` and `--write ../OTHER_CHECKOUT`, review each diff, and commit the manifests with the implementation. The copy refuses unrecorded changes in the destination.

Run the browser regression with a separate optional test dependency:

```sh
python -m pip install -r requirements-browser.txt
python -m playwright install --with-deps chromium
python scripts/check_browser.py
```

This creates a temporary local app with fixture users and chats. It checks desktop and mobile layouts, real login with CSRF enabled, private-chat access, Markdown escaping, source links, simulated voice callbacks, declining model downloads after restore, and continuing a chat when its model fails. It needs no model server or microphone. The app and its data are removed afterward; screenshots and results are saved under `.browser-artifacts/`. CI runs this check and Ruff's undefined-name checks alongside the Python suite.

Contributions are made under the project's GNU AGPL version 3 license. Contributors retain copyright in their contributions.

Discussions and reviews follow the [code of conduct](CODE_OF_CONDUCT.md).
