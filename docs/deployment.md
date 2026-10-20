# Deploy and maintain BananaChat

BananaChat uses one `banana` command for installation, updates, backup, migration, restoration, and uninstall. It remembers the server's role. After installation the command is available globally as `bananachat`.

**Automatic updates are optional and off by default.** Updates follow the highest signed release tag of the update source by default, or a branch you choose, and a new release may remove features. Keep manual updates, or follow a branch of your own fork, when you need a fixed feature set.

## Choose a layout

BananaChat normally runs on **two servers**: a reliable web server (for example a VPS) keeps accounts, chats and files, and a compute server with the GPU runs the models. `single` puts both on one machine, for a small installation or testing.

| Mode | Runs here | Typical machine |
| --- | --- | --- |
| `compute` | Ollama (managed, or the one already running there) and an authenticated streaming gateway | The GPU server or cluster node. |
| `web` | Accounts, chats, files, and the web/API service; never Ollama | A reliable VPS paired with the compute server. |
| `single` | Chat application and Ollama | One machine with enough memory for the models. |

The web server keeps the chat data and the compute server the model files. Either can be updated, replaced or restarted independently. Optional personal workers and ComfyUI remain separate components.

## Requirements

Use Linux with systemd, Python 3.12+, Python venv support, Git, and local storage. Debian 13 and Ubuntu 24.04 are suitable starting points. Install Caddy through its official distribution for HTTPS. On `compute` and `single` hosts install Ollama and the GPU drivers through their official channels. The web server needs neither.

Install from a clean checkout of the newest release tag (`git checkout "$(git tag --list 'v*' --sort=-v:refname | head -n 1)"`). Installation uses that exact commit. Later updates come from `https://github.com/BananaSuite/BananaChat.git` and follow its release tags; see [update policy](#update-policy-and-private-repositories).

## Two servers (default)

### 1. The compute server

```sh
git clone https://github.com/BananaSuite/BananaChat.git
cd BananaChat
git checkout "$(git tag --list 'v*' --sort=-v:refname | head -n 1)"
sudo ./banana install --mode compute --domain compute.example.org
sudo bananachat proxy --install
```

This runs a managed Ollama on `127.0.0.1:11434` (its models under `/opt/bananachat/data/models`) and the authenticated gateway on `127.0.0.1:11435`, which the HTTPS proxy publishes. Ollama has no authentication of its own, so it is never exposed: the gateway checks a generated token (stored in `/opt/bananachat/data/.compute-api-token`), forwards only the Ollama/OpenAI API calls BananaChat and API clients use, and streams the answers. Downloading, creating, copying and deleting models needs a second generated token (`/opt/bananachat/data/.compute-management-token`), so the first one alone cannot change the models; see [compute servers](compute.md).

At the end the installer prints a **pairing code**, one line starting with `bcpair1.`. It contains the gateway's address and token, so treat it like a password. Show it again at any time with `sudo bananachat compute pairing-code`.

**An Ollama is already running on this machine** (a shared cluster node, for example): keep it and let BananaChat use it as it is:

```sh
sudo ./banana install --mode compute --domain compute.example.org --ollama-url http://127.0.0.1:11434
```

With `--ollama-url` (a loopback address only) BananaChat installs no Ollama service, needs no `ollama` program, never starts, stops, updates or reconfigures that Ollama, and uses it for readiness checks and model downloads. Its models stay wherever that Ollama keeps them, outside BananaChat's backups. Without the option, an occupied port 11434 stops the installation with this suggestion instead of replacing the running service. `--ollama-binary /absolute/path` selects the program for the managed Ollama when it is not on `PATH`.

The managed Ollama gets `OLLAMA_HOST` and `OLLAMA_MODELS` in `/opt/bananachat/config/app.env` at installation. Later changes you make there are kept by updates and restores; if you move Ollama to another port, change `BC_COMPUTE_UPSTREAM` with it (readiness checks follow that address).

### 2. The web server

```sh
git clone https://github.com/BananaSuite/BananaChat.git
cd BananaChat
git checkout "$(git tag --list 'v*' --sort=-v:refname | head -n 1)"
sudo ./banana install --domain chat.example.org --pair
sudo bananachat proxy --install
```

`--pair` asks for the pairing code (the input is hidden) and implies `--mode web`; `--pair-file FILE` reads it from a private file instead, and `--pair CODE` takes it directly (visible in the shell history and process list). It then shows the gateway address in the code and asks you to confirm it before the token is sent anywhere; `--yes` confirms without asking and is needed without a terminal. Before anything is installed, the web server connects to the compute server exactly as it will later (`GET /api/version` through the gateway, with the token) and stops with an explanation if the address cannot be reached, the certificate is not accepted, the token is rejected or Ollama is not ready. `--skip-connection-check` installs anyway, for a compute server that is not reachable yet.

The address and tokens are stored in the web server's private `config/app.env` as `BC_OLLAMA_URL`, `BC_OLLAMA_API_KEY` and, when the code carries one, the model management token `BC_OLLAMA_MANAGEMENT_API_KEY` (checked against the gateway too). A web installation without a compute server is refused: a web server never uses an Ollama on its own machine, and `--ollama-binary`/`--ollama-url` are rejected in web mode. The older form, `--mode web --backend-url https://compute.example.org --backend-token-file /root/banana-compute.token`, keeps working and is tested the same way; add `--backend-management-token-file FILE` for a compute server that keeps model management for its own token.

The installer ends with what to do next: point DNS at the server, install the HTTPS proxy, and read the first-administrator setup token privately with `sudo grep BC_SETUP_TOKEN /opt/bananachat/config/app.env`. The token only works until the first administrator exists; you may remove it afterwards (updates do not add it back). Then download and enable a model in Admin → Models (downloads run on the compute server; new models wait for your review unless you switch Admin → Models → Settings to automatic enrolment) and review sign-up, model access and quotas before inviting users.

### Models on the compute server

The web server manages the compute server's models through the gateway: it lists them (`/api/tags`), reads their details (`/api/show`), downloads (`/api/pull`) and deletes them (`/api/delete`); it never runs Ollama itself. Downloads queue in Admin → Models → Downloads (one at a time by default, several at once from a list), survive restarts of either server and are retried after network errors. The web server cannot see the compute server's disk: keep enough free space under `/opt/bananachat/data/models`; a download that runs out of space fails with Ollama's "no space left on device" message and can be retried after you free space. Models that disappear from the compute server are marked missing after a few syncs and hidden (their chats stay); a compute server that is unreachable changes nothing in the catalogue.

### Check or change the connection later

```sh
sudo bananachat backend status          # address and token fingerprint, no secrets
sudo bananachat backend test            # connect now, as the chat service does
sudo bananachat backend connect         # paste a new pairing code (hidden)
```

`backend connect` also accepts `CODE` (visible in the shell history and process list, like `--pair CODE`), `--pair-file FILE`, or `--url URL --token-file FILE` (add `--management-token-file FILE` when the compute server keeps model management for its own token; a pairing code carries both); `--url` next to a pairing code overrides its address (for an SSH tunnel on another port). It asks to confirm the address in a code like `install --pair` (`--yes` skips the question), tests the connection before changing anything, writes `app.env` and restarts the web service if it runs; if that restart fails its readiness checks, the previous connection is put back. `sudo bananachat status` shows the same summary, and the compute server's `status` shows its token fingerprint so the two can be compared.

**Rotating the tokens.** On the compute server run `sudo bananachat compute rotate-token`: it writes new tokens (the gateway token and the model management token, which it adds if the server has none yet), restarts the gateway (the old tokens stop working at once) and prints a new pairing code. On the web server run `sudo bananachat backend connect` and paste it; update the web server first, since a release before 1.7 ignores the management token and then cannot download or delete models. Doing it by hand works too: write new random values (`openssl rand -hex 32`) into the token files with mode 0600, restart the compute service, then on the web server run `backend connect --url https://compute.example.org --token-file FILE --management-token-file FILE` (or edit `BC_OLLAMA_API_KEY` and `BC_OLLAMA_MANAGEMENT_API_KEY` in its `app.env` and restart). The web server keeps its copies of the tokens in `app.env`, not in token files.

### Without a public name: SSH tunnel

Install the compute server without `--domain`; its pairing code then points at `http://127.0.0.1:11435`. Run a supervised SSH connection on the web server that forwards that loopback port to the compute server's gateway (for example `ssh -N -L 11435:127.0.0.1:11435 compute`), then pair as above. If the tunnel uses another local port, add `--url http://127.0.0.1:PORT` (to `install` as `--backend-url`, or to `backend connect`), or print a code for it on the compute server with `compute pairing-code --url`. Direct unencrypted remote HTTP is rejected.

The web server treats a tunnel like any remote compute server: it never checks its own memory, disk or GPU for inference, and it probes the compute server so an outage shows a banner instead of failing requests (see `BC_INFERENCE_LOCAL` in [configuration](configuration.md) for unusual setups).

## One server

```sh
git clone https://github.com/BananaSuite/BananaChat.git
cd BananaChat
git checkout "$(git tag --list 'v*' --sort=-v:refname | head -n 1)"
sudo ./banana install --mode single --domain chat.example.org
sudo bananachat proxy --install
```

Point DNS at this server and allow HTTPS ports 80/443. `proxy` prints a matching Caddyfile. `proxy --install` validates it before loading it, and preserves an unrelated existing proxy unless you explicitly select `--replace`. On a server with other sites, merge the printed block into the existing proxy configuration, global options included. `--email` sets the address Let's Encrypt writes to about certificates.

### Behind Cloudflare

`proxy` looks up the domain. When it resolves only to Cloudflare's addresses, the record is proxied (orange cloud): the Caddyfile then skips the TLS-ALPN challenge, which can never pass Cloudflare's proxy, and the command says so. Pass `--cloudflare` or `--no-cloudflare` to decide yourself, for example before DNS points at Cloudflare. The choice is not stored, so repeat it when you run `proxy` again.

The site answers on port 80 as well as 443, which Let's Encrypt's HTTP-01 challenge needs. Cloudflare's "Always Use HTTPS" must be off, or a rule must skip `/.well-known/acme-challenge/`, or the first certificate cannot be issued. Wildcard and origin certificates are not supported here. Under SSL mode Flexible, a request Cloudflare already received over HTTPS is served rather than redirected back to itself.

The visitor's address is taken from `CF-Connecting-IP` for requests that come from Cloudflare (Caddy 2.7 or newer; with an older Caddy every visitor appears with a Cloudflare address, and `proxy` warns). The idle timeout is 16 minutes, above the 15 minutes for which Cloudflare reuses an idle connection to the origin, so a reused connection is never one Caddy has just closed.

The chat service and Ollama bind to loopback. Model files are stored under `/opt/bananachat/data/models`; download models in Admin → Models (or with the Ollama CLI) and enable them there. Add `--ollama-url http://127.0.0.1:11434` to use an Ollama that already runs on the machine instead, exactly as for a compute server. Create the first administrator with the setup token as described above.

## Update policy and private repositories

```sh
sudo bananachat source check
sudo bananachat update
sudo bananachat updates enable --require-signatures /root/allowed_signers --interval 60
sudo bananachat updates status
sudo bananachat updates disable
```

`source check` shows what an update would deploy, without deploying it. Updates follow **release tags** by default: the highest `vX.Y.Z` tag of the source, never work pushed after it. Until the source has one, an update reports that there is nothing to update to. A source configured before 1.7 keeps following its branch until `source set --track tags`. Automatic checking uses a systemd timer with a small randomised delay. Disabling updates prevents the next update; an already applying transaction finishes safely. An intentionally stopped application is not restarted by the updater.

An automatic update installs new code as root with nobody reviewing it, so `updates enable` requires signature checks (`--require-signatures`, now or configured before; see [below](#what-a-bad-commit-can-and-cannot-do)). `--allow-unsigned` turns automatic updates on without them: then anyone who can push to the source can run code on your server, and `status` warns about it. Clearing the requirement while automatic updates are on (`source set --clear-signatures`) needs `--allow-unsigned` too.

To follow a branch's latest commit instead (your own fork, for example), with an optional fallback:

```sh
sudo bananachat source set --repo https://forge.example.org/team/BananaChat.git --track branch --branch stable --fallback-branch main
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

Obtain and verify the forge's SSH host key through a trusted channel first. Strict host checking is enforced. Repository credentials are stored as root-private files under `config/`, kept out of URLs and Git configuration, and passed only to the updater's Git process. They are separate from the compute API credentials. Repeat `source set` with a new credential file to rotate access; `source set --clear-credentials` removes it. Changing hosts without a replacement credential disables authentication.

Each release installs its Python dependencies from `requirements.lock`: every package, dependencies included, at the version and file hashes the release was tested with, wheels only (`pip install --require-hashes --only-binary=:all:`), so nothing is resolved anew on your server and no package build script runs there. A release without a lock (a rollback to an older one) installs its ranges, still wheels only.

Each update prepares the new release, checks that there is disk space for its rollback package and for a rollback, stops the remembered services, creates a backup (model weights stay in place rather than being copied into it), switches source, and checks readiness before allowing traffic. Failed readiness restores the old code and data together. A failed revision is not retried, automatically or manually, until you correct the problem and run `update --retry-failed` (or publish a newer corrected commit). For each kind of package the tool creates by itself (`auto`, `before-update`, `before-restore`, `before-legacy-import`, `remote`), the newest three are retained by default; set `--keep-backups` on `updates enable` to change that policy. Packages you create with `backup`/`migrate` are never removed, and neither is the package needed for `rollback`. Prepared releases use the same retention count, always preserving the active and previous releases.

## Updating to a new release

Read [UPGRADING.md](../UPGRADING.md) before updating: it lists every change that needs a decision or an action. The update itself takes the normal path:

```sh
sudo bananachat source check
sudo bananachat update
```

The installed updater prepares the new code, backs everything up, starts it and checks `/health` before letting users back in; if anything fails it restores the old code together with the old database. On first start the database is upgraded in place (the current release uses schema version 26). New optional settings are listed in [configuration](configuration.md) and in the [changelog](../CHANGELOG.md). After updating, reload open browser tabs. For a manual installation, take a complete backup before starting the new release and restore its matching code and database if you roll back.

Web and compute servers update independently, each with its own timer; if a release needs an order, UPGRADING.md says so.

A web server that reaches its compute server through an SSH tunnel (a loopback `BC_OLLAMA_URL` with `BC_OLLAMA_API_KEY`) treats it as remote: it does not check its own memory and shows an outage banner when the compute server is unreachable. `BC_INFERENCE_OUTAGE_MODE=fallback` needs an explicit `BC_INFERENCE_FALLBACK_URL`; without one the server logs a warning and pauses new answers during an outage.

## What a bad commit can and cannot do

The updater assumes the source repository can go wrong, whether through a mistake or through someone who should not have push access.

A commit that deletes the application cannot take the deployment with it. Nothing is switched until a release has been prepared from the new revision, and a revision that is empty or missing the managed entry point is refused at that point, with the running version untouched. A revision that installs and starts but fails its readiness checks is rolled back to the previous code and its matching database. Either way the failed revision is recorded and never retried automatically. Chat history, settings and model recipes live outside the source tree, so no commit can delete them.

Rewritten history is refused as well. An automatic update only moves forward from the installed revision, so a force-push that replaces history pauses updates instead of applying them, and switching to unrelated history takes a deliberate `update --allow-divergent` from a maintainer.

Readiness checks do not catch a hostile commit that works. If the update source can be compromised, require signatures:

```sh
sudo bananachat source set --require-signatures /root/allowed_signers
```

The file is an SSH allowed-signers file, one line per key you trust:

```text
you@example.org namespaces="git" ssh-ed25519 AAAAC3Nza...
```

With it in place, updates deploy only signed code:

- A server that follows release tags deploys only an annotated tag (`git tag -s vX.Y.Z`) signed by a listed key for its own name. A signed commit under an unsigned or lightweight tag does not count, and neither does a signed tag pushed under another name.
- A server that follows a branch needs every commit signed by a listed key.
- The highest release that verifies is deployed. Higher tags that do not verify are skipped and listed in the update result (`skipped_releases`) and in a warning.
- Only the 20 highest tags are tried, so 20 tags that do not verify hold updates back. Let only maintainers create `v*` tags on the update source.
- Anything else stops the update before anything is fetched into a release. The running version stays as it is.

To sign, set `gpg.format=ssh` and `user.signingkey`, then sign releases with `git tag -s` (and commits with `git commit -S` for servers that follow a branch). A restore accepts the same signatures: the commit's own, or a signed release tag of the update source that names it. `source set --clear-signatures` returns to unsigned updates.

This checks SSH signatures only. An OpenPGP-signed commit is refused, and the message says so. That is deliberate: Git picks its verifier from the signature itself, so an OpenPGP signature would be checked against whatever the server's GnuPG keyring happens to trust instead of against the file you configured. If you sign with OpenPGP today, move the signing key to SSH format for the server you want protected.

Keep the allowed-signers file on the server, owned by the operator and mode 0600; the updater refuses to read it otherwise, and refuses to update rather than carrying on unverified if it goes missing. Signature checks protect the code only: a compromised token still lets someone read a private repository.

## Backup, move, and restore

[Repository backups](backups.md) add optional encrypted storage and restore through private GitHub or Forgejo repositories. Install age and configure `bananachat backups` separately on each server, using a unique series name per role. Scheduling stays off until `backups enable`. Git backups exclude weights and save model download recipes. After a restore the web server checks by itself which of those models its model server lacks: if none, nothing is asked; otherwise Admin → Overview shows one card to download them, choose some, or dismiss the list (see [model recovery](backups.md#model-recovery-and-existing-chats)). A compute server that is restored or rebuilt needs nothing on its own side: the paired web server notices the published models it no longer has and offers them the same way. Only a compute server used without a web server downloads its saved list with `bananachat models status` and `models restore --yes`.

```sh
sudo bananachat migrate --output /root/bananachat-migration.tar.gz
```

`backup` and `migrate` create the same portable package. They briefly stop services for a consistent snapshot and return them to their prior running state. The package contains source, data, session/API keys, environment settings, private Git credentials, and managed model files. Allow enough disk space for these, especially on a compute server. Keep external ComfyUI data, GPU drivers, and any paths outside the managed root backed up separately.

Packages have mode `0600` but are **not encrypted**. Transfer them over SSH and store them privately off the server. For a final migration, prevent new writes on the old server after the final package, test the new server, then change routing/DNS. Do not leave two independently writable copies serving the same users.

On a new server with the prerequisites and a compatible clean checkout:

```sh
sudo ./banana install --restore /root/bananachat-migration.tar.gz
# It stops before changing anything and shows the package's source revision; if you made the package:
sudo ./banana install --restore /root/bananachat-migration.tar.gz --trust-source REVISION
sudo bananachat status
sudo bananachat proxy
```

This restores the saved mode, compute connection and exact bundled source without fetching the update repository. The bundled source runs with root privileges for maintenance, so a new server deploys it only once you confirm its revision with `--trust-source` (see [Restore or move servers](backups.md#restore-or-move-servers)). `--mode`, `--pair`, `--backend-url` and `--skip-connection-check` are refused here; change the connection afterwards with `backend connect`. Installing Python dependencies still needs package-registry access. `single` and `compute` also need Ollama installed locally; add `--ollama-url http://127.0.0.1:11434` to restore onto an Ollama that already runs on the new machine instead of a managed one. Add `--domain new.example.org` when changing the public hostname, then configure the proxy and review integration URLs.

To restore after installing an empty matching mode, run `sudo bananachat restore PACKAGE`. It saves a `before-restore` package first and puts the previous data back if readiness checks fail. Restore packages only from trusted operators: they include executable source and secrets.

- Ollama: the server keeps its own choice. A managed Ollama stays managed and an existing one (`--ollama-url`) stays in use, whichever the package came from.
- Mode: restore cannot convert between `web`, `single` and `compute`. For a role change, use a separate root and migrate the data and backend deliberately.
- Code on an installed server: it comes from a release the server already prepared or from its own update source (signature-checked when signatures are required). The server asks for `--trust-source REVISION` only when neither has the package's revision.
- Code when signatures are required: the server never deploys the package's own source, even with `--trust-source`. The refusal says how to allow it once.
- Update settings on an installed server: it keeps its own update source, repository credentials and allowed signers. The result's `source_warning` says when the package's differ, and `source set` changes them.
- Update settings on a new server: it takes the package's.

New packages include `config/repo.allowed_signers` when SSH signatures are required for updates. Packages made by earlier releases may omit it; after restoring one onto a new server, recover the operator's original allowed signers file and configure `source set --require-signatures FILE` before updating that installation. Signature verification fails closed while the trust file is missing.

**Automatic updates are disabled after every restore.** Verify login, chat history, attachments, models, and a streaming reply before enabling them again. For a split deployment, create a separate package for each server; after moving either one, run `sudo bananachat backend test` on the web server, and `backend connect` with a fresh pairing code if the compute address or token changed.

`sudo bananachat rollback` restores the code/data package saved before the last successful update. `rollback --package PATH` selects another package. `sudo bananachat recover` recovers an interrupted transaction from its durable journal. Restoring old code alone is insufficient after a database migration.

## Older installations and optional components

To move from an installation older than 1.6, keep the old checkout and services available while you test the new managed installation. Export the old chat database from Admin → Migration, then import it into the installed single or web server with `sudo bananachat restore --legacy-database /root/bananachat_export.tar.gz`. The command stops all managed processes, makes a rollback package and checks start-up before reopening. While services are stopped, copy persistent session keys, uploaded files, operator configuration and anything else the export omits into the managed data directory; audio from `app/static/audio` goes to `data/audio`. Preserve ownership and private permissions, restart, and check everything before switching users. Old application exports are not the same format as `banana` installation packages.

Volunteer [worker PCs](workers.md) are configured separately. Dedicated inference servers use `--mode compute` (see [compute server](compute.md)). [ComfyUI](comfyui.md) and the optional checkpoint downloader keep their own data and credentials; configure and back them up separately.

## Status and removal

Use `sudo bananachat status` and `sudo journalctl -u bananachat` for the current revision and service logs. `status` also summarises inference: the compute address and token fingerprint on a web server, the gateway address, token fingerprint and Ollama (managed or existing) on a compute server, and any model list waiting after a restore. `start`, `stop`, and `restart` manage the remembered service set, including a managed Ollama; an existing Ollama chosen with `--ollama-url` is never touched, and neither does `uninstall` remove it. Configuration is under `/opt/bananachat/config`, runtime data under `data`, prepared code under `releases`, and `current` selects the active source. Keep mutable data there instead of editing installed source. Operation results are saved privately in `config/history.jsonl`.

When someone lost both their authenticator app and their recovery codes and no administrator can reset it in Admin → Users (for example the only administrator), `sudo bananachat reset-two-step USERNAME` on the single or web server turns off that account's two-step verification, signs it out everywhere and records the reset in the audit log; see [two-step verification](two-step-verification.md).

`sudo bananachat uninstall` disables updates and backup scheduling and removes managed services and the command while retaining files. An unchanged Caddyfile installed by this command is reverted to its prior configuration; a later operator edit is retained for manual adjustment. Shared packages, system accounts, and other services are retained. Reinstall preserved data from a clean checkout with the same root/mode/name and `install --reuse-data`; this is also how a compute server switches between a managed Ollama and an existing one (`--ollama-url`).

Permanent deletion requires `sudo bananachat uninstall --purge --confirm bananachat`. This also removes local backups under the installation root; copy the migration package elsewhere first. For multiple installations use separate `--root`, `--name`, and `--port` values and merge proxy configuration deliberately.

## Optional desktop worker

Volunteer PCs can contribute their Ollama capacity to the web server. Read [workers](workers.md) for what that means for privacy, how to enable it (`BC_WORKERS_ENABLED`), how to register a worker in Admin → Workers and how to install the daemon on Linux, Windows and macOS.

## Source availability

AGPL-3.0-only permits personal and commercial use. Keep `BC_SOURCE_URL` pointing to corresponding source users can obtain. A private update repository does not remove the source-offer requirement for a modified network service; provide an accessible corresponding-source copy or authorised access. The updater preserves explicitly customised source-offer URLs.
