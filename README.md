<img src="bananachat/static/img/logo.svg" alt="BananaChat logo" width="72">

# BananaChat

**BananaChat** is a self-hosted chat application for [Ollama](https://ollama.com/). Run open models on your own hardware and give a group of people a private, polished chat interface with accounts, quotas and administration — no conversation leaves the servers you control.

- **Chat** with streaming answers, reasoning display, Markdown and code rendering, file attachments (images, PDFs, text and code), model picker, sampling options, personalities (emoji avatar, greeting, conversation starters, preferred model and response style; a default for new chats, templates, share links and JSON import/export), sharing links, search, exports and no-history chats.
- **OpenAI-compatible API** (`/v1/chat/completions`, `/v1/models`, `/v1/images/generations`) with personal keys, a playground and per-account token limits (5-hour and weekly windows, per-model limits and reasoning-effort levels).
- **Administration**: users and invitations, model catalog and downloads, fine-grained access policies (per model, per category, uncensored models, image generation, personalities) with user requests, featured personalities published for everyone and personality moderation, quotas, metrics, audit log, maintenance mode and theming.
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

## Install on a server

On Linux with systemd, the usual layout is two servers: a GPU server that runs the models and a web server (a VPS is enough) that keeps accounts and chats. On the GPU server:

```sh
git clone https://github.com/BananaSuite/BananaChat.git
cd BananaChat
sudo ./banana install --mode compute --domain compute.example.org
sudo bananachat proxy --install
```

It prints a pairing code (keep it secret). On the web server, the same checkout and:

```sh
sudo ./banana install --domain chat.example.org --pair
sudo bananachat proxy --install
```

`--pair` asks for the code, shows the address in it for confirmation and tests the connection before installing. An Ollama that already runs on the GPU server can be kept with `--ollama-url http://127.0.0.1:11434`; `--mode single` runs everything on one machine. Updates are **off by default**: run `sudo bananachat update` when you want one, or opt in with `sudo bananachat updates enable`. Every update backs up the installation first and rolls back automatically if the new version does not start. See [deployment](docs/deployment.md) and [backups](docs/backups.md).

### Upgrading from the previous release

Run `sudo bananachat update` as usual. This release is a complete rewrite that keeps your database, settings, accounts, chats, API tokens and sign-ins; the database is upgraded in place on first start (and restored with the code if anything fails). Read the [changelog](CHANGELOG.md) for what changed.

## Documentation

- [Deployment and updates](docs/deployment.md) · [Backups](docs/backups.md) · [Configuration](docs/configuration.md)
- [API](docs/api.md) · [Image generation with ComfyUI](docs/comfyui.md) · [Compute node](docs/compute.md) · [Worker PCs](docs/workers.md)
- [Architecture](docs/architecture.md) · [Development](docs/development.md) · [Capacity](docs/capacity.md)
- [Contributing](CONTRIBUTING.md) · [Code of conduct](CODE_OF_CONDUCT.md) · [Security policy](SECURITY.md)

## Privacy

Conversations are stored in the server's SQLite database and sent only to the inference backend you configure. No-history chats are left out of the user's history and erased after a configurable time, but administrators can review them in an audit log — the interface says so. If you enable volunteer worker PCs, the people running them can read the prompts they process. Shared links make a conversation readable by anyone who has the link.

## History

BananaChat began as BananaAI — the wish to run AI models ourselves instead of sending every request to someone else's servers. It started inside BananaWiki and was split out to run on its own. The current code is a from-scratch rewrite for open-source release. The repository starts at a single commit because the private history contains credentials and details of the servers it ran on; [NOTICE](NOTICE) records the original dates.

## License

BananaChat is licensed under the **GNU Affero General Public License v3.0 only** (`AGPL-3.0-only`). Personal and commercial use are allowed. If you run a modified version for others over a network, offer them its source code (AGPL section 13) and set `BC_SOURCE_URL` to it; the interface links to it. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright © 2026 Luca Zani and all contributors. Originally started by Luca Zani ([OverloadedTech](https://github.com/OverloadedTech)) at Officina Tecnologica. Each contributor retains copyright in their contributions.
