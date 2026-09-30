# Changelog

## 1.6.0

This release is a complete rewrite of the application for its open-source
release. It updates existing installations in place.

### Updating

Run `sudo bananachat update` (or let automatic updates pick it up). The
previous release's updater backs up the installation, starts the new version
and checks its health before letting users in; if anything fails it restores
the old code and database together.

On first start the database is upgraded in place (schema version 12). The
upgrade only adds: accounts, chats, attachments, API tokens, quotas, access
policies, models, settings and sign-ins all carry over, and no configuration
change is required.

- **The former product name is gone.** Sites installed while the project was
  called BananaAI still had "BananaAI" stored as their site name, which every
  page title and the navigation showed. The upgrade renames exactly that value
  (any spelling) to BananaChat; a custom site name is kept.
- Existing sign-in cookies keep working once and are converted to revocable
  server-side sessions.
- Split deployments: update the web and compute servers in either order.
- Deployed worker daemons keep working; updating them is recommended.
- The previous release's requests-per-minute settings, daily credits, chat limits and custom API
  quotas become the new limit policies automatically (see Limits).
- `BC_INFERENCE_OUTAGE_MODE=fallback` now needs an explicit
  `BC_INFERENCE_FALLBACK_URL`; without one the server logs a warning and
  behaves as `shutdown` (it used to fall back to an Ollama on the web server).

### Two servers by default

The documented and suggested layout is now a compute server (the cluster with
Ollama) plus a web server (the VPS); one machine running both stays available.

- `install --mode compute` prints a **pairing code**; `install --domain … --pair`
  on the web server (or `bananachat backend connect` later) tests the
  connection before changing anything and stores it (the address in the code
  is shown for confirmation first; `--yes` without a terminal). `backend status` and
  `backend test` check it at any time and `compute rotate-token` replaces the
  gateway token in one step. `--backend-url`/`--backend-token-file` still work.
- **No more Ollama on the VPS.** A web server never treats itself as the Ollama
  host: a token or a non-loopback address means a remote compute server (SSH
  tunnels included), so the VPS is no longer checked for free memory, GPU or a
  model folder, and outage detection works. `BC_INFERENCE_LOCAL` overrides the
  detection. The fallback server has no default and gets its own
  `BC_INFERENCE_FALLBACK_API_KEY`, never the compute token.
- **Use an Ollama that already runs** on a compute or single server
  (`--ollama-url http://127.0.0.1:11434`): BananaChat then never starts, stops
  or reconfigures it. Edits to a managed Ollama's `OLLAMA_HOST`/`OLLAMA_MODELS`
  survive updates and restores.

### Restoring models

After a restore the web server checks by itself which models from the backup
are missing on the compute server. If none are, nothing is asked. Otherwise one
card on the Overview offers **Download them**, **Choose…** or **Dismiss**, with
the download size when known; the same card appears when a rebuilt compute
server lacks published models. Nothing stays pending forever. The compute-side
`models` command is only needed for compute servers without a web server.

### Limits

The daily-quota system is now a full limit system counted in **tokens**
(prompt plus answer; amounts can be typed as `50k` or `1.5M`). Chat, the API
and agents each have an administrator-editable policy:

- **Request rates**: rules such as "60 per minute and 1,000 per day" (per
  second, minute, hour or day; every rule must pass). One account's API keys,
  playground, chat and agents share them. They never rise by themselves;
  users can ask for more. API responses carry OpenAI-style
  `x-ratelimit-*-requests` and `x-ratelimit-*-tokens` headers, and refusals a
  `Retry-After`.
- **Tokens per 5 hours** (regular, then an optional slow allowance): the
  window opens with an account's first request and refills 5 hours later.
  It can rise automatically through **tier levels** (promotion by account
  age, activity and a clean record) or by an approved request.
- **Tokens per week**, off by default: a rolling week that starts with the
  first request after the previous one ended.

**Per-model limits**, globally or per account: models have a weight (a heavy
model can count ×3), their own rate rules and token limits, and can be locked
for one account; light, standard and heavy presets set them up quickly, and a
model can be kept out of the shared service limits. When a service's tokens
are used up, `auto` moves to a model that does not count toward them.

**Reasoning effort tiers** for reasoning models (off, low, medium, high, max):
everyone gets up to medium; higher levels are unlocked by an administrator, by
a request from the account page or automatically after sustained use, and
unlocking a level unlocks every level below it. The chat composer shows locked
levels with a link to request them; the API answers
`403 reasoning_effort_locked`. Effort gating can be switched off, and levels
locked or unlocked, for everyone or one account.

Limits can also adapt **dynamically** to current demand (queue load and how
many people are online), to each person's usage pattern and, for heavy models,
more strongly (0.5×–2×, explained on the account page; approved custom limits
are never lowered). Administrators can create **grants** (unlimited, a
multiplier or extra tokens, for one person or everyone, for one service or one
model, from one hour to forever — for example unlimited use during a coding
jam; an unlimited grant for every service also lifts each model's own
limits), reset usage for one account or everyone without touching the history,
set custom limits, slow accounts down or speed them up, and assign or lock
tiers. Every action is audited. Users see "Your limits" on their account page
and can ask for more tokens per 5 hours or per week, a higher request rate, a
temporary increase or a higher reasoning effort. Amounts raised by automatic
approval are minimums, so later tier promotions still apply; amounts an
administrator sets are exact.

The upgrade converts the previous release's settings: 1 credit becomes 1,000
tokens and the daily amount becomes the same amount per 5 hours, which is
more generous than before, so administrators may want to lower it.
`BC_IMAGE_TOKENS_PER_GENERATION` replaces `BC_IMAGE_CREDITS_PER_GENERATION`
(still read as its default).

### Models

- **Detection**: each new or updated model's capabilities, context length,
  family, size, quantisation and reasoning levels are read; a failed or empty
  listing never marks models missing, and one broken model no longer stops the
  sync (the last sync and its errors show in Admin → Models).
- **Enrollment**: new models either wait for review (the default) or are
  enabled automatically with limits for their size and flagged for review.
  Unreviewed models are never picked automatically (`auto`, fallbacks,
  agents); administrators can still try them by name. An ignore list (names or
  patterns such as `*-embed*`) keeps unwanted models out; an ignored model is
  published only after it is restored.
- **Retirement**: models missing for several syncs are hidden and come back
  unchanged, and leave the catalog after 30 days (chats keep their names);
  models that keep failing are hidden until a background test prompt succeeds
  (timeouts while the server is overloaded, and models removed on the model
  server, don't count as failures); fallbacks that stopped working while a
  request waited are skipped;
  administrators can deprecate a model with a replacement and a retirement
  date.
- **Downloads**: one job per model that survives restarts, retries network
  errors and stalls with back-off, checks disk space and is verified by digest;
  pause, reorder and cancel (with or without the partial files); several models
  can be queued at once, and queued downloads wait out a compute-server outage
  instead of failing. Deleting from the model server waits for running
  requests and moves personalities to the replacement; a chat whose model is
  removed mid-answer keeps saving, and API requests for a model that has just
  gone get `404 model_not_found`.

### Personalities

Personalities now have an emoji avatar, an accent colour, a description, an
optional greeting and up to four conversation starters, a preferred model and a
response style (length; precise, balanced or creative). Choose a default
personality for new chats, start from templates, preview changes live and try
a personality in a fresh chat. Share one through a revocable link or move it as
a JSON file. Administrators can publish featured personalities that everyone
can duplicate. Instructions still sit below each model's system prompt.

### Agents and agent swarms (optional, off by default)

Administrators can enable **Agents**: a person describes a coding task
(optionally with files or an archive) and a tool-capable model works on it in
an isolated sandbox on the compute server with bash, file, search and finish
tools, reporting back with a summary. **Swarms** (also off by default) let the
main agent delegate to a bounded number of sub-agents. The task page shows a
live timeline of every tool call and its output, sub-agent lanes, Stop,
follow-up messages and a workspace browser with downloads.

When an administrator allows it (off by default), a task can **start from a
public Git repository** on GitHub, GitLab, Codeberg or an allowed self-hosted
GitLab/Gitea server, at a branch, tag or commit, and the result can be
downloaded as a **patch** (`git diff --binary` against the imported commit,
computed without running anything the agent configured). The web server fetches
only source archives, over verified HTTPS, from allowlisted hosts whose
addresses must be public, with size and time limits; sandboxes still have no
network. `compute/sandbox-image` is a ready-made agent image with Git, a C
toolchain, Node.js and ripgrep.

Safety comes first:

- A new **sandbox runner** (`compute/sandbox_runner.py`, standard library only)
  on the compute server is the only component that talks to Podman or Docker.
  Every sandbox has no network, a read-only root, a size-capped in-memory
  workspace, a non-root user with all capabilities dropped, no-new-privileges,
  default seccomp and memory, CPU, process and file limits; each container is
  inspected to confirm this. The runner refuses a rootful engine unless
  explicitly allowed, never pulls images, kills timed-out commands with
  everything they started, caps output, confines file access to the workspace
  (symlinks included), validates uploads before extracting them inside the
  container and removes idle or old sandboxes.
- The web server never runs agent code. Tool calls are strictly validated;
  steps, tokens and minutes per run, concurrent tasks and task starts are hard
  limits; every model call counts against the new *agent* token limits and each
  step takes a request from the model's own rate (waiting when it is used
  up), at the lowest queue priority, and never goes to worker PCs.
- Users need the new *Agents* access capability (allowlist-only by default).
  Tasks can be stopped from any server process, survive restarts safely
  (marked interrupted), pause during maintenance and are deleted after a
  retention period. Admin → Agents shows the runner's health with warnings
  (network on, rootful engine), limits, model tool support and every task with
  its full log, plus a kill switch; turning Agents off also stops running
  tasks at their next step. See [docs/agents.md](docs/agents.md).

### Service status and downtime

- Maintenance mode and an unreachable AI (compute) server no longer take the
  site down or answer every page with an error. Users can still sign in and
  read their chats; a clear banner explains the situation and updates itself;
  only sending new messages is paused (HTTP 503 with a helpful message).
  Administrators can still use everything during maintenance.
- New public `GET /status` endpoint for monitors: always HTTP 200 with
  `ok`, `degraded`, `maintenance`, `outage` or `updating`.
- While the lifecycle tool updates the server, visitors see a short bilingual
  "back in a moment" page instead of a plain-text error.

### Security and privacy

- Server-side sessions: signing out, changing a password or being suspended
  really ends sessions; users can review and end their other sessions.
- Stricter Content-Security-Policy (no inline scripts or styles at all);
  CSRF protection without an expiry that broke long-open pages.
- Sign-in throttling per address and per account; no setup token in URLs.
- API tokens and worker tokens are shown once and never stored in cookies.
- No-history chats are never sent to volunteer worker PCs; worker job prompts
  are deleted as soon as a job ends.
- Deleted chats are erased after `BC_DELETED_CHAT_RETENTION_DAYS` (default 30);
  metrics after `BC_METRICS_RETENTION_DAYS` (default 30).
- Request bodies are limited per endpoint (the old global limit was 512 MB).
- Model downloads interrupted by a restart are resumed, never deleted.
- PDF text is extracted in a separate process with time and memory limits;
  API images with too many pixels are refused before reaching the model.
- Administrator invitations stop working when their creator is no longer an
  administrator; share-link tokens are kept out of the access log.
- Usage reported by worker PCs is capped at what the job can have used.
- Tokens reserved for image requests that died with their process are
  released automatically.

### Chat

- Rebuilt interface: collapsible sidebar with search across all chats,
  keyboard-accessible model picker, reasoning display, safer Markdown with
  tables and links, attachments by drag-and-drop or paste, voice input,
  read-aloud, PDF and Markdown export, share links that stay stable until
  revoked.
- Answers keep generating and are saved if the browser disconnects; reloading
  shows the progress. Stop works across server processes.
- Under heavy load, health checks and pages stay fast: generating requests
  never take the last threads of a server process, and requests over capacity
  are refused at once with HTTP 503 instead of hanging.
- Slow model loading no longer times out (`BC_FIRST_TOKEN_TIMEOUT`); models
  that fail before answering fall back to another one.
- No-history chats can be reloaded and ended explicitly.
- On phones, Enter starts a new line and the send button sends.

### API

- OpenAI-compatible errors with proper types, `created` fields, a single role
  chunk in streams, `stream_options.include_usage`, content arrays (text and
  images for vision models), `stop`, `seed` and penalties. `owned_by` is now
  `bananachat`. See [docs/api.md](docs/api.md).
- The token page moved to `/developer` (old `/api` links redirect).

### Administration

- New overview with service status, audit log, error log, session management
  per user, quota defaults applied correctly, access policies explained in
  plain words, metrics with CSV export, and a database export compatible with
  `restore --legacy-database`.
- Model names and descriptions edited by an administrator are no longer
  overwritten by the automatic model sync.

### Lifecycle tool

- Every kind of automatic backup package is pruned (not only `auto-`).
- A failed revision is retried only with `update --retry-failed`.
- Data permissions are fixed without following links; purging refuses system
  directories; Git errors are shown (with credentials removed).

### New optional settings

`BC_SESSION_DAYS`, `BC_DEFAULT_LANGUAGE`, `BC_QUEUE_TIMEOUT`,
`BC_FIRST_TOKEN_TIMEOUT`, `BC_MAX_NUM_CTX`, `BC_WORKER_CLAIM_TIMEOUT`,
`BC_DELETED_CHAT_RETENTION_DAYS`, `BC_METRICS_RETENTION_DAYS`, `BC_WORKERS`,
`BC_INFERENCE_LOCAL`, `BC_INFERENCE_FALLBACK_API_KEY`, `BC_AGENTS_RUNNER_URL`,
`BC_AGENTS_RUNNER_TOKEN_FILE`, `BC_AGENTS_MAX_UPLOAD_MB`, `BC_AGENTS_GIT_PROXY` (sandbox runner:
`BC_SANDBOX_*`, see docs/agents.md).
Invalid values no longer stop the server; they fall back to defaults with a
warning in the log. See [docs/configuration.md](docs/configuration.md).

### Removed

- The `/admin-access` sign-in page (it redirects to the normal sign-in, which
  now works during maintenance).
- Flask-WTF and the unused shared HTTP/logging helper modules.
- The per-user quota form and "apply defaults to everyone" in the old admin
  quota page; the new per-account limits panel and "Restore everyone's default
  limits" replace them.
