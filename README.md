<img src="app/static/favicons/banana_yellow.png" alt="BananaChat logo" width="64">

# BananaChat

**BananaChat** is a self-hosted chat application for Ollama. It provides streaming conversations, model selection, file attachments, shared chats, user accounts, and administrator controls for access and usage. Optional image generation uses a separate ComfyUI server.

## Run locally

Install Python 3.12 or newer and [Ollama](https://ollama.com/). Pull a model suitable for your machine, then start BananaChat:

```sh
ollama pull qwen3:4b
./dev.sh
```

Open `http://127.0.0.1:8000`. Use the installation token printed in the terminal to create the first administrator. The development server stays on loopback; `--debug` is an explicit option for local development.

For a persistent installation with HTTPS, follow [deployment](docs/deployment.md). BananaChat can run independently of BananaWiki and BananaVibe. You can run Ollama on the same machine or connect to a protected backend elsewhere.

Managed servers use `sudo ./banana install --mode single`, or `--mode web` and `--mode compute` for a split deployment. Subsequent `bananachat update`, `backup`, `restore`, and `uninstall` commands remember the role. Automatic updates are **off by default** and can be enabled or disabled with `bananachat updates`. Custom Git URLs, branches, explicit fallbacks, and private repository tokens/SSH keys are supported.

[Encrypted repository backups](docs/backups.md) support private GitHub and Forgejo destinations. They keep chats, settings, source, and model download recipes while excluding weights. After restoring, administrators can approve Ollama/Hugging Face downloads or leave them for later. Existing chats can continue with available models, and access rules still apply.

## Data and administration

Chat data and account settings are stored in SQLite. `BC_INSTANCE_DIR` selects the data directory; preserve it and the persistent session key across updates. Configure available models, users, invitations, quotas, and retention in the administrator interface.

The end-user interface ships in Italian and English, with Italian as the default. The administrator interface is English only, since it is aimed at whoever operates the instance rather than at its users.

Incognito chats stay out of the user's history, but administrators can retain them in the incognito audit log. Shared links make the selected conversation accessible to people who have the link. Model requests go to the backend you configure; review that backend's privacy and retention settings.

## Where this came from

BananaChat began as BananaAI, out of wanting to self-host AI models rather than send every request to someone else's servers. It started inside BananaWiki and was split out so it can run on its own; a wiki and a chat client share a login screen and little else. It is meant as a first look at local AI for people who have not tried it. Requests go to the Ollama server you configure, which can be the same machine.

It was an internal project until September 2026. The repository starts at one commit because the private history holds credentials and details of the servers it ran on. [NOTICE](NOTICE) has the original dates.

## Documentation

- [Deployment and backups](docs/deployment.md)
- [Configuration](docs/configuration.md)
- [Measured capacity and operating limits](docs/capacity.md)
- [API](docs/api.md)
- [ComfyUI image generation](docs/comfyui.md)
- [Contributing](CONTRIBUTING.md), [code of conduct](CODE_OF_CONDUCT.md) and [security reports](SECURITY.md)

## License and ownership

BananaChat is licensed under **GNU AGPL version 3** (`AGPL-3.0-only`). Personal and commercial use are allowed. If users interact with a modified version over a network, offer them its corresponding source under AGPL section 13. Set `BC_SOURCE_URL` to that source; the interface displays the link. See [LICENSE](LICENSE) and preserve third-party notices.

Originally started by Luca Zani ([OverloadedTech](https://github.com/OverloadedTech)) on 9 July 2026, the date of its first recorded project commit. It began as BananaAI and is developed at Officina Tecnologica. BananaChat inherits earlier BananaWiki history beginning on 20 February 2026.

Copyright © 2026 Luca Zani and all contributors. Each contributor retains copyright in their contributions. [NOTICE](NOTICE) records the original commits and exact timestamps.
