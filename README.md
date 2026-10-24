<img src="bananachat/static/img/apple-touch-icon.png" alt="BananaChat logo" width="90" height="90">

# BananaChat

BananaChat is a self-hosted chat application for [Ollama](https://ollama.com/). Run open models on your own hardware and give a group of people a private chat interface with accounts, quotas and administration.

- **Chat** with streaming answers, reasoning display, Markdown, attachments (images, PDFs, text and code), personalities, share links, search, exports and no-history chats.
- **OpenAI-compatible API** (`/v1/chat/completions`, `/v1/models`, `/v1/images/generations`) with personal keys, a playground and per-account token limits.
- **External API providers**: Anthropic, OpenAI, OpenRouter and other OpenAI-compatible services, with presets and keys encrypted on the server. LM Studio, llama.cpp and vLLM on your own hardware count as local models, with their model lists kept current. See [setup](docs/external-providers.md).
- **Claude Code and Codex subscription pools**: several subscription accounts each, with automatic model and usage discovery, caps at a share of each provider's five-hour, weekly and per-family allowance, and strict model limits. Off by default; administrators sign each account in from the administration page with the official CLIs' own login (a link and a code), after a one-time setup script. See [Claude Code](docs/claude-code.md) and [Codex](docs/codex-cli.md) for provider-approved deployment.
- **Cloud sessions** for coding work in isolated sandboxes. Off by default. See [agents](docs/agents.md).
- **Two-step verification** for every account, which administrators can require for all administrators. See [two-step verification](docs/two-step-verification.md).
- **Administration**: users and invitations, model catalogue and downloads, access policies, quotas, metrics, audit log, maintenance mode and theming.
- **Scales out**: separate web and GPU servers, volunteer worker PCs, and image generation with [ComfyUI](docs/comfyui.md). Workers whose owners allow it can be maintained from the browser (terminal and VNC) or with native SSH, VNC and RDP clients over a virtual network, without opening ports, and administrators manage their models remotely. See [workers](docs/workers.md).
- **Operations**: one command installs, updates with automatic rollback, backs up (optionally encrypted to a private Git repository) and restores a server. The HTTPS proxy it writes also works behind Cloudflare.
- English, Italian and German interface, light and dark themes, accessibility options, and BM mode: an easy mode with a simpler interface.

## Try it locally

You need Python 3.12 or newer and Ollama with at least one model:

```sh
ollama pull qwen3:4b
./dev.sh
```

Open <http://127.0.0.1:8000>. The terminal prints the setup token for creating the first administrator. The development server listens on loopback only.

## Run the tests

`make test` creates a virtual environment and runs the suite; `make lint` runs ruff. By hand:

```sh
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements.txt pytest ruff==0.17.1
python -m pytest -q
python -m ruff check .
```

Some tests need tools outside Python: Git and [`age`](https://github.com/FiloSottile/age) for the backup tests, `ssh-keygen` for the signed-update tests, and Node.js for the Markdown renderer tests (skipped without it). See [CONTRIBUTING](CONTRIBUTING.md) and [development](docs/development.md).

## Install on a server

On Linux with systemd, the usual layout is two servers: a GPU server that runs the models and a web server (a VPS is enough) that keeps accounts and chats. Install from the newest release tag. On the GPU server:

```sh
git clone https://github.com/BananaSuite/BananaChat.git
cd BananaChat
git checkout "$(git tag --list 'v*' --sort=-v:refname | head -n 1)"
sudo ./banana install --mode compute --domain compute.example.org
sudo bananachat proxy --install
```

It prints a pairing code; keep it secret. On the web server, check out the same release and run:

```sh
sudo ./banana install --domain chat.example.org --pair
sudo bananachat proxy --install
```

`--pair` asks for the code, shows its address for confirmation and tests the connection before installing. `--mode single` runs everything on one machine.

Updates are off by default and follow release tags. Run `sudo bananachat update` when you want one, or turn on automatic updates with `sudo bananachat updates enable --require-signatures ALLOWED_SIGNERS`. Every update backs up the installation first and rolls back if the new version does not start. Read [UPGRADING.md](UPGRADING.md) before updating and the [changelog](CHANGELOG.md) for what changed. See [deployment](docs/deployment.md) and [backups](docs/backups.md).

## Documentation

- [Deployment and updates](docs/deployment.md) · [Backups](docs/backups.md) · [Configuration](docs/configuration.md) · [Two-step verification](docs/two-step-verification.md)
- [API](docs/api.md) · [Image generation with ComfyUI](docs/comfyui.md) · [Compute server](docs/compute.md) · [Worker PCs](docs/workers.md)
- [Architecture](docs/architecture.md) · [Development](docs/development.md) · [Capacity](docs/capacity.md)
- [Contributing](CONTRIBUTING.md) · [Code of conduct](CODE_OF_CONDUCT.md) · [Security policy](SECURITY.md)

## Privacy

Conversations are stored in the server's SQLite database and sent only to the inference backend you configure. No-history chats are left out of the user's history and erased after a configurable time, but administrators can review them in an audit log, and the interface says so. If you enable volunteer worker PCs, the people running them can read the prompts they process. Shared links make a conversation readable by anyone who has the link.

## History

BananaChat began as BananaAI, a way to run AI models ourselves instead of sending every request to someone else's servers. It started inside BananaWiki and was rewritten as a separate application for the open-source release. The public repository starts from a clean export; [NOTICE](NOTICE) records the original dates. The [about page](https://bananawiki.com/about/) on the BananaWiki site tells the longer story.

## Licence

BananaChat is licensed under the **GNU Affero General Public License v3.0 only** (`AGPL-3.0-only`). Personal and commercial use are allowed. If you run a modified version for others over a network, offer them its source code (AGPL section 13) and set `BC_SOURCE_URL` to it; the interface links to it. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright © 2026 Luca Zani and contributors. Started by Luca Zani ([OverloadedTech](https://github.com/OverloadedTech)) in July 2026. Each contributor retains copyright in their contributions.

## Maintainers

Maintained by Luca Zani and contributors. To take part, start with [CONTRIBUTING.md](CONTRIBUTING.md).
