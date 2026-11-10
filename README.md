<img src="bananachat/static/img/apple-touch-icon.png" alt="BananaChat logo" width="90" height="90">

# BananaChat

BananaChat is a self-hosted chat application for [Ollama](https://ollama.com/). Run open models on your own hardware and give a group of people a private chat interface with accounts, quotas and administration.

- **Chat** with streaming answers, reasoning display, Markdown, attachments (images, PDFs, text and code), personalities, share links, search, exports and no-history chats.
- **OpenAI-compatible API** (`/v1/chat/completions`, `/v1/models`, `/v1/images/generations`) with personal keys, a playground and per-account token limits.
- **External API providers**: Anthropic, OpenAI, OpenRouter and other OpenAI-compatible services, with presets and keys encrypted on the server. LM Studio, llama.cpp and vLLM running on your own hardware count as local models, and their model lists stay up to date. See [setup](docs/external-providers.md).
- **Claude Code and Codex subscription pools**: connect several subscription accounts per provider. BananaChat discovers the available models and usage, and keeps each account within a share of the provider's five-hour, weekly and per-model-family limits. This feature is off by default. After a one-time setup script, administrators sign in each account from the administration page with the official CLI login (a link and a code). See [Claude Code](docs/claude-code.md) and [Codex](docs/codex-cli.md) for deployments the providers allow.
- **Cloud sessions** for coding work in isolated sandboxes. This feature is off by default. See [agents](docs/agents.md).
- **Two-step verification** for every account. Administrators can require it for all administrators. See [two-step verification](docs/two-step-verification.md).
- **Administration**: users and invitations, model catalogue and downloads, access policies, quotas, metrics, audit log, maintenance mode and theming.
- **Scaling out**: separate web and GPU servers, volunteer worker PCs, and image generation with [ComfyUI](docs/comfyui.md). If a worker's owner allows it, administrators can maintain the worker from the browser (terminal and VNC) or with native SSH, VNC and RDP clients over a virtual network, without opening any ports, and manage its models remotely. See [workers](docs/workers.md).
- **Operations**: a single command installs, updates, backs up and restores a server. Updates roll back automatically if the new version fails to start, and backups can be encrypted and stored in a private Git repository. The generated HTTPS proxy configuration also works behind Cloudflare.
- English, Italian and German interface, light and dark themes, accessibility options, and BM mode, an easy mode with a simpler interface.

## Try it locally

You need Python 3.12 or newer and Ollama with at least one model:

```sh
ollama pull qwen3:4b
./dev.sh
```

Then open <http://127.0.0.1:8000>. The terminal prints a setup token that you use to create the first administrator. The development server only listens on the loopback interface.

## Run the tests

`make test` creates a virtual environment and runs the test suite, and `make lint` runs ruff. To do the same by hand:

```sh
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements.txt pytest ruff==0.17.1
python -m pytest -q
python -m ruff check .
```

Some tests need tools outside Python: Git and [`age`](https://github.com/FiloSottile/age) for the backup tests, `ssh-keygen` for the signed-update tests, and Node.js for the Markdown renderer tests, which are skipped if Node.js isn't installed. See [CONTRIBUTING](CONTRIBUTING.md) and [development](docs/development.md) for details.

## Install on a server

On Linux with systemd, the usual setup uses two servers: a GPU server that runs the models, and a web server (a small VPS is enough) that stores accounts and chats. Install from the latest release tag. On the GPU server, run:

```sh
git clone https://github.com/BananaSuite/BananaChat.git
cd BananaChat
git checkout "$(git tag --list 'v*' --sort=-v:refname | head -n 1)"
sudo ./banana install --mode compute --domain compute.example.org
sudo bananachat proxy --install
```

The installer prints a pairing code. Keep it secret. On the web server, check out the same release and run:

```sh
sudo ./banana install --domain chat.example.org --pair
sudo bananachat proxy --install
```

With `--pair`, the installer asks for the pairing code, shows the GPU server's address for you to confirm, and tests the connection before installing. To run everything on one machine, use `--mode single` instead.

Updates are off by default and follow release tags. Run `sudo bananachat update` when you want to update, or turn on automatic updates with `sudo bananachat updates enable --require-signatures ALLOWED_SIGNERS`. Every update backs up the installation first and rolls back if the new version doesn't start. Before updating, read [UPGRADING.md](UPGRADING.md), and see the [changelog](CHANGELOG.md) for what changed. For more details, see [deployment](docs/deployment.md) and [backups](docs/backups.md).

## Documentation

- [Deployment and updates](docs/deployment.md) · [Backups](docs/backups.md) · [Configuration](docs/configuration.md) · [Two-step verification](docs/two-step-verification.md)
- [API](docs/api.md) · [Image generation with ComfyUI](docs/comfyui.md) · [Compute server](docs/compute.md) · [Worker PCs](docs/workers.md)
- [Architecture](docs/architecture.md) · [Development](docs/development.md) · [Capacity](docs/capacity.md)
- [Contributing](CONTRIBUTING.md) · [Code of conduct](CODE_OF_CONDUCT.md) · [Security policy](SECURITY.md)

## Privacy

Conversations are stored in the server's SQLite database and are only sent to the inference backend you configure. No-history chats don't appear in the user's history and are deleted after a configurable time, but administrators can review them in an audit log, and the interface tells users this. If you enable volunteer worker PCs, the people running them can read the prompts they process. Anyone with a share link can read the shared conversation.

## History

BananaChat started as BananaAI, a way to run AI models on our own hardware instead of sending every request to someone else's servers. It began as part of BananaWiki and was rewritten as a separate application for its open-source release. The [about page](https://bananawiki.com/about/) on the BananaWiki site tells the full story.

## Licence

BananaChat is licensed under the [GNU Affero General Public License v3.0 only](LICENSE) (`AGPL-3.0-only`). You can use it for personal and commercial purposes.

If you run a modified version of BananaChat that other people use over a network, the AGPL requires you to make your source code available to them. Set `BC_SOURCE_URL` to the address of your source code, and the interface will link to it. If you redistribute BananaChat, keep the third-party notices listed in [NOTICE](NOTICE).

BananaChat was created by Luca Zani ([OverloadedTech](https://github.com/OverloadedTech)) in July 2026.

Copyright © 2026 Luca Zani and contributors. Contributors keep the copyright to their contributions.

## Maintainers

BananaChat is maintained by Luca Zani and contributors. If you'd like to help, start with [CONTRIBUTING.md](CONTRIBUTING.md).
