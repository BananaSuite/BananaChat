# Upgrading BananaChat

Updates keep the database, files, secret key and URLs. This page lists what
needs a decision or an action; the [changelog](CHANGELOG.md) lists every
change and [deployment](docs/deployment.md) explains the update itself.

On a managed server:

```sh
sudo bananachat source check
sudo bananachat update
```

The updater backs everything up, starts the new release and checks `/health`
before letting users back in. If anything fails it restores the old code and
database together.

## 1.7.2 to 1.7.3

One database migration (`v27_worker_maintenance`, schema version 27), additive
only. Nothing new is switched on by the update except what is listed here.
Going back to 1.7.2 is a normal `sudo bananachat rollback`, which restores the
database from before the update.

- **Hosted lane.** Requests to external providers and the subscription pools
  no longer count towards `BC_MAX_CONCURRENT`; they have their own limit,
  `BC_MAX_CONCURRENT_HOSTED` (16). Lower it if a provider rate-limits you.
- **Local providers.** Providers made from the LM Studio, vLLM and llama.cpp
  presets are marked *Runs on our own hardware*: their models now count as
  local models for the chat token settings, and while such a server is off its
  models are hidden. Untick the option on a provider that is not on your
  hardware.
- **Subscriptions.** Existing profiles and manifests keep working. To sign new
  accounts in from the page, run
  `sudo python3 /opt/bananachat/current/scripts/setup_subscriptions.py`
  (add `--allow-bwrap-userns` on Ubuntu 23.10 and later) and follow
  [Claude Code](docs/claude-code.md#quick-setup-sign-in-from-the-administration-page).
- **Worker maintenance access** is off until you set
  `BC_WORKER_REMOTE_ACCESS=1` and a worker's owner allows it. It uses
  WebSockets through the existing proxy; each open session holds an HTTP
  thread (`BC_WORKER_TUNNELS_PER_PROCESS`, default 4 per process).
- **Workers** keep working with their current daemon. Copy the new `worker/`
  folder to a PC for maintenance access, model management, hardware readings
  and reasoning relay; model management is on by default there
  (`BC_WORKER_MODEL_MANAGEMENT=0` refuses it).
- **BM mode** starts off for every account. Administration → Settings turns
  it on for everyone, or leaves it per account; people may switch their own
  unless you untick that there or lock an account on its page.
- **Easter eggs** stay as they were (all on); Administration → Settings →
  Easter eggs switches each off for the site.

## 1.7.1 to 1.7.2

One database migration (`v26_subscription_pool`, schema version 26) adds the
subscription pool tables, empty, and lets `ai_models.backend` be `codex`.
Nothing changes until you configure a pool. Going back to 1.7.1 is a normal
`sudo bananachat rollback`, which restores the database from before the update.

### Claude and Codex subscription pools

Shared subscription routing requires the provider's approval; read its terms
before enabling a pool ([router design](docs/subscription-router.md)). Anthropic
and OpenAI models can still be offered through an
[external API provider](docs/external-providers.md) instead.

- `BC_CLAUDE_EXTENSION` and `BC_CLAUDE_CODE_CONFIG` work again, so values left
  over from 1.6 reconnect the Claude pool on the first start. Delete them
  unless you mean to use it. If you upgrade from 1.6 straight to 1.7.2, skip the
  advice under "1.6 to 1.7" to remove `BC_CLAUDE_*`. Codex uses
  `BC_CODEX_EXTENSION` and `BC_CODEX_CLI_CONFIG`; see
  [configuration](docs/configuration.md#subscription-pools).
- The 1.6 pool's accounts are not carried over: the 1.7 upgrade dropped them.
  Add the accounts again under Administration → Models → Claude, then restore
  the models to offer from the catalogue.
- Managed installs give the service write access to the profile directories
  named in those manifests. Run `sudo bananachat restart` after changing a
  manifest.
- An exhausted pool answers `provider_capacity_unavailable` (503 with
  `Retry-After`); other busy providers still answer `provider_rate_limited` or
  `provider_overloaded` ([API](docs/api.md)).

### Proxy configuration

The next `sudo bananachat proxy --install` writes a different Caddyfile for
single, web and compute servers (a site on port 80 too, a redirect that does not
loop behind Cloudflare, trusted Cloudflare addresses, a 16 minute idle timeout).
Run `proxy` first to see it. A block you merged into another Caddy configuration
by hand needs the global options merged again. Add `--cloudflare` if the record
is proxied but does not point at Cloudflare yet.

## 1.7.0 to 1.7.1

There is no database migration and no required configuration change. Going
back to 1.7.0 is a normal `sudo bananachat rollback`.

### Linux workers

Worker units installed by earlier versions have a quoted `EnvironmentFile=`
line that systemd ignores (it logs a warning on every daemon-reload). The
worker still runs because it reads its settings file itself. To install the
corrected unit, on each worker computer:

```sh
python bananachat_worker.py install            # add --system if you installed it that way
python bananachat_worker.py stop && python bananachat_worker.py start
```

### Larger API requests

Chat completion bodies on `/v1` and in the playground can now carry their
images: 4 MB plus up to `BC_CHAT_MAX_REQUEST_MB` (default 8) of base64 images,
about 15 MB with the default settings. If a reverse proxy of your own caps
request bodies at 4 to 8 MB, raise the cap or API requests with large images
are still refused. The managed Caddy proxy sets no cap.

### Compute gateway

`BC_COMPUTE_MAX_CONNECTIONS` now counts only requests with a valid token;
clients still checking their token wait separately. Nothing changes on the
compute server's configuration.

### Encrypted Git backups

- `backups configure` keeps the stored recovery key when `--key-file` is left
  out. A script that rotates keys by running `configure` with a new key file
  must add `--replace-key`.
- `backups restore` holds the backup lock until it is done, so a scheduled
  `backups run` that starts during a restore fails with "Another backup
  operation is running" and keeps its local package.
- `backups run` needs four times the package size plus 144 MiB free under
  `config/remote-backup` and checks this before uploading.

### German interface

The interface is now also in German. Browsers that prefer German, which
until now got `BC_DEFAULT_LANGUAGE`, now get German; people who picked a
language keep it. `BC_DEFAULT_LANGUAGE` also accepts `de`.

### Translation forks

If you keep your own copy of the i18n catalogues: `community.rules` and
`account.quota_community_hint` take `{window}` (a phrase such as "72 hours")
in place of `{hours}`, and several texts with a number are `{one, other}`
objects. Every catalogue has a `de` section; keys missing from it fall back
to English.

## 1.6 to 1.7

1.7 updates a 1.6 installation in place. The database is upgraded to schema
version 25; accounts, chats, attachments, personalities, quotas and API keys
are kept, and so are sign-ins on HTTPS servers for browsers used within a day
(see [Session cookie](#session-cookie)).

### Before you update

1. **Check the update source.** `sudo bananachat source check` shows what an
   update would deploy. Servers installed before 1.7 follow their branch; see
   [Follow release tags](#follow-release-tags).
2. **Keep the secret key with the data.** `BC_SECRET_KEY` (or `.secret_key`)
   also encrypts provider API keys and authenticator secrets. Managed backups
   include it.
3. **Remove `BC_CLAUDE_*` settings** from `config/app.env`.
4. **Split deployments:** update the web and compute servers in either order,
   but rotate the compute token only once the web server runs 1.7.
5. **Free space.** The update to 1.7 is run by the installed 1.6 tool, whose
   pre-update backup on a single or compute server still copies every model's
   weights. Make room for that copy.

The upgrade runs five migrations; only the first removes anything:

| Migration | What it does |
| --- | --- |
| `v21_remove_claude_connector` | Retires the Claude subscription models and drops the connector's tables and state |
| `v22_api_token_scopes` | Key scope and expiry; existing keys keep full access and never expire |
| `v23_external_provider_presets` | Provider presets and key headers, inferred from each address; Gemini models enrolled with 1.6 keep their public ID |
| `v24_two_step` | Two-step verification and the administrator requirement (off) |
| `v25_run_owner_worker_backoff` | Run owners, usage records of running API answers, worker pauses, stored attachment sizes, sharing blocks |

After updating, reload open browser tabs.

### Follow release tags

New installations deploy the highest `vX.Y.Z` tag. A server installed before
1.7 keeps following its branch until you switch it:

```sh
sudo bananachat source set --track tags
sudo bananachat source check
```

### Automatic updates need signatures

```sh
sudo bananachat updates enable --require-signatures /root/allowed_signers --interval 60
```

With release tags, the signature must be on an annotated tag made for that
version (`git tag -s vX.Y.Z`); a server that follows a branch needs signed
commits. Only the 20 highest tags are tried, so let only maintainers create
`v*` tags on the update source. `--allow-unsigned` enables automatic updates
without signatures; anyone who can push to the source can then run code on
your server as root. Automatic updates enabled before 1.7 keep running, with a
warning while they are unsigned.

### Hash-pinned dependencies

Releases install `requirements.lock` and `requirements-build.lock` with hashes,
wheels only. The managed updater does this itself. A manual installation
should do the same:

```sh
python -m pip install --only-binary=:all: --no-deps --require-hashes --upgrade -r requirements-build.lock
python -m pip install --only-binary=:all: --require-hashes -r requirements.lock
```

A 1.7 or later release without both locks is refused unless you pass
`--allow-unpinned`. The locks cover CPython 3.12 to 3.14 on Linux x86_64 and
aarch64 with glibc 2.39 or newer.

### Manual Gunicorn configuration

`gunicorn.conf.py` has new `worker_exit`, `post_worker_init`, `pre_request`,
`post_fork` and `child_exit` hooks that hand over running answers when a
worker is replaced. Copy them if you start Gunicorn with your own
configuration.

### Claude subscription connector removed

- Offer Anthropic models through **Admin → Models → External APIs**, preset
  **Anthropic** ([external providers](docs/external-providers.md)).
- The connector's models are hidden and refused by the API; chats keep their
  history.
- `BC_CLAUDE_*` settings are ignored with one warning at start.
- Clients that handled `provider_capacity_unavailable` should handle
  `provider_rate_limited` and `provider_overloaded`.

### External API providers

- Keys are encrypted the first time they are used after the update. If the
  instance secret changes, enter them again.
- Claude models get `temperature` and `top_p` only when **Sampling
  parameters** is ticked for the model.
- Agents on Claude think at every tool step and use more output tokens. Lower
  the agents' reasoning effort if cost matters more.

### Two-step verification

- **Admin → Site settings → Sign-in security → Require it for administrators**
  is off after the upgrade. Turn it on once your own account uses it.
- High-impact administrator actions ask for the password or a code unless the
  browser confirmed in the last ten minutes.
- For an account that lost its app and recovery codes:
  `sudo bananachat reset-two-step USERNAME` (see
  [two-step verification](docs/two-step-verification.md)).

### API keys

- Changing the password, or **Sign out all other devices**, revokes the
  account's API keys unless *Also revoke my API keys* is unticked. People who
  script against the API should untick it or create a new key afterwards.
- `bananachat reset-two-step` output gains `api_keys_revoked`.

### Session cookie

With secure cookies (the default behind the managed HTTPS proxy), the cookies
are named `__Host-bc_session` and `__Host-bc_session_device`, so other
subdomains cannot plant a session. Users who visit within a day of the update
from the browser they signed in with stay signed in; others sign in again.

During that day, someone who controls another subdomain and kept their own
old cookie can sign one other browser with exactly the same User-Agent into
their account. If people you do not trust control such subdomains, sign
everyone out at the update: stop the service, run
`sqlite3 <BC_DATABASE_PATH> 'DELETE FROM auth_sessions;'` as the service user
and start it again.

Do not put `__Host-` or `__Secure-` in `BC_SESSION_COOKIE_NAME`.

### Sign-in and sign-up limits

| Setting | Default |
| --- | --- |
| `BC_LOGIN_ATTEMPTS_PER_MINUTE` | `60` |
| `BC_LOGIN_FAILURES_PER_ADDRESS` | `20` in 15 minutes |
| `BC_LOGIN_FAILURES_PER_ACCOUNT` | `10` in 15 minutes |
| `BC_SIGNUPS_PER_HOUR` | `30` accounts created |
| `BC_SIGNUP_FAILURES_PER_HOUR` | `20` failed attempts |

Raise them for large groups behind one address. Behind a reverse proxy on the
same host, set `BC_PROXY_MODE=1`.

### Invitation codes

Invitations created under the old rules keep working but are marked **Weak**
in **Admin → Invitations**. Delete them and create new ones.

### Attachments and images

- `BC_CHAT_MAX_ACCOUNT_ATTACHMENT_MB` (default 1024) limits one account's
  attachments; administrators are exempt.
- This limit and `BC_CHAT_MAX_SESSION_ATTACHMENT_MB` count what is stored,
  including the text extracted from documents. Accounts with many PDFs may be
  over a limit after the update; new uploads are refused until files are
  removed or the limit is raised.
- New images are stored at most `BC_CHAT_MAX_IMAGE_DIMENSION` (2048) pixels on
  their longest side.

### Compute management token

New compute servers get a second token for model management. To add one to an
existing compute server:

1. Update the web server to 1.7 first.
2. On the compute server: `sudo bananachat compute rotate-token`.
3. On the web server: `sudo bananachat backend connect` with the new pairing
   code.

`BC_COMPUTE_MODEL_MANAGEMENT=0` on the compute server refuses model management
with any token. See [compute servers](docs/compute.md).

### Encrypted Git backups

Snapshots are authenticated. Snapshots uploaded by 1.6 need
`--allow-unauthenticated`. **Run `sudo bananachat backups run` right after
updating** so the series holds authenticated snapshots. Optionally use
`backups configure --append-only` with `backups prune --token-file FILE`
([backups](docs/backups.md)).

### Restores

A restore deploys a release the server already prepared, or builds the
revision from the server's own update source. On a new server it stops and
asks for `--trust-source REVISION`; scripts that migrate to a new server need
the flag. An installed server keeps its own update source, credentials and
allowed signers.

### Backup packages

Packages taken before an update or restore no longer contain model weights.
Plain `backup` and `migrate` packages still do.

### Volunteer workers

Workers that fail are paused for 1 minute, doubling up to 1 hour. Expect a
little more load on the local server while workers misbehave
([workers](docs/workers.md)).

### Going back to 1.6

`sudo bananachat rollback` restores the code and database of the package
taken before the update; changes made since are lost. Users with only the new
`__Host-` cookie sign in again.

With encrypted Git backups scheduled, 1.6 uploads unauthenticated snapshots
that 1.7 later refuses to restore. Run `sudo bananachat backups disable` before
going back and `backups enable` after updating again;
[backups](docs/backups.md) explains how to recover such a snapshot by hand.
