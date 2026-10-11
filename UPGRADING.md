# Upgrading from BananaChat 1.6 to 1.7

BananaChat 1.7 updates a 1.6 installation (and any development build made
after it) **in place**: same database, files, secret key and URLs. Accounts,
passwords, chats, attachments, personalities, quotas and API keys are kept,
and so are sign-ins (on HTTPS servers, for browsers used within a day of the
update: see [Session cookie](#session-cookie)). This page lists everything that needs a decision or an action, in
the order to read it before updating. The [changelog](CHANGELOG.md) lists
every change; [deployment](docs/deployment.md) explains the update itself.

## Before you update

1. **Keep the secret key with the data.** `BC_SECRET_KEY` (or the instance's
   `.secret_key`) now also encrypts external provider API keys and
   authenticator secrets. A backup without it cannot bring those back: provider
   keys must be entered again, and accounts with two-step verification must sign
   in with a recovery code. Managed backups include it.
2. **Read your `config/app.env`.** Delete any `BC_CLAUDE_*` setting (see
   [Claude connector removed](#claude-subscription-connector-removed)). Check
   the new sign-in and attachment limits below if your users sign in from one
   shared address or store a lot of attachments.
3. **Split deployments:** the web and compute servers can be updated in
   either order. Rotate the compute token, or pair a newly installed compute
   server, only once the web server runs 1.7 (see
   [Compute management token](#compute-management-token)).
4. **Free space.** The update to 1.7 is carried out by the lifecycle tool
   already installed, the 1.6 one: on a single or compute server its
   pre-update backup still compresses every model's weights while the service
   is stopped, and it does not check free space first. Make sure the disk has
   room for that copy. From 1.7 on, updates leave the weights in place and
   check for room before stopping anything (twice the backup package plus
   128 MiB when a rollback may follow); a refusal for space stops nothing and
   is not recorded as a failed revision.

## Updating

On a managed server:

```sh
sudo bananachat source check
sudo bananachat update
```

The 1.6 updater installs 1.7, backs everything up, starts it and checks
`/health` before letting users back in; if anything fails it restores the old
code and database together. On first start the database is upgraded in place
to schema version 25 by five migrations; only the first removes anything
(the connector's own tables and state):

| Migration | What it does |
| --- | --- |
| `v21_remove_claude_connector` | Retires the Claude subscription models, resets personalities that preferred them to the default model, drops the connector's account, lease and usage tables and its stored state |
| `v22_api_token_scopes` | `api_tokens.scope` (default `full`) and `expires_at` (empty): existing keys keep full access and never expire |
| `v23_external_provider_presets` | The preset, key header and extra headers of each external provider, inferred from its address; existing providers keep working unchanged. Gemini models enrolled with 1.6 (`models/<id>`) are called by the bare ID Gemini lists and keep their public ID, publication, limits and access |
| `v24_two_step` | Two-step verification (`user_two_step`), `auth_sessions.verified_at` and the administrator requirement (off) |
| `v25_run_owner_worker_backoff` | The account of each running answer (`active_streams.user_id`, `is_incognito`); usage records of running API and playground answers (`completion_leases`, new table); the volunteer-worker pause and failure attribution (`worker_nodes.strikes`, `cooldown_until`, `worker_jobs.requeues`, `deferred_by`, `requested_by`, `fault`); what each attachment stores (`chat_attachments.stored_bytes`, measured from existing rows); the administrator's sharing block (`chat_sessions.share_blocked_at`) |

After updating, reload open browser tabs.

### Servers installed before 1.7: follow release tags

New installations deploy the highest signed `vX.Y.Z` tag. A server installed
before 1.7 keeps following its branch until you switch it:

```sh
sudo bananachat source set --track tags
sudo bananachat source check   # shows what an update would deploy
```

Until the source has a release tag, a server that tracks tags reports that
there is nothing to update to.

### Automatic updates need signatures

`sudo bananachat updates enable` now refuses unless revisions must be signed:

```sh
sudo bananachat updates enable --require-signatures /root/allowed_signers --interval 60
```

With release tags, the signature must be on an annotated tag made for that
version (`git tag -s vX.Y.Z`); a server that follows a branch needs signed
commits. Higher tags that do not verify are skipped and reported
(`skipped_releases`); only the 20 highest are tried, so let only maintainers
create `v*` tags on the update source. `--allow-unsigned`
enables automatic updates without signatures anyway: then anyone who can push
to the update source can run code on your server as root, and `status` warns
about it. Automatic updates enabled before 1.7 keep running as they were, with
the same warning while they are unsigned. Clearing the signature requirement
while automatic updates are on (`source set --clear-signatures`) needs
`--allow-unsigned` too.

### Hash-pinned dependencies

From 1.7 on, each release installs `requirements.lock` and
`requirements-build.lock`: every package at the version and SHA-256 the
release was tested with, wheels only, so nothing is resolved anew on the
server and no build script runs there. The managed updater does this by
itself (the update to 1.7 itself is prepared by the 1.6 tool, which still
installs `requirements.txt`). A manual installation should do the same
instead of installing `requirements.txt`:

```sh
python -m pip install --only-binary=:all: --no-deps --require-hashes --upgrade -r requirements-build.lock
python -m pip install --only-binary=:all: --require-hashes -r requirements.lock
```

A release that declares version 1.7 or later and lacks either lock (or has
only one of them) is refused by `update`, `restore`, `rollback` and
`backups restore` before anything changes; `--allow-unpinned` installs it from
its ranges anyway.
Releases from before 1.7 still install their `requirements.txt` ranges, with a
warning and `dependencies_unpinned` in the result.

`requirements.txt` keeps the version ranges for development. New dependencies
in 1.7: `cryptography` (provider keys) and `qrcode` (two-step setup); `psutil`
is bounded below 8. The locks cover CPython 3.12 to 3.14 on Linux x86_64 and
aarch64 with glibc 2.39 or newer (Ubuntu 24.04, Debian 13); on another
platform preparing the next release fails, before anything is stopped.

### Manual installations with their own Gunicorn configuration

`gunicorn.conf.py` changed its hooks: `worker_exit` hands over the chat,
API and playground answers of a worker that is replaced or stopped (they are
finished or saved and charged instead of cut off); `post_worker_init` does
the same shortly before the master's `graceful_timeout` on a stop or reload,
even while open streams keep the worker busy; `pre_request` keeps a worker
from being replaced by `max_requests` before it has run an hour (with
`post_fork`, which records when it started); and `child_exit` frees at once
the inference queue places of a worker that exited, even one that crashed.
Copy all of them if you start Gunicorn with a configuration of your own; the
managed service uses the file in the release. A custom `--timeout` or
`GUNICORN_CMD_ARGS` is respected; when `timeout` minus `graceful_timeout`
leaves less than 15 seconds, running answers are stopped and charged at once
instead of being given time to finish.

## Changes that need attention

### Claude subscription connector removed

The Claude Code subscription connector and its account pool are gone. They
pooled consumer Pro/Max subscriptions to serve many users, which Anthropic's
terms do not allow.

- Offer Anthropic models through an external API provider with an Anthropic
  API key instead: **Admin → Models → External APIs**, preset **Anthropic**.
  See [external providers](docs/external-providers.md).
- The upgrade retires the models the connector served: they are hidden from
  model pickers and `/v1/models`, and the API refuses them. Existing chats keep
  their history and model names. Retired rows can be deleted from the catalogue.
- `BC_CLAUDE_EXTENSION`, `BC_CLAUDE_CODE_CONFIG` and every other `BC_CLAUDE_*`
  setting are ignored; BananaChat starts normally and logs one warning naming
  them until they are deleted from `config/app.env`.
- After the next managed install or update, the service no longer gets write
  access to Claude profile directories. Private Claude CLI profiles and
  manifests outside the installation are no longer used and can be removed.
- Clients that handled `provider_capacity_unavailable` should handle
  `provider_rate_limited` and `provider_overloaded` (both 503 with
  `Retry-After`); see [the API guide](docs/api.md).

### External API providers

- **Presets** fill in the base URL, API format, key header and suggested
  headers for Anthropic, OpenAI, OpenRouter, Google Gemini, Mistral, Groq,
  DeepSeek, xAI, Together, Fireworks, Azure OpenAI, LM Studio, vLLM, llama.cpp,
  LiteLLM and other OpenAI-compatible services. Existing providers get a preset
  inferred from their address and keep their settings.
- **Gemini models enrolled with 1.6** keep their public ID
  (`external:<n>:models/…`), publication, limits and access. They are now
  called by the bare model ID Gemini lists, and are shown under that ID in the
  provider's model list; automatic enrolment does not add a second copy. The
  upgrade recognises Gemini by its address; a provider reached through
  another host gets the same treatment when you choose the **Google Gemini**
  preset for it later.
- **Keys are encrypted at rest** with a key derived from the instance secret,
  each the first time it is used after the update (the background model
  refresh encrypts the rest within about 15 minutes). If the instance secret
  changes, the keys must be entered again.
- Behaviour changes: Claude models get `temperature` and `top_p` only when
  **Sampling parameters** is ticked for the model; a model is withdrawn only
  after two listings in a row omit it; **Max** effort is sent as `xhigh` to
  OpenAI-compatible APIs; interrupted answers are charged with the input count
  the provider reported.
- Agents on Claude with a reasoning effort other than off now think at every
  tool step, not only the first, so agent runs on Claude use more output
  tokens (and on newer models the replayed thinking counts as input). Lower
  the agents' reasoning effort if your provider bill matters more.

### Two-step verification and administrator confirmation

- Any account can turn on authenticator-app codes on **Account → Two-step
  verification**. Nobody has it until they turn it on. API keys do not ask for
  a code; turning it on offers to revoke the account's API keys (ticked by
  default).
- **Admin → Site settings → Sign-in security → Require it for
  administrators** is off after the upgrade. Turn it on once your own account
  uses it; administrators without it are then sent to set it up.
- High-impact administrator actions (the database export, resetting another
  account's password or two-step verification, role changes, creating
  administrator accounts or invitations, suspending, reinstating or deleting
  an administrator, registering a worker, adding, changing or removing any
  external provider (with or without a key), opening sign-up to everyone, and
  the requirement itself) ask for the password, or an authenticator code,
  unless that browser signed in or confirmed in the last ten minutes. Sessions
  from before the upgrade count as not recently confirmed.
- For an account that lost both its app and its recovery codes, including the
  only administrator: `sudo bananachat reset-two-step USERNAME` on the single
  or web server (without the managed installer:
  `python -m bananachat.manage reset-two-step USERNAME` as the service user,
  with the service's environment). See
  [two-step verification](docs/two-step-verification.md).

### API keys

- Keys can be **read-only** (they list models; anything that generates gets
  `403 insufficient_scope`) or have full access, and can expire after 7, 30,
  90 or 365 days. Keys that existed before the update keep full access and
  never expire.
- **Changing the password, or Sign out all other devices, now revokes the
  account's API keys** unless *Also revoke my API keys* is unticked (it is
  ticked by default). People who script against the API should untick it or
  create a new key afterwards. An administrator's password reset already
  revoked them.
- Turning on two-step verification, and an administrator's **Sign out
  everywhere** or two-step reset, also offer to revoke the keys (ticked by
  default). `bananachat reset-two-step` always revokes them, and its JSON
  output gains `api_keys_revoked`: scripts that compare the whole output must
  accept the new key.

### Session cookie

With secure cookies (`BC_SECURE_COOKIES=1`, the default when
`BC_PROXY_MODE=1`, as on every managed HTTPS server) the session cookie is
sent as `__Host-<BC_SESSION_COOKIE_NAME>` (default `__Host-bc_session`) and the
device cookie as `__Host-bc_session_device`, so other subdomains of the same
domain, such as hosted wikis, cannot plant a session. Users stay signed in
if they visit within a day of the update, from the browser they signed in
with. After that day, from another browser, or from a second browser of the
same make and version (each account moves one session per User-Agent), they
sign in again. A session that moves counts as not recently confirmed.

The server cannot tell its own old cookie from a copy planted by another
subdomain. During that first day, someone who controls another subdomain of
the chat's domain and kept their own cookie from before the update can still
sign one other browser into that account (one per account they hold): a
browser that has not used the chat since the update and sends exactly the
same User-Agent, such as an identical school laptop. If people you do not trust
control such subdomains, you can sign everyone out at the update instead:
right after updating, stop the service, run
`sqlite3 <BC_DATABASE_PATH> 'DELETE FROM auth_sessions;'` as the service user
and start it again. Everyone then signs in again; nothing else is lost.

Do not put `__Host-` or `__Secure-` in `BC_SESSION_COOKIE_NAME` (it is
stripped with a warning). Tools that read the cookie by name must use the new
name. Going back to 1.6 signs out users who only have the new cookie.

### Sign-in and sign-up limits

The fixed limits (30 sign-ins a minute, 10 sign-ups an hour per address) are
now settings with more generous defaults, and a browser that signed in to an
account before has its own allowance, so failures from strangers on a shared
network (school or office NAT, mobile CGNAT, VPN) cannot lock it out:

| Setting | Default |
| --- | --- |
| `BC_LOGIN_ATTEMPTS_PER_MINUTE` | `60` |
| `BC_LOGIN_FAILURES_PER_ADDRESS` | `20` in 15 minutes |
| `BC_LOGIN_FAILURES_PER_ACCOUNT` | `10` in 15 minutes |
| `BC_SIGNUPS_PER_HOUR` | `30` accounts created |
| `BC_SIGNUP_FAILURES_PER_HOUR` | `20` failed attempts |

Raise them for very large groups behind one address. Behind a reverse proxy on
the same host, `BC_PROXY_MODE=1` is required, or everyone shares the proxy's
address; a startup warning now says so.

### Invitation codes

Custom codes need 12 to 32 characters with at least 12 letters or digits, at
least 6 of them different. Administrator invitations must expire within 30
days and have a limited number of uses (1 by default). Invitations created
under the old rules keep working but are marked **Weak** in
**Admin → Invitations**: delete them and create new ones.

### Attachments and images

- `BC_CHAT_MAX_ACCOUNT_ATTACHMENT_MB` (default 1024) limits the attachments
  one account may store; deleted chats count until they are erased, and
  administrators are exempt. Accounts already over it cannot upload until
  they delete chats or the limit is raised (uploads get `413 storage_limit`).
- This limit and `BC_CHAT_MAX_SESSION_ATTACHMENT_MB` (default 40, per chat)
  now count what is stored: the text extracted from a document and the stored
  image, not the uploaded file. The upgrade measures existing attachments. A
  chat or account holding many PDFs may therefore be over a limit right after
  the update: new uploads are refused until files are removed or the limit is
  raised. Messages without files are never refused.
- New image attachments are stored at most `BC_CHAT_MAX_IMAGE_DIMENSION`
  (2048) pixels on their longest side. Images already stored are unchanged.
- API requests with an image that cannot be decoded get `400 invalid_image`
  instead of being forwarded to the model.

### Compute management token

New compute servers get a second token for model management
(`data/.compute-management-token`, `BC_COMPUTE_MANAGEMENT_TOKEN_FILE`): the
main token alone can then no longer download, create, copy or delete models.
The pairing code carries it, and the web server stores it as
`BC_OLLAMA_MANAGEMENT_API_KEY`.

Existing compute servers keep one token for everything after the update.
To separate the two:

1. Update the web server to 1.7 first. A 1.6 web server ignores the
   management token, and then cannot download or delete models (the gateway
   answers 403); inference is unaffected.
2. On the compute server: `sudo bananachat compute rotate-token`. It replaces
   the token, adds the management token and prints a new pairing code.
3. On the web server: `sudo bananachat backend connect` with that code.

Instead, `BC_COMPUTE_MODEL_MANAGEMENT=0` on the compute server refuses model
management with any token; pull models there with `ollama pull`. A compute
server installed with 1.7 has a management token from the start, so pair it
only with a web server that runs 1.7 (a 1.6 one gets 403 on model downloads
and deletions). Connecting
with a pairing code that has no management token (from an older compute
server) removes `BC_OLLAMA_MANAGEMENT_API_KEY` from the web server's
`app.env`. See [compute servers](docs/compute.md).

### Encrypted Git backups are authenticated

Each snapshot now carries an HMAC keyed from the recovery key, checked before
anything is decrypted, so someone who can push to the backup repository can no
longer forge a snapshot whose restore runs their code. Snapshots uploaded by
1.6 have no tag: `backups verify`, `download` and `restore` refuse them unless
you add `--allow-unauthenticated`, which prints a warning. **Run
`sudo bananachat backups run` right after updating** so the series holds
authenticated snapshots.

Optional: `backups configure --append-only` makes uploads never delete
snapshots, so the stored token needs no right to delete branches and the
branches can be protected on the forge; apply retention with
`backups prune --token-file FILE`. See [backups](docs/backups.md).

Retention (`backups prune` and the pruning after each upload) counts only
snapshots that the series' recovery key authenticates and never deletes while
fewer than `--keep` of them would remain; branches dated in the future or not
named by a date are neither counted nor deleted. `--allow-unauthenticated`
refuses a snapshot that cannot be an old one: dated in the future, or at or
after the series' first authenticated snapshot.

### Restores check the source they deploy

A restore no longer deploys the source code bundled in a package unchecked.
It reuses a release the server already prepared, or builds the revision from
the server's own update source (signature-checked when signatures are
required). When neither has the revision, typically on a new server, it stops
before changing anything, shows the revision and asks for
`--trust-source REVISION`. This applies to `install --restore`,
`backups restore`, `restore` and `rollback`; scripts that migrate to a new
server need the flag. A server that requires signed revisions never deploys
an unverified revision from a package; a release whose tag is signed
restores like one whose commit is signed. To restore an older unsigned package
there, the refusal gives the steps: clear the requirement
(`source set --clear-signatures`), restore with `--trust-source REVISION`,
then require signatures again.

A restore onto an installed server keeps that server's update source
settings, repository credentials and allowed signers, which decided what code
may run; the result's `source_warning` says when the package's differ. Only a
new server (no update source yet) takes the package's; change them afterwards
with `source set` if you mean to.

### Backup packages and model weights

`auto-`, `before-update-`, `before-restore-` and `before-legacy-import-`
packages no longer contain model weights: the weights stay in place and
rollbacks keep them. Restoring one of them on another server asks the
administrator to approve downloading the models again, as weight-free backups
already did. Plain `backup` and `migrate` packages still include the weights.
An upload named like a database (`notes.db`) no longer makes packages
unrestorable.

### Volunteer workers

With `BC_WORKERS_ENABLED`, a worker that times out, reports a failure or
breaks the protocol is paused (1 minute, doubling to 1 hour, cleared by a
completed job); the administrator page shows "paused after failures". A
failure the request caused (input the model cannot read, an Ollama 4xx
refusal, memory the request needs) does not pause the worker; that account is
then not sent to that worker for 30 minutes. After a failure held against a
worker, the account's requests are answered by the server for 30 minutes. A
worker on which the jobs of three accounts failed for their requests' sake
within 30 minutes, with no answered job in between, is paused all the same.
Expect a little more load on the local server right after workers misbehave.
See [workers](docs/workers.md).

### Shared chats

Public share links answer "not found" while the owner's account is suspended
and stop working when it is deleted. **Stop sharing** in the chat audit
(Admin → Chats) takes a link offline without deleting the chat, and the owner
cannot share that chat again (`403 share_blocked`) until an administrator
chooses **Allow sharing**. A chat that is not shared can be blocked the same
way (**Block sharing**).

### Personal data export

The export (`format_version` still 2) gains a `two_step` object (on, since
when, recovery codes left) and, for each API key, `scope`, `expires_at` and
`expired`. The changes are additive; secrets are never included.

## Going back

`sudo bananachat rollback` restores the code and the database of the package
taken before the update; changes made since are lost, and 1.6 refuses a
database at schema version 25. Signed-in users who only have the new
`__Host-` cookie sign in again.
