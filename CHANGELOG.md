# Changelog

## 1.7.2 — 2026-10-20

### Claude and Codex subscription pools

- Pools of Claude and Codex (ChatGPT) subscription accounts, answered by the
  official command-line tools under private server profiles signed in to
  their own subscriptions. Both are off until an operator sets
  `BC_CLAUDE_EXTENSION` or `BC_CODEX_EXTENSION`. A Claude model is only ever
  answered by a Claude account, and a Codex model by a Codex account.
  Administration → Models → Claude and Codex: accounts, profiles, caps, usage
  and model enrolment ([Claude](docs/claude-code.md), [Codex](docs/codex-cli.md)).
- Each account can stop at a share of the provider's own five-hour and weekly
  allowance (for example 70 % and 90 %) and, per model family such as Fable,
  at a share of that family's allowance. Local token budgets remain. Without
  provider usage an account with a percentage cap does not serve, and
  what is left in a pool shrinks that provider's model limits.
- Accounts are leased in the database, so every Gunicorn worker routes safely.
  A failed account rests and another is tried before any text is produced; a
  request that finds every account busy waits up to 15 seconds. A request the
  adapter rejects (too long, not text) rests no account. A Claude account that
  starts using paid extra usage is rested instead.
- Codex models come from the tool's own catalogue. The Codex transport switches
  off every tool its configuration can remove, stops an answer at any tool
  activity and answers in one piece rather than streaming.
- `BC_SUBSCRIPTION_PROBE_INTERVAL` sets how often usage is read again and rest
  periods end. A provider without an adapter or an active account stops
  offering its models until one is configured.
- `provider_capacity_unavailable` (503, `Retry-After: 30`) is back for an
  exhausted pool ([API](docs/api.md)).
- The database is upgraded to schema version 26 (`v26_subscription_pool`: the
  pool tables, and `ai_models.backend` also allows `codex`). The 1.6 pool is
  not carried over; see [UPGRADING.md](UPGRADING.md).

### Proxy behind Cloudflare

- `bananachat proxy` writes a site that also answers on port 80, so certificates
  can be issued and Cloudflare's Flexible SSL does not redirect in a loop. It
  takes the visitor's address from Cloudflare, keeps idle connections for 16
  minutes and, when the domain is proxied, skips the TLS-ALPN challenge.
  `--cloudflare` and `--no-cloudflare` override the DNS lookup, and `--email`
  sets the certificate contact ([deployment](docs/deployment.md#behind-cloudflare)).

## 1.7.1 — 2026-10-18

### Security

- Compute gateway: clients without a valid token can no longer take every
  connection slot. Only requests with a valid token count towards
  `BC_COMPUTE_MAX_CONNECTIONS`, and an address that sent too many wrong tokens
  gets 429 before its upload is read.
- Servers that require signed updates accept only SSH signatures from the
  allowed signers file, also for a tag or commit whose message quotes one.
- Maintenance commands run as root (updates, backups, restores,
  `compute rotate-token`) do not follow links planted in the data folder when
  they set a file's mode or owner.
- Minimum dependency versions: Pillow 13.0.0 (image decoder fixes), pypdf
  6.21.1 (limits on parsing crafted PDFs) and filelock 3.20.3.

### Fixes

- API and playground: chat completion bodies have room for their images (4 MB
  plus up to `BC_CHAT_MAX_REQUEST_MB` of base64 images), so images up to
  `BC_CHAT_MAX_IMAGE_MB` are accepted as documented.
- Request bodies sent without `Content-Length` that exceed the limit get 413
  instead of an invalid JSON error.
- An API provider at `http://localhost` works when localhost resolves to `::1`
  and private endpoints are allowed.
- Attachments with long names in non-Latin text or with typographic
  punctuation display, download and keep their extension; names over 160
  characters are accepted.
- Agent tasks on OpenAI-compatible and Anthropic models can be continued after
  a run stopped partway through a step.
- Agent model calls that a provider bills without producing output are
  charged, as in chat and the API.
- Chat downloads and account exports read attachments in batches.
- Encrypted Git backups:
  - `backups run` checks the staging space it needs before uploading, and
    uploads and downloads need less disk space at their peak.
  - The next backup command removes working files that an interrupted one
    left under `config/remote-backup`.
  - `backups restore` holds the backup lock until it is done.
  - A branch with a name that is not valid UTF-8, or that only ends like the
    series name, no longer breaks listing, uploads, restores or pruning.
  - `backups configure` keeps the stored recovery key when `--key-file` is
    left out, and replaces it with a different key only with `--replace-key`.
  - Restoring a snapshot uploaded by 1.6 after a rollback explains why it is
    refused and how to recover it ([backups](docs/backups.md)).
- Worker: while you use the computer, all of Ollama's threads and model runner
  processes run at lower priority.
- Worker: the systemd unit loads its settings file and sets
  `BC_WORKER_ENV_FILE`.

### Interface

- German interface, chosen automatically for browsers that prefer German.
- Menus and pop-up forms stay on screen on phones and tablets, with larger
  text and inside tables.
- Long paths in the worker setup steps wrap instead of scrolling the page
  sideways.
- Rate-limit, "session ended" and "administrator access required" errors, the
  API playground's errors and the cloud sessions' "sending paused" notice
  follow the interface language.
- Texts with a number use the singular for 1 ("1 hour", "1 step").
- Italian: interrupted cloud sessions read "Da riprendere", the stop button
  "Interrompi", and file sizes and durations use a decimal comma.
- The banana in the top bar is BananaWiki's mascot; its sunglasses are kept in
  the browser.

### Development

- `./dev.sh` upgrades an existing `.venv` to the newest versions in range.
- `scripts/check_upgrade.py` tests upgrades from 1.7.0 and 1.6 by default;
  `--from` can be repeated.
- Playwright 1.64.0 for the browser checks; CI actions on their Node 24
  releases, pinned by commit; Markdown renderer tests on Node.js 22.23.3.

### Upgrading

- Linux workers: run `python bananachat_worker.py install` again (with
  `--system` if you used it) and restart the service. See
  [UPGRADING.md](UPGRADING.md).

## 1.7.0 — 2026-10-11

### Upgrading

- Run `sudo bananachat update` after reading [UPGRADING.md](UPGRADING.md).
  The database is upgraded in place to schema version 25.
- The Claude subscription connector is removed; offer Anthropic models through
  the Anthropic API provider.
- Changing a password revokes the account's API keys unless the box is
  unticked.
- Attachment limits count the text stored from documents, so some accounts may
  be over them after the update.
- Restoring onto a new server needs `--trust-source`; snapshots made by 1.6
  need `--allow-unauthenticated`.
- Automatic updates need signatures. Servers installed before 1.7 keep
  following their branch until `source set --track tags`.

### Removed

- The Claude subscription connector and its account pool. They pooled consumer
  subscriptions, which Anthropic's terms do not allow. The upgrade retires the
  models it served (chats keep their history) and drops its tables.
- `BC_CLAUDE_*` settings are ignored with one warning at start.
- The `provider_capacity_unavailable` API error; busy providers answer
  `provider_rate_limited` or `provider_overloaded`.

### External API providers

- OpenAI-compatible and native Anthropic providers, with private keys,
  connection checks, model selection and optional automatic enrolment
  ([setup](docs/external-providers.md)).
- Presets for Anthropic, OpenAI, OpenRouter, Google Gemini, Mistral, Groq,
  DeepSeek, xAI, Together, Fireworks, Azure OpenAI, LM Studio, vLLM, llama.cpp,
  LiteLLM and other OpenAI-compatible services.
- **Test connection** checks the key and the model list and can stream a short
  answer; failures say what went wrong.
- The provider page lists every model with context window, prices and
  features. A model is withdrawn only after two listings in a row omit it.
- Reasoning output from Claude, OpenRouter, DeepSeek, vLLM, Mistral and xAI is
  shown; **Max** effort is sent as `xhigh` to OpenAI-compatible APIs.
- Agents on Claude models keep extended thinking through tool rounds.
- Limits use the provider's token counts, also for interrupted answers.
- Clear error codes: `provider_rate_limited`, `provider_overloaded`,
  `provider_unavailable`, `provider_rejected_request`,
  `provider_misconfigured` and `model_not_found`.
- Provider keys are encrypted at rest with a key derived from the instance
  secret. New dependency: `cryptography`.
- Connections pin verified DNS results, require HTTPS for public endpoints,
  block metadata addresses and never forward credentials through redirects.
- Gemini models enrolled with 1.6 keep their public ID, limits and access.
- Hosted models stay available while the local model server is down.

### Sign-in and accounts

- Optional two-step verification with an authenticator app and recovery codes
  ([details](docs/two-step-verification.md)). New dependency: `qrcode`.
- Administrators can require two-step verification for all administrators
  and reset it for an account that lost its codes.
- `sudo bananachat reset-two-step USERNAME` turns it off from the server.
- High-impact administrator actions ask for the password or a code unless the
  browser confirmed in the last ten minutes.
- Sign-in limits no longer lock out people who share an address: a browser
  that signed in before has its own allowance. New settings
  `BC_LOGIN_ATTEMPTS_PER_MINUTE`, `BC_LOGIN_FAILURES_PER_ADDRESS`,
  `BC_LOGIN_FAILURES_PER_ACCOUNT`, `BC_SIGNUPS_PER_HOUR` and
  `BC_SIGNUP_FAILURES_PER_HOUR`.
- API keys can be read-only and can expire after 7, 30, 90 or 365 days.
- Changing the password or signing out other devices also revokes API keys
  unless unticked.
- With secure cookies, the session and device cookies use the `__Host-`
  prefix. Existing sign-ins move to it within one day.
- Custom invitation codes need at least 12 letters or digits; administrator
  invitations must expire within 30 days. Older ones are marked **Weak**.
- The personal data export includes API key scope and expiry and the two-step
  status.
- Sign-in, password changes and deletions recheck credentials inside the
  database transaction.

### Chats and usage

- One five-hour token allowance replaces the separate slow allowance. The
  upgrade combines the old regular and slow amounts when slow tokens were
  enabled.
- Admin → Limits has separate switches for charging local and cloud chats.
  Local chats are not charged by default.
- Internal token and rate exemptions can be set separately per account and per
  model.
- New reasoning level **Extra** (`xhigh`) between High and Max.
- Answers are charged when the chat or account is deleted while they generate,
  and when a Gunicorn worker is replaced or stopped.
- Interrupted answers are charged more accurately (images, CJK text, silent
  reasoning).
- New API error codes `503 interrupted` and `503 busy`.
- `BC_CHAT_MAX_ACCOUNT_ATTACHMENT_MB` (1024) limits each account's
  attachments; attachment limits count what is stored.
- `BC_CHAT_MAX_IMAGE_DIMENSION` (2048) limits the stored size of image
  attachments.
- Requests a model cannot read no longer count towards marking it failing;
  undecodable API images get `400 invalid_image`.
- Shared links stop working while the owner is suspended or deleted.
  Administrators can stop or block sharing of a chat.
- A link whose text names a different site than its target shows the real
  host.
- Interrupted answers have a **Try again** button.

### Workers and compute servers

- Volunteer workers that time out, fail or break the protocol are paused for
  1 minute, doubling up to 1 hour.
- Failures caused by a request do not pause the worker, so one account cannot
  pause every worker.
- Model management on the compute gateway can need its own token;
  `compute rotate-token` adds it to existing servers.
- After 20 wrong tokens in a minute from one address, the gateway answers 429.
- `BC_CHECKPOINT_AGENT_INSECURE_NETWORKS` and
  `BC_AGENTS_RUNNER_INSECURE_NETWORKS` allow plain HTTP on listed private
  networks. The `*_ALLOW_INSECURE_TAILSCALE` settings are deprecated aliases.
- Both clients warn once per address when the token is sent without TLS.

### Agents and cloud sessions

- Cloud sessions can be opened to administrators only, everyone signed in, or
  the existing access lists. New installations keep them off.
- Permissions, model access and budgets are checked again after queue waits
  and before each step.
- Repository imports respect the per-user limit under concurrent starts.
- Sandboxes start on Podman 4, and the post-start check covers Podman too.
- A command that cannot be stopped ends its sandbox.

### Operations, backups and releases

- Managed updates follow release tags; new installations deploy the highest
  `vX.Y.Z` tag.
- `updates enable` requires `--require-signatures` or an explicit
  `--allow-unsigned`. With signatures required, only an annotated tag
  SSH-signed for its own name is deployed.
- Releases install from hash-pinned locks (`requirements.lock`,
  `requirements-build.lock`), wheels only.
- Encrypted Git backup snapshots are authenticated before anything is
  decrypted.
- Restores no longer deploy a package's bundled source unchecked; a new server
  needs `--trust-source REVISION`.
- `backups configure --append-only` and `backups prune`.
- Retention counts only authenticated snapshots and never goes below `--keep`.
- Packages taken before an update or restore leave model weights in place.
  Updates check free space before stopping anything.
- Static assets are versioned, so a normal reload after an update loads the
  current interface.
- Administrators can show or hide model-server offline notices; users can hide
  them for 24 hours.

### Interface

- A more compact layout across the application, with one page frame, grouped
  settings and a header that folds into a menu when space runs out.
- Form errors appear inside the form and are announced to screen readers.
- Tables on phones show each row as a card.
- Better contrast, larger touch targets and visible switch states.
- English text uses British spelling; URLs and setting names are unchanged.
- New favicon, logo and home-screen icon.

### Development

- CI runs Ruff's security rules, a dependency audit and the sandbox tests on
  Podman.
- The Markdown renderer tests fail instead of skipping when Node.js is missing
  in CI.

## 1.6.0 — 2026-09-30

A rewrite of the application for its open-source release. It updates existing
installations in place.

### Upgrading

- Run `sudo bananachat update`. The upgrade keeps accounts, chats, API tokens,
  quotas, settings and sign-ins.
- A site still named "BananaAI" is renamed to BananaChat.
- Old sign-in cookies are converted to server-side sessions.
- Old requests-per-minute settings, daily credits and quotas become the new
  limit policies (1 credit = 1,000 tokens).
- `BC_INFERENCE_OUTAGE_MODE=fallback` needs `BC_INFERENCE_FALLBACK_URL`.

### Two servers by default

- The default layout is a compute server with Ollama plus a web server.
- `install --mode compute` prints a pairing code; `install --pair` on the web
  server tests and stores the connection.
- A web server never runs Ollama itself, and gets an outage banner when the
  compute server is unreachable.
- `--ollama-url` uses an Ollama that already runs on the machine.
- After a restore, the web server offers to download missing models.

### Limits

- Token limits per five hours and, optionally, per week, for chat, the API and
  agents.
- Request-rate rules per second, minute, hour or day, with OpenAI-style
  `x-ratelimit-*` headers.
- Per-model weights, rate rules and token limits.
- Reasoning effort levels unlocked by administrators, by request or
  automatically.
- Tiers, dynamic adjustment to demand, grants and per-account custom limits.
- Users can ask for more from their account page; the community can back a
  request with part of its own quota.
- When a model's quota is used up, the chat falls back to a model the person
  can still use.

### Models

- Model capabilities, context length and reasoning levels are detected.
- New models wait for review or are enabled automatically with limits.
- Missing or failing models are hidden and come back when they work again.
- Downloads survive restarts, retry with back-off and are verified by digest.
- Claude subscription pool (removed in 1.7.0).

### Personalities

- Avatar, accent colour, description, greeting, conversation starters,
  preferred model and response style.
- Templates, share links, JSON import and export, and featured personalities.

### Agents (off by default)

- Coding tasks run by a tool-capable model in an isolated sandbox on the
  compute server ([details](docs/agents.md)).
- Optional sub-agents and tasks started from a public Git repository, with the
  result as a patch.
- Isolation: the sandbox runner (`compute/sandbox_runner.py`) gives each
  sandbox no network, a read-only root, a non-root user, dropped capabilities
  and resource limits.
- The web server never runs agent code; steps, tokens and time per run are
  hard limits.

### Service status

- Maintenance and compute outages pause sending only; users can still sign in
  and read their chats.
- New public `GET /status` endpoint for monitors.
- A "back in a moment" page during updates.

### Security and privacy

- Server-side sessions that users can review and end.
- Content-Security-Policy without inline scripts or styles.
- Sign-in throttling per address and per account.
- No-history chats are never sent to volunteer workers.
- Deleted chats and metrics are erased after a retention period.
- Request bodies are limited per endpoint.
- PDF text is extracted in a separate process with time and memory limits.

### Chat and API

- Rebuilt interface with search, model picker, reasoning display, attachments,
  voice input, read-aloud and exports.
- Answers keep generating when the browser disconnects.
- Health checks and pages stay fast under load; requests over capacity get 503.
- OpenAI-compatible errors, usage in streams, image content and sampling
  options ([API guide](docs/api.md)).
- The token page moved to `/developer`.

### Administration

- Overview with service status, audit log, error log, sessions per user,
  metrics with CSV export and a database export.
- Model names edited by an administrator survive the model sync.

### Settings

- New optional settings: `BC_SESSION_DAYS`, `BC_DEFAULT_LANGUAGE`,
  `BC_QUEUE_TIMEOUT`, `BC_FIRST_TOKEN_TIMEOUT`, `BC_MAX_NUM_CTX`,
  `BC_WORKER_CLAIM_TIMEOUT`, `BC_DELETED_CHAT_RETENTION_DAYS`,
  `BC_METRICS_RETENTION_DAYS`, `BC_WORKERS`, `BC_INFERENCE_LOCAL`,
  `BC_INFERENCE_FALLBACK_API_KEY`, `BC_AGENTS_RUNNER_URL`,
  `BC_AGENTS_RUNNER_TOKEN_FILE`, `BC_AGENTS_MAX_UPLOAD_MB`,
  `BC_AGENTS_GIT_PROXY` and `BC_SANDBOX_*` ([configuration](docs/configuration.md)).
- Invalid values fall back to their defaults with a warning.

### Removed

- The `/admin-access` sign-in page.
- Flask-WTF and unused helper modules.
- The old per-user quota form.
