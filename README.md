<img src="bananachat/static/img/apple-touch-icon.png" alt="BananaChat logo" width="90" height="90">

# BananaChat

**BananaChat** is a self-hosted chat application for [Ollama](https://ollama.com/). Run open models on your own hardware and give a group of people a private chat interface with accounts, quotas and administration.

- **Chat** with streaming answers, reasoning display, Markdown and code rendering, file attachments (images, PDFs, text and code), model picker, sampling options, personalities (emoji avatar, greeting, conversation starters, preferred model and response style; a default for new chats, templates, share links and JSON import/export), sharing links, search, exports and no-history chats.
- **OpenAI-compatible API** (`/v1/chat/completions`, `/v1/models`, `/v1/images/generations`) with personal keys, a playground and per-account token limits (a single five-hour allowance, optional weekly limits, per-model limits and reasoning-effort levels).
- **External API providers**: Anthropic, OpenAI, OpenRouter and other OpenAI-compatible services, with ready-made presets (also for Google Gemini, Mistral, Groq, DeepSeek, xAI, Together, Fireworks, Azure OpenAI and local servers such as LM Studio, vLLM, llama.cpp and LiteLLM). Admins test the connection, choose models from each provider's own list and keep keys encrypted on the server; limits use the provider's token counts. See [setup](docs/external-providers.md).
- **Cloud sessions** for coding work in isolated sandboxes, with live logs, files, follow-ups and downloadable patches. Disabled by default; admins choose administrators only, everyone signed in, or existing access lists. See [setup and limits](docs/agents.md).
- **Sign-in security**: optional two-step verification for every account (authenticator app codes and one-time recovery codes), which administrators can require of all administrators; high-impact administrator actions ask for the password again. See [two-step verification](docs/two-step-verification.md).
- **Administration**: users and invitations, model catalogue and downloads, fine-grained access policies (per model, per category, uncensored models, image generation, personalities) with user requests, featured personalities published for everyone and personality moderation, quotas, metrics, audit log, maintenance mode and theming.
- **Scales out**: split the web server from a GPU compute node, add volunteer worker PCs, and generate images with [ComfyUI](docs/comfyui.md).
- **Operations**: one command installs, updates (with automatic rollback), backs up (optionally encrypted to a private Git repository) and restores a server.
- Italian and English interface, light and dark themes, accessibility options.

## Try it locally

You need Python 3.12 or newer and Ollama with at least one model:

```sh
ollama pull qwen3:4b
./dev.sh
```

Open <http://127.0.0.1:8000>. The installation token for creating the first administrator is printed in the terminal. The development server listens on loopback only.

To run the tests, `make test` creates a virtual environment and runs the suite; `make lint` runs ruff. By hand:

```sh
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements.txt pytest pytest-xdist ruff
python -m pytest -q -n 4
ruff check .
```

Two tools outside Python are used. The encrypted backup tests need the [`age`](https://github.com/FiloSottile/age) binary and fail with a message when it is missing. The Markdown renderer tests run with Node.js and are skipped without it.

## Install on a server

On Linux with systemd, the usual layout is two servers: a GPU server that runs the models and a web server (a VPS is enough) that keeps accounts and chats. Install from the latest release tag (`v1.7.0` at the time of writing; `git tag --sort=-v:refname | head -n 1` shows the newest). On the GPU server:

```sh
git clone https://github.com/BananaSuite/BananaChat.git
cd BananaChat
git checkout v1.7.0
sudo ./banana install --mode compute --domain compute.example.org
sudo bananachat proxy --install
```

It prints a pairing code (keep it secret). On the web server, the same release checkout and:

```sh
sudo ./banana install --domain chat.example.org --pair
sudo bananachat proxy --install
```

`--pair` asks for the code, shows the address in it for confirmation and tests the connection before installing. An Ollama that already runs on the GPU server can be kept with `--ollama-url http://127.0.0.1:11434`; `--mode single` runs everything on one machine. Updates are **off by default** and follow release tags: run `sudo bananachat update` when you want one, or opt in to automatic updates with `sudo bananachat updates enable --require-signatures ALLOWED_SIGNERS`. Every update backs up the installation first and rolls back automatically if the new version does not start. See [deployment](docs/deployment.md) and [backups](docs/backups.md).

### Upgrading from the previous release

Run `sudo bananachat update` as usual. Servers installed before 1.7 keep following their branch; switch them to release tags with `sudo bananachat source set --track tags`. An update keeps your database, settings, accounts, chats, API tokens and sign-ins (on HTTPS servers, the update to 1.7 keeps sign-ins only for browsers used within a day; see UPGRADING.md); the database is upgraded in place on first start (and restored with the code if anything fails). Read [UPGRADING.md](UPGRADING.md) before updating from 1.6, and the [changelog](CHANGELOG.md) for what changed.

Token limits now use one five-hour allowance. Upgrades combine the old regular
and slow allocations when slow tokens were enabled; sites that had them disabled
keep their regular allocation. Existing usage counts towards the combined allowance
without a reset. Weekly limits and the other quota and appearance settings remain
available; an administrator can still set an account's queue priority to slow.

## Documentation

- [Deployment and updates](docs/deployment.md) · [Backups](docs/backups.md) · [Configuration](docs/configuration.md) · [Two-step verification](docs/two-step-verification.md)
- [API](docs/api.md) · [Image generation with ComfyUI](docs/comfyui.md) · [Compute node](docs/compute.md) · [Worker PCs](docs/workers.md)
- [Architecture](docs/architecture.md) · [Development](docs/development.md) · [Capacity](docs/capacity.md) · [Production validation](docs/production-readiness.md)
- [Contributing](CONTRIBUTING.md) · [Code of conduct](CODE_OF_CONDUCT.md) · [Security policy](SECURITY.md)

## Privacy

Conversations are stored in the server's SQLite database and sent only to the inference backend you configure. No-history chats are left out of the user's history and erased after a configurable time, but administrators can review them in an audit log — the interface says so. If you enable volunteer worker PCs, the people running them can read the prompts they process. Shared links make a conversation readable by anyone who has the link.

## History

BananaChat began as BananaAI — the wish to run AI models ourselves instead of sending every request to someone else's servers. It started inside BananaWiki and was split out to run on its own. The current code is a from-scratch rewrite for open-source release. The repository starts at a single commit because the private history contains credentials and details of the servers it ran on; [NOTICE](NOTICE) records the original dates. The [about page](https://bananawiki.com/about/) on the BananaWiki site tells the longer story.

## Licence

BananaChat is licensed under the **GNU Affero General Public License v3.0 only** (`AGPL-3.0-only`). Personal and commercial use are allowed. If you run a modified version for others over a network, offer them its source code (AGPL section 13) and set `BC_SOURCE_URL` to it; the interface links to it. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright © 2026 Luca Zani and contributors. Originally started by Luca Zani ([OverloadedTech](https://github.com/OverloadedTech)) in July 2026. Each contributor retains copyright in their contributions.

## Maintainers

Started by Luca Zani; maintained by Luca Zani and contributors. To take part, start with [CONTRIBUTING.md](CONTRIBUTING.md).
