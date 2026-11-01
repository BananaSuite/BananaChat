# Encrypted repository backups

A managed BananaChat server can back up to a dedicated private GitHub or Forgejo repository. This is optional: installation and updates do not enable it. Local portable backups remain available.

| Deployment | Included in a Git backup |
| --- | --- |
| `single` or `web` | Accounts, chats, attachments, settings, matching source, credentials, and model download recipes. |
| `compute` | Managed configuration and API token, matching source, and the Ollama model names (read from an existing Ollama's API when one is used with `--ollama-url`; the backup stops with an explanation if that Ollama does not answer). |

Git backups **exclude model weights**. The web and compute servers need separate packages. External ComfyUI installations, checkpoint-agent directories, GPU drivers, and files outside the managed data directory need their own backup. The installed operating-system packages are reinstalled through the deployment prerequisites. [BananaWiki](https://github.com/BananaSuite/BananaWiki) stores its own backups in the same encrypted format; follow its backup documentation.

## Set up a destination

Install Git and [age](https://age-encryption.org/) through your operating-system packages. On Debian/Ubuntu, `sudo apt install age` supplies `age` and `age-keygen`.

Create a private repository for backups. It may be empty. Keep it separate from application source; one backup repository can hold multiple installations if each has a distinct series name. Give the web and compute servers different series names. GitHub Enterprise and Forgejo at a custom HTTPS address are supported. Backup transport uses HTTPS; SSH remains an option for the separate source updater.

Create a **separate** backup token with Contents read/write and Metadata read on that GitHub repository, or repository read/write on the Forgejo repository. Store it in an operator-owned mode-`0600` file, such as `/root/banana-backup.token`. Do not put tokens in URLs, shell arguments, or source files. Source updater credentials can stay read-only.

By default each upload also deletes the snapshots beyond `--keep`, so the token needs the right to delete branches, and so does anyone who takes over the server. To keep the off-site copies out of reach of a compromised server, configure the series with `--append-only` and protect its branches on the forge: on GitHub, a repository ruleset for `refs/heads/banana-backups/**` that restricts deletions and blocks force pushes; on Forgejo, a protected-branch rule for `banana-backups/**`. Uploads then only create new branches. Apply retention from time to time with a token that may delete branches, supplied only for that command (it is not stored):

```sh
sudo bananachat backups prune --token-file /root/banana-backup-admin.token
```

`prune` keeps the `--keep` newest authenticated snapshots of the series (see below). With deletion blocked on the forge, lift the rule while pruning, or delete old `banana-backups/bananachat/SERIES/…` branches in the forge's interface.

Retention (`prune`, and the pruning after each upload) counts only snapshots that the series' recovery key authenticated, newest first. It deletes only what is older than the `--keep`-th of them, so it never leaves fewer than `--keep` authenticated snapshots. Snapshot IDs are branch names that anyone who can push may choose, so retention leaves other branches alone: unauthenticated snapshots newer than the cut-off are kept and not counted, and IDs dated more than a day in the future, or that are not dates, are neither counted nor deleted. Branches pushed with a stolen upload token therefore cannot make retention delete genuine snapshots. Retention lists them in the result's `warnings`; check who pushed them and delete them on the forge. Each snapshot it examines is fetched into a repository of its own that is removed straight after, and a run reads at most 40 unauthenticated snapshots: past them, retention reads and deletes nothing older and says so in `warnings`, and an upload stops with an error before anything is pushed when that many come before the series' newest authenticated snapshot (it could not tell when the series' authentication started). Delete those branches and run the backup again.

Where the forge allows partial fetches (GitHub does), retention reads only each snapshot's index; elsewhere it downloads each snapshot it checks.

```sh
sudo bananachat backups keygen --output /root/banana-recovery.agekey
sudo bananachat backups configure \
  --forge github --repo https://github.com/YOUR_TEAM/private-backups.git \
  --name chat-web --username YOUR_BOT \
  --token-file /root/banana-backup.token --key-file /root/banana-recovery.agekey \
  --keep 7 --max-mib 512  # add --append-only for an append-only series
sudo bananachat backups run
sudo bananachat backups list
sudo bananachat backups status
```

For Forgejo, use `--forge forgejo --repo https://forge.example.org/YOUR_TEAM/private-backups.git`. Set `--username` to the account your forge requires; GitHub installation tokens commonly use `x-access-token`. A custom service name installed with `install --name` replaces `bananachat` in these commands.

**Save an offline copy of the recovery key before relying on this backup.** Losing the server and that key makes the encrypted backups unrecoverable, even with repository access. The operator-only working copy and backup token live under `config/remote-backup/`; neither is included in its own backup. `keygen` prints the public recipient and the key filename, never the secret identity. Keep the original key for older snapshots when rotating keys; use a new series name for the new key. Running `configure` again (for a new token, `--keep` or `--max-mib`) without `--key-file` keeps the key the server holds; a different key replaces it only with `--replace-key`, since only the old key restores the snapshots made with it.

The recovery key also authenticates every snapshot: each `index.json` carries an HMAC-SHA256, keyed from the recovery key, over the snapshot's identity and the checksum of every encrypted part. Encryption alone cannot do this, because anyone who knows the public recipient (it is in the series' `identity` branch) can encrypt a package of their own. Restores, downloads and `verify` check the tag before decrypting anything, so a snapshot pushed by someone with write access to the repository but without the recovery key is refused.

The server keeps its working copy of the key, because it authenticates each upload and verifies it by downloading it again. Whoever takes over the server can therefore read every snapshot still retained in the series, including chats deleted since. Keep `--keep` as small as your recovery needs allow, and start a new series with a new key (keeping the old key offline) after a suspected compromise.

The tool checks repository privacy through the forge API before each transfer. It refuses public, archived, or mirrored destinations and does not follow credential-bearing redirects. A visibility or permission change stops future transfers. Encryption also protects the payload if the repository later becomes public.

## Schedule, inspect, and stop

After a successful manual backup and restore rehearsal:

```sh
sudo bananachat backups enable --interval 1440
sudo bananachat backups status
sudo bananachat backups disable
```

The systemd backup timer is independent of automatic updates. The interval is in minutes, from 60 to 10080; the default is one day. A small randomised delay spreads load. These are intervals, not exact clock times. `backups disable` prevents new scheduled uploads; an upload already underway can finish verification and retention. Reconfiguring or restoring disables the schedule. Uninstall removes the timer and services but keeps files unless you explicitly purge the installation.

`journalctl -u bananachat-backup.service` shows scheduled failures. `backups status` includes the last verified remote snapshot and any retention warnings. If upload fails, the tool keeps the private local package and reports its path; free that disk space only after recovering a verified off-server copy. Periodically check the journal and rehearse restoration. The system does not send external failure notifications.

The tool stops application writers only while creating the consistent local package, then restores their prior running state before network transfers. Busy or interrupted maintenance fails visibly and uses the recovery journal. A failed upload is reported as failed.

## Restore or move servers

Use an exact ID returned by `backups list` in place of `SNAPSHOT_ID`:

```sh
sudo bananachat backups verify SNAPSHOT_ID
sudo bananachat backups download SNAPSHOT_ID --output /root/recovered-chat.tar.gz
sudo bananachat restore /root/recovered-chat.tar.gz
```

Or download, verify, and restore in one operation:

```sh
sudo bananachat backups restore SNAPSHOT_ID
```

`download` produces an ordinary private **unencrypted** portable package. Existing output files are never overwritten. `verify` checks the snapshot's authentication tag, its identity, the age decryption, and the full package checksum; a restore additionally validates the package, the application database, the deployment, and readiness.

Snapshots uploaded by 1.6 have no authentication tag, so they cannot be told apart from forged ones. `verify`, `download` and `restore` refuse them unless you pass `--allow-unauthenticated`, and then print a warning. Even then, a snapshot dated at or after the series' first authenticated snapshot, or dated in the future, is refused. Every authenticated snapshot records that date, so the check still works after retention deleted the first one. Use the option only for a snapshot you know predates the upgrade, from a repository whose writers you all trust, and take a new authenticated backup afterwards.

A server that goes back to 1.6 with backups scheduled keeps uploading unauthenticated snapshots to the same series, and once 1.7 runs again they are refused like forgeries, because they are newer than its authenticated ones. Run `backups disable` before going back and `backups enable` after updating again, or configure another series name under 1.6. To recover such a snapshot by hand when you know this server made it, fetch its branch, decrypt its parts in order, and drop the two header lines:

```sh
git clone --depth 1 --branch banana-backups/bananachat/SERIES/SNAPSHOT_ID https://github.com/YOUR_TEAM/private-backups.git snapshot
cat snapshot/part-*.age | age --decrypt --identity /root/banana-recovery.agekey | tail -n +3 > /root/recovered-chat.tar.gz
sudo bananachat restore /root/recovered-chat.tar.gz
```

A package contains the source code of the release it was made with, and restoring it deploys that code, which runs as the service and, through the installed `bananachat` command, as root. A restore therefore uses the code bundled in a package only when nothing better is available:

1. If this server already prepared that revision (a rollback, or a restore of its own recent package), it reuses that release.
2. Otherwise, if this server's configured update source has the revision, the release is built from the source, after checking its SSH signature when the source requires signed revisions. The bundled copy is not used.
3. Otherwise, for example on a new server, the restore stops before changing anything and shows the revision, the source and branch recorded in the package, and when it was made. If you made the package or trust whoever did, run the same command again with `--trust-source REVISION`. A server whose update source requires signed revisions never deploys an unverified revision from a package.

A release from 1.7 on installs exactly the dependencies its `requirements.lock` and `requirements-build.lock` pin. If the snapshot's release lacks them (because it was deployed with `update --allow-unpinned`), the restore stops before changing anything; `backups restore SNAPSHOT_ID --allow-unpinned`, like `restore` and `rollback`, installs its dependencies from the `requirements.txt` ranges instead, without hash checks.

Restoring a package also restores its data and configuration, so only restore packages you or your team made.

On a new server, install the prerequisites from the [deployment guide](deployment.md), including age. From a compatible reviewed checkout, configure the original repository, series name, and saved key at the new root, then restore:

```sh
sudo ./banana --root /opt/bananachat backups configure \
  --forge github --repo https://github.com/YOUR_TEAM/private-backups.git \
  --name chat-web --username YOUR_BOT \
  --token-file /root/banana-backup.token --key-file /root/banana-recovery.agekey
sudo ./banana --root /opt/bananachat backups list
sudo ./banana --root /opt/bananachat backups restore SNAPSHOT_ID
# A new server shows the snapshot's source revision; confirm it:
sudo ./banana --root /opt/bananachat backups restore SNAPSHOT_ID --trust-source REVISION
sudo bananachat status
sudo bananachat proxy
```

The saved mode is restored automatically. `--name` on `backups restore` selects a service name on an empty installation; `--domain` and `--port` permit an intentional address change. On an existing installation, restore first makes a local `before-restore` package and rolls back if the replacement fails readiness. Re-check configuration, the web and compute token pairing, and proxy routing before admitting users. Automatic updates and backup scheduling stay off until explicitly re-enabled. Keep the old server read-only throughout final migration so two copies do not diverge.

## Model recovery and existing chats

Restoring a backup never starts a registry download by itself, and it asks only about models that are actually missing. Old queued or pulling jobs in the restored database are cancelled before workers can see them.

**On the web (or single) server**, the one place to decide in a two-server setup, since it downloads through the compute gateway:

1. After the restore, BananaChat compares the saved model list with what its model server (the compute server, or the local Ollama of a single server) and ComfyUI have. It does this in the background every minute and whenever an administrator opens the overview or the models page.
2. If everything is installed, the list is closed silently and nothing is shown.
3. Otherwise **Admin → Overview** shows one card, for example "2 models from the backup are missing on the compute server (about 9.5 GB)", with **Download them**, **Choose…** (Admin → Models, where the missing models are listed with checkboxes) and **Dismiss**. The size appears when it is known from the catalogue before the backup.
4. The card closes by itself once the downloads have finished. **Not now** postpones it, and **Dismiss** closes it for good: nothing stays on the dashboard forever, and a dismissed model is not offered again unless it is installed and later lost again.

A compute server that is restored from its own backup, or replaced by an empty one, needs no action on its side: the web server notices the published models its compute server no longer has and shows the same card ("published models not installed"). The restore command says which of these happens next.

Ollama names include the saved tags and supported Hugging Face GGUF names. Hugging Face safetensors recipes retain the repository, revision, filename, target name, expected size, and SHA-256 from successful checkpoint downloads. The checkpoint agent performs those downloads and verifies their digest. Preserve or recreate its credentials and enable the integration before downloading them. A manually installed model or cleared pull history may have no retained recipe; the card lists it as needing manual action until it is installed or dismissed. Custom weights that cannot be downloaded again need a separate full backup.

A compute server used **without** a web server (for API clients only) downloads its saved list from the command line; installed models are skipped and the list closes itself when nothing is missing:

```sh
sudo bananachat models status
sudo bananachat models restore --yes
# Or leave all downloads for later:
sudo bananachat models skip
```

The explicit `--yes` approves the displayed missing Ollama models; they are downloaded through the configured `BC_COMPUTE_UPSTREAM`. Only Ollama library and Hugging Face names are downloaded: a model from any other registry (such as `intranet-host/team/model`, even an existing Ollama's) is listed for manual review instead, with the same rule the web server applies. Registry changes, gated or private repositories, missing artifacts, disk limits, or incompatible hardware can require manual intervention; the original source and digest are kept for review.

Chat history is kept. When a chat's text model is missing, an installed model the user may use answers instead; images still need a vision model. A model that fails before producing output can be retried with another permitted model, and the chat shows the substitution. If no suitable model works, the error is shown and the conversation is kept.

`sudo bananachat backup --exclude-model-weights --output /root/chat-data.tar.gz` creates the same lightweight local package. Plain `backup` and `migrate` still include managed weights, for a complete copy that needs no download.

The packages taken automatically before an update, a restore or a legacy database import leave the weights out. Those operations never change the weights, and compressing a model library into each package would keep the service down longer and need its size again in free disk space.

- If the operation fails, the rollback restores the data and keeps the weights already on disk. A restore sets the replaced weights aside until it has succeeded.
- Restoring a package without weights on the same server keeps the models it already has; the card above appears only for models that are really missing.
- Before stopping anything, an update or restore checks that there is room for its package and for extracting it again in a rollback. If there is not, it refuses and says how much space it needs.

## Retention and Git limits

Each snapshot is an independent commit on `banana-backups/bananachat/SERIES/SNAPSHOT_ID`. A small permanent `identity` branch binds that series to its public recovery recipient. The tool never pushes to application source branches. It uploads encrypted parts of at most 32 MiB, downloads the result through a fresh Git repository, authenticates and decrypts it, and checks the full checksum before deleting old snapshot branches in that series (an append-only series skips that step; see `backups prune`). Updates to branches use leases to prevent overwriting concurrent changes. Other products, series, and unrelated branches are preserved.

The default maximum is 512 MiB per compressed package, configurable up to 1024 MiB. Encryption prevents efficient deduplication, so retained full snapshots consume storage. GitHub/Forgejo repository and push quotas still apply; splitting files does not bypass those quotas. Retention removes branch references; the hosting provider controls when unreachable objects and reflogs are garbage-collected, so space may not shrink immediately. Deleted snapshots may remain recoverable by the forge operator until its retention and garbage collection complete.

Allow staging space for the local package, encryption, and a verification download. Under `config/remote-backup/`, `verify`, `download` and `restore` reserve three times the configured package limit plus 128 MiB; `run` needs four times the package plus 144 MiB and checks that before it uploads anything. Working files that an interrupted command leaves there are removed by the next backup command. Larger installations should keep using suitable file or object backup storage. A backup in the same forge or account does not replace an independent offline copy and a tested recovery key.
