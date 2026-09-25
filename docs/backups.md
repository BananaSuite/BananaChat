# Encrypted repository backups

A managed BananaChat server can back up to a dedicated private GitHub or Forgejo repository. This is optional: installation and updates do not enable it. Local portable backups remain available.

| Deployment | Included in a Git backup |
| --- | --- |
| `single` or `web` | Accounts, chats, attachments, settings, matching source, credentials, and model download recipes. |
| `compute` | Managed configuration and API token, matching source, and local Ollama model names. |

Git backups **exclude model weights**. The web and compute servers need separate packages. External ComfyUI installations, checkpoint-agent directories, GPU drivers, and files outside the managed data directory need their own backup. The installed operating-system packages are reinstalled through the deployment prerequisites. [BananaWiki](https://github.com/BananaSuite/BananaWiki) and [BananaVibe](https://github.com/BananaSuite/BananaVibe) store their own backups in the same encrypted format; follow the backup documentation in those repositories.

## Set up a destination

Install Git and [age](https://age-encryption.org/) through your operating-system packages. On Debian/Ubuntu, `sudo apt install age` supplies `age` and `age-keygen`. No model API is involved in server backups.

Create a private repository for backups. It may be empty. Keep it separate from application source; one backup repository can hold multiple installations if each has a distinct series name. Give the web and compute servers different series names. GitHub Enterprise and Forgejo at a custom HTTPS address are supported. Backup transport uses HTTPS; SSH remains an option for the separate source updater.

Create a **separate** backup token with Contents read/write and Metadata read on that GitHub repository, or repository read/write on the Forgejo repository. Include branch creation/deletion rights within the backup namespace. Store it in an operator-owned mode-`0600` file, such as `/root/banana-backup.token`. Do not put tokens in URLs, shell arguments, or source files. Source updater credentials can stay read-only.

```sh
sudo bananachat backups keygen --output /root/banana-recovery.agekey
sudo bananachat backups configure \
  --forge github --repo https://github.com/YOUR_TEAM/private-backups.git \
  --name ai-web --username YOUR_BOT \
  --token-file /root/banana-backup.token --key-file /root/banana-recovery.agekey \
  --keep 7 --max-mib 512
sudo bananachat backups run
sudo bananachat backups list
sudo bananachat backups status
```

For Forgejo, use `--forge forgejo --repo https://forge.example.org/YOUR_TEAM/private-backups.git`. Set `--username` to the account your forge requires; GitHub installation tokens commonly use `x-access-token`. A custom service name installed with `install --name` replaces `bananachat` in these commands.

**Save an offline copy of the recovery key before relying on this backup.** Losing the server and that key makes the encrypted backups unrecoverable, even with repository access. The operator-only working copy and backup token live under `config/remote-backup/`; neither is included in its own backup. `keygen` prints the public recipient and the key filename, never the secret identity. Keep the original key for older snapshots when rotating keys; use a new series name for the new key.

The tool checks repository privacy through the forge API before each transfer. It refuses public, archived, or mirrored destinations and does not follow credential-bearing redirects. A visibility or permission change stops future transfers. Encryption also protects the payload if the repository later becomes public.

## Schedule, inspect, and stop

After a successful manual backup and restore rehearsal:

```sh
sudo bananachat backups enable --interval 1440
sudo bananachat backups status
sudo bananachat backups disable
```

The systemd backup timer is independent of automatic updates. The interval is in minutes, from 60 to 10080; the default is one day. A small randomized delay spreads load. These are intervals, not exact clock times. `backups disable` prevents new scheduled uploads; an upload already underway can finish verification and retention. Reconfiguring or restoring disables the schedule. Uninstall removes the timer and services but keeps files unless you explicitly purge the installation.

`journalctl -u bananachat-backup.service` shows scheduled failures. `backups status` includes the last verified remote snapshot and any retention warnings. If upload fails, the tool keeps the private local package and reports its path; free that disk space only after recovering a verified off-server copy. Periodically check the journal and rehearse restoration. The system does not send external failure notifications.

The tool stops application writers only while creating the consistent local package, then restores their prior running state before network transfers. Busy or interrupted maintenance fails visibly and uses the existing recovery journal; it does not claim an incomplete upload as a successful backup.

## Restore or move servers

Use an exact ID returned by `backups list` in place of `SNAPSHOT_ID`:

```sh
sudo bananachat backups verify SNAPSHOT_ID
sudo bananachat backups download SNAPSHOT_ID --output /root/recovered-ai.tar.gz
sudo bananachat restore /root/recovered-ai.tar.gz
```

Or download, verify, and restore in one operation:

```sh
sudo bananachat backups restore SNAPSHOT_ID
```

`download` produces an ordinary private **unencrypted** portable package. Existing output files are never overwritten. `verify` checks age authentication, snapshot identity, and the full package checksum; a restore additionally validates the package, databases, deployment, and readiness. Only restore packages from trusted operators: the bundled source will run on your server.

On a new server, install the prerequisites from the [deployment guide](deployment.md), including age. From a compatible reviewed checkout, configure the original repository, series name, and saved key at the new root, then restore:

```sh
sudo ./banana --root /opt/bananachat backups configure \
  --forge github --repo https://github.com/YOUR_TEAM/private-backups.git \
  --name ai-web --username YOUR_BOT \
  --token-file /root/banana-backup.token --key-file /root/banana-recovery.agekey
sudo ./banana --root /opt/bananachat backups list
sudo ./banana --root /opt/bananachat backups restore SNAPSHOT_ID
sudo bananachat status
sudo bananachat proxy
```

The saved mode is restored automatically. `--name` on `backups restore` selects a service name on an empty installation; `--domain` and `--port` permit an intentional address change. On an existing installation, restore first makes a local `before-restore` package and rolls back if the replacement fails readiness. Re-check configuration, the web and compute token pairing, and proxy routing before admitting users. Automatic updates and backup scheduling stay off until explicitly re-enabled. Keep the old server read-only throughout final migration so two copies do not diverge.

## Model recovery and existing chats

After a restore without weights, **Admin → Models** shows the saved model list with **Download selected models** and **Not now**. Restoring a backup does not start any registry downloads. Old queued or pulling jobs in the restored database are cancelled before workers can see them. An administrator can approve selected downloads, defer them, retry failures, or install different models. Existing models are checked against the configured inference servers and skipped.

Ollama names include the saved tags and supported Hugging Face GGUF names. Hugging Face safetensors recipes retain the repository, revision, filename, target name, expected size, and SHA-256 from successful checkpoint downloads. The existing authenticated checkpoint agent performs those downloads and verifies their digest. Preserve or recreate its credentials and enable the integration before approving them. A manually installed model or cleared pull history may have no retained recipe; the recovery screen lists that model for manual action. Custom weights that cannot be downloaded again need a separate full backup.

On a compute-only server, which has no admin website:

```sh
sudo bananachat models status
sudo bananachat models restore --yes
# Or leave all downloads for later:
sudo bananachat models skip
```

The explicit `--yes` approves the displayed Ollama inventory. Hugging Face checkpoint recovery is handled from the web server's admin interface and configured checkpoint agent. Registry changes, gated or private repositories, missing artifacts, disk limits, or incompatible hardware can require manual intervention; the original source and digest are kept for review.

Existing chat history is preserved. A missing selected text model falls back to an installed model authorized for that user; vision attachments still require a vision model. A model that fails before producing output can trigger a bounded retry with another authorized model, and the chat displays the substitution. The tool never widens model access or substitutes an unapproved tag. It does not combine a partial answer with output from a different model. If no suitable model works, it reports the error and keeps the conversation.

`sudo bananachat backup --exclude-model-weights --output /root/ai-data.tar.gz` creates the same lightweight local package. Plain `backup` and `migrate`, and pre-update rollback packages, still include managed weights for a complete local rollback.

## Retention and Git limits

Each snapshot is an independent commit on `banana-backups/bananachat/SERIES/SNAPSHOT_ID`. A small permanent `identity` branch binds that series to its public recovery recipient. The tool never pushes to application source branches. It uploads encrypted parts of at most 32 MiB, downloads the result through a fresh Git repository, authenticates and decrypts it, and checks the full checksum before deleting old snapshot branches in that series. Updates to branches use leases to prevent overwriting concurrent changes. Other products, series, and unrelated branches are preserved.

The default maximum is 512 MiB per compressed package, configurable up to 1024 MiB. Encryption prevents efficient deduplication, so retained full snapshots consume storage. GitHub/Forgejo repository and push quotas still apply; splitting files does not bypass those quotas. Retention removes branch references; the hosting provider controls when unreachable objects and reflogs are garbage-collected, so space may not shrink immediately. Deleted snapshots may remain recoverable by the forge operator until its retention and garbage collection complete.

Allow staging space for the local package, encryption, and a verification download. Downloads reserve three times the configured package limit plus 128 MiB; upload needs additional package-sized working space. Larger installations should keep using suitable file or object backup storage. A backup in the same forge or account does not replace an independent offline copy and a tested recovery key.
