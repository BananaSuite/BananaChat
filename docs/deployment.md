# Deploy and maintain BananaChat

BananaChat uses one `banana` command for installation, updates, backup, migration, restoration, and uninstall. It remembers the server's role. After installation the command is available globally as `bananachat`.

**Automatic repository updates are optional and off by default.** Enabling them authorizes deployment of future changes from the selected branch, including changes that may remove features. Keep manual updates or choose a controlled fork/branch when you need a fixed feature set.

## Choose a role

| Mode | Runs here | Typical machine |
| --- | --- | --- |
| `single` | Chat application and local Ollama | One machine with sufficient model memory. |
| `web` | Accounts, chats, files, and the web/API service | A reliable VPS connected to a separate inference backend. |
| `compute` | Local Ollama and an authenticated streaming inference API | A GPU server behind HTTPS or an SSH tunnel. |

The split setup keeps chat data on the web server and model files/inference on the compute server. Either server can be maintained independently. Existing optional personal workers and ComfyUI remain supported as additional components, separate from the main server installer.

## Requirements

Use Linux with systemd, Python 3.12+, Python venv support, Git, and local storage. Debian 13 and Ubuntu 24.04 are suitable starting points. Install Caddy through its official distribution for HTTPS. Install Ollama and GPU drivers through their official channels on `single` or `compute` hosts. The installer manages the Ollama process and model directory once the binary is available; use `--ollama-binary /absolute/path` if needed.

If another Ollama service already listens on port 11434, stop/disable it before selecting a role that manages Ollama, or use `web` mode with that existing backend. The installer reports occupied ports instead of replacing an unrelated service. Moving an existing model cache into the managed `data/models` directory is an operator decision; preserve it before changing an old setup.

Obtain a clean, reviewed Git checkout. Installation uses its exact local commit; automatic updates remain off. The default future source is `https://github.com/BananaSuite/BananaChat.git`, branch `main`.

## One server

```sh
git clone https://github.com/BananaSuite/BananaChat.git
cd BananaChat
sudo ./banana install --mode single --domain ai.example.org
sudo bananachat proxy
sudo bananachat proxy --install
```

Point DNS at this server and allow HTTPS ports 80/443. `proxy` prints a matching Caddyfile. `proxy --install` validates it before loading it, and preserves an unrelated existing proxy unless you explicitly select `--replace`. On a server with other sites, merge the printed block into the existing proxy configuration.

The chat service and Ollama bind to loopback. Model files are stored under `/opt/bananachat/data/models`; pull a model through the local Ollama API/CLI, for example `ollama pull qwen3:4b`, and enable it in the administrator interface. Choose a model that fits your hardware.

Read the generated `BC_SETUP_TOKEN` privately from `/opt/bananachat/config/app.env`, open the HTTPS address, and create the first administrator. Remove the setup token after setup if desired, then run `sudo bananachat restart`. Review account registration, model access, and quotas before inviting users.

## Two servers

On the compute machine:

```sh
sudo ./banana install --mode compute --domain compute.example.org
sudo bananachat proxy
sudo bananachat proxy --install
```

This runs Ollama on `127.0.0.1:11434` and the authenticated gateway on `127.0.0.1:11435`. The gateway checks a generated token stored in `/opt/bananachat/data/.compute-api-token`. It forwards allowed Ollama/chat API requests and streams responses. Health and source-offer endpoints are public; model requests require a Bearer token. Ordinary Ollama does not enforce authentication through an API-key environment variable, which is why this role includes an actual validating gateway.

Securely copy that token into a private file on the web server, for example `/root/banana-compute.token` with mode `0600`. Do not put its value in a command line or repository. On the web machine:

```sh
sudo ./banana install --mode web --domain ai.example.org --backend-url https://compute.example.org --backend-token-file /root/banana-compute.token
sudo bananachat proxy
sudo bananachat proxy --install
```

The installer stores the backend URL and key in the web server's private `config/app.env`. Finish first-admin setup there. Keep inference access restricted to the web server where your network allows it. The compute server can be replaced or temporarily unavailable without moving the web server's chat database.

For an SSH tunnel, omit the compute domain and forward its authenticated loopback gateway to a loopback port on the web host. Set `--backend-url http://127.0.0.1:11435` and provide the same token. A tunnel needs its own supervised SSH connection. Direct unencrypted remote HTTP is rejected by the managed setup.

The two update timers are independent. For internal automatic rollout, keep web/compute API changes backward compatible and merge only reviewed, tested changes. An incompatible upgrade needs a documented order and manual updates during the transition. The updater does not provide a distributed transaction across two machines.

## Update policy and private repositories

```sh
sudo bananachat source check
sudo bananachat update
sudo bananachat updates enable --interval 60
sudo bananachat updates status
sudo bananachat updates disable
```

`source check` checks the configured repository/branch without deployment. Automatic checking uses a systemd timer with a small randomized delay. Disabling updates prevents the next update; an already applying transaction finishes safely. An intentionally stopped application is not restarted by the updater.

Choose your own source, branch, and optional fallback:

```sh
sudo bananachat source set --repo https://forge.example.org/team/BananaChat.git --branch stable --fallback-branch main
```

A fallback is used only when the selected branch has been deleted. Without one, updates pause and the running version stays in place. Rewritten or unrelated history also pauses updates. A reviewed manual `update --allow-divergent` can make an intentional source switch; automatic runs always require ancestry. Remove the fallback with `source set --clear-fallback`.

For a private HTTPS repository, create a mode-0600 read-only token file and configure it separately from the URL:

```sh
sudo bananachat source set --repo https://github.com/YOUR_TEAM/BananaChat.git --branch main --token-file /root/banana-repo.token --username YOUR_BOT
sudo bananachat source check
```

GitHub fine-grained tokens need Contents read access to that repository. Forgejo tokens need repository read access. Use the username required by your forge; GitHub App tokens commonly use `x-access-token`.

SSH deploy keys are also supported:

```sh
sudo bananachat source set --repo git@forge.example.org:team/BananaChat.git --branch main --ssh-key /root/banana-deploy-key --known-hosts /root/banana-known-hosts
sudo bananachat source check
```

Obtain and verify the forge's SSH host key through a trusted channel first. Strict host checking is enforced. Repository credentials are stored as root-private files under `config/`, kept out of URLs and Git configuration, and passed only to the updater's Git process. They are separate from compute API credentials and BananaVibe bot credentials. Repeat `source set` with a new credential file to rotate access; `source set --clear-credentials` removes it. Changing hosts without a replacement credential disables authentication.

Each update prepares the new release, stops the remembered services, creates a complete backup, switches source, and checks readiness before allowing traffic. Failed readiness restores the old code and data together. A failed revision is not retried automatically; correct the problem and use `update --retry-failed`, or deploy a newer corrected commit. The newest three automatic packages are retained by default; set `--keep-backups` on `updates enable` to change that policy. Manual packages are preserved. Prepared releases use the same retention count, always preserving the active and previous releases.

## What a bad commit can and cannot do

The updater assumes the source repository can go wrong, whether through a
mistake or through someone who should not have push access.

A commit that deletes the application cannot take the deployment with it.
Nothing is switched until a release has been prepared from the new revision,
and a revision that is empty or missing the managed entry point is refused at
that point, with the running version untouched. A revision that installs and
starts but fails its readiness checks is rolled back to the previous code and
its matching database. Either way the failed revision is recorded and never
retried automatically. Chat history, settings and model recipes live outside the
source tree, so no commit can delete them.

Rewritten history is refused as well. An automatic update only moves forward
from the installed revision, so a force-push that replaces history pauses
updates instead of applying them, and switching to unrelated history takes a
deliberate `update --allow-divergent` from a maintainer.

What none of that catches is a well-formed commit that does exactly what it
says and is also hostile: code that installs cleanly, passes readiness and
then does something you did not want. If the repository you follow is
compromised, the updater will deploy what it finds there. Against that, require
signatures:

```sh
sudo bananachat source set --require-signatures /root/allowed_signers
```

The file is an SSH allowed-signers file, one line per key you trust:

```text
you@example.org namespaces="git" ssh-ed25519 AAAAC3Nza...
```

With it in place, every revision must carry a valid SSH signature from a listed
key before anything is fetched into a release. An unsigned commit, or one
signed by a key you have not listed, stops the update and leaves the running
version alone. Sign your releases with `git commit -S` after setting
`gpg.format=ssh` and `user.signingkey`. `source set --clear-signatures` returns
to unsigned updates.

This checks SSH signatures only. An OpenPGP-signed commit is refused, and
the message says so. That is deliberate: Git picks its verifier from the
signature itself, so an OpenPGP signature would be checked against whatever
the server's GnuPG keyring happens to trust instead of against the file you
configured. If you sign with OpenPGP today, move the signing key to SSH format
for the server you want protected.

Keep the allowed-signers file on the server, owned by the operator and mode
0600; the updater refuses to read it otherwise, and refuses to update rather
than carrying on unverified if it goes missing. Signature checks protect the
code only: a compromised token still lets someone read a private repository.

## Backup, move, and restore

[Repository backups](backups.md) add optional encrypted storage and restore through private GitHub or Forgejo repositories. Install age and configure `bananachat backups` separately on each server, using a unique series name per role. Scheduling stays off until `backups enable`. Git backups exclude weights and save model download recipes; after restoring, Admin → Models offers downloads or deferral. A compute-only host uses `bananachat models status`, then `models restore --yes` or `models skip`.

```sh
sudo bananachat migrate --output /root/bananachat-migration.tar.gz
```

`backup` and `migrate` create the same portable package. They briefly stop services for a consistent snapshot and return them to their prior running state. The package contains source, data, session/API keys, environment settings, private Git credentials, and managed model files. Allow enough disk space for these, especially on a compute host. Keep external ComfyUI data, GPU drivers, and any paths outside the managed root backed up separately.

Packages have mode `0600` but are **not encrypted**. Transfer them over SSH and store them privately off the server. For a final migration, prevent new writes on the old server after the final package, test the new server, then change routing/DNS. Do not leave two independently writable copies serving the same users.

On a new server with the prerequisites and a compatible clean checkout:

```sh
sudo ./banana install --restore /root/bananachat-migration.tar.gz
sudo bananachat status
sudo bananachat proxy
```

This restores the saved mode and exact bundled source without fetching the update repository. Installing Python dependencies still needs package-registry access. `single` and `compute` also need Ollama installed locally. Add `--domain new.example.org` when changing the public hostname, then configure the proxy and review integration URLs.

To restore after installing an empty matching mode, run `sudo bananachat restore PACKAGE`. It saves a `before-restore` package before replacement. Restore cannot convert `web`, `single`, and `compute` modes; use a separate root and a deliberate data/backend migration for a role change. Restore packages only from trusted operators: they include executable source and secrets.

**Automatic updates are disabled after every restore.** Verify login, chat history, attachments, models, and a streaming reply before enabling them again. For a split deployment, create a separate package for each server and verify their URL/token pairing after moving either one.

`sudo bananachat rollback` restores the code/data package saved before the last successful update. `rollback --package PATH` selects another package. `sudo bananachat recover` recovers an interrupted transaction from its durable journal. Restoring old code alone is insufficient after a database migration.

## Older installations and optional components

The old production shell entrypoints have been retired. Keep the old checkout and services available while exporting data and testing the new managed installation. Export the old chat database from Admin → Migration, then import it into the installed single/web service with `sudo bananachat restore --legacy-database /root/bananachat_export.tar.gz`. The command stops all managed processes, makes a rollback package and checks startup before reopening service. Browser imports have been retired because a live request cannot quiesce its peer processes. Also copy persistent session keys, uploaded files/audio, operator configuration, and other data omitted by that export separately into the managed data directory while services are stopped. For older audio stored in `app/static/audio`, move it into the managed `data/audio` directory. Preserve ownership and private permissions, restart, and verify before switching users. Old application exports are not the same format as `banana` installation packages.

The existing [personal worker](../worker/README.md) can be used independently through its single Python entrypoint. Dedicated inference servers use `--mode compute`. [ComfyUI](comfyui.md) and the optional [checkpoint downloader](../compute/README.md) keep their own data and credentials; configure and back them up separately.

## Status and removal

Use `sudo bananachat status` and `sudo journalctl -u bananachat` for the current revision and service logs. `start`, `stop`, and `restart` manage the remembered service set, including managed Ollama. Configuration is under `/opt/bananachat/config`, runtime data under `data`, prepared code under `releases`, and `current` selects the active source. Keep mutable data there instead of editing installed source. Operation results are saved privately in `config/history.jsonl`.

`sudo bananachat uninstall` disables updates and backup scheduling and removes managed services and the command while retaining files. An unchanged Caddyfile installed by this command is reverted to its prior configuration; a later operator edit is retained for manual adjustment. Shared packages, system accounts, and other services are retained. Reinstall preserved data from a clean checkout with the same root/mode/name and `install --reuse-data`.

Permanent deletion requires `sudo bananachat uninstall --purge --confirm bananachat`. This also removes local backups under the installation root; copy the migration package elsewhere first. For multiple installations use separate `--root`, `--name`, and `--port` values and merge proxy configuration deliberately.

## Optional desktop worker

The worker can contribute an existing machine's Ollama capacity to the web server. Configure `BC_SERVER_URL` and the separately issued `BC_WORKER_TOKEN`, then run `python worker/bananachat_worker.py run`. Its `install`, `start`, `stop`, `status`, and `uninstall` commands manage background execution.

Linux uses a user service by default; run `install --system` from the intended non-root worker account to create a system service under that account. The installer uses sudo only for system configuration. Windows uses a Task Scheduler job for the current account at login, with an interactive token and least privilege, preferring `pythonw.exe` so the worker runs without a console window. Its settings file goes under `%APPDATA%\BananaChat` and the installer points the worker at a rotating log file beside it, since a scheduled task has nowhere to send stdout. Run installation from that account. An old `BananaChatWorker` Windows system service must first be stopped and removed using `sc.exe stop BananaChatWorker` and `sc.exe delete BananaChatWorker` in an administrator terminal. The new installer refuses to leave that old service running alongside the user task.

macOS uses a LaunchAgent in `~/Library/LaunchAgents` that starts at login, on both Intel and Apple Silicon. `install --system` writes a LaunchDaemon to `/Library/LaunchDaemons` that starts at boot; run it from the worker's own account, since the daemon is pinned to that account rather than left as root. Control uses `launchctl kickstart`, `bootout` and `print`, falling back to `load -w` on older systems that predate `bootstrap`. launchd has no `EnvironmentFile`, so the worker reads `~/.config/bananachat-worker.env`, which the installer creates with mode 0600; the plist carries only the path to it, never the token.

The managed server deployment trials cover Linux. The Windows task and the macOS agent are covered by tests against the artefacts they generate and the commands they issue, but neither has been exercised on that hardware here, and the chosen desktop GPU still needs a trial on the machine before relying on unattended work.

On macOS the worker has no GPU utilisation reading, so the "gaming" gate that pauses inference on a busy NVIDIA card never fires. The worker still yields, using the IOKit idle reading to drop to below-normal priority whenever someone is at the keyboard.

## Source availability

AGPL-3.0-only permits personal and commercial use. Keep `BC_SOURCE_URL` pointing to corresponding source users can obtain. A private update repository does not remove the source-offer requirement for a modified network service; provide an accessible corresponding-source copy or authorized access. The updater preserves explicitly customized source-offer URLs.
