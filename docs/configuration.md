# Configuration

BananaChat reads its configuration from environment variables when it
starts. Managed installations keep them in `/opt/bananachat/config/app.env`
(apply changes with `sudo bananachat restart`); for a manual installation,
export them or use your process supervisor's environment file.
[`.env.sample`](../.env.sample) lists every variable with its default.

Everything has a safe default, so an update never requires new settings. An
invalid value does not stop the server: it is replaced by the default (or
clamped into its range) and reported as a warning in the log at start-up.
The database settings (`BC_DB_*`) are the exception: an invalid or
out-of-range value stops the start with an error naming the variable.
Booleans accept `1/0`, `true/false`, `yes/no` and `on/off`.

Settings that administrators change in the browser (site name, sign-up,
maintenance mode, theme, quotas, access policies, the music program) are
stored in the database, not here. Maintenance mode and AI-server outages never
close the site: users can sign in and read their chats. Maintenance pauses new
answers; a local model-server outage still permits accessible hosted models.
Offline local models remain unavailable. Monitors should poll
`GET /status` (always HTTP 200, `status` is `ok`, `degraded`, `maintenance`,
`outage` or `updating`); `GET /health` is the readiness probe.

### Offline model-server notices

In **Admin → Site settings → Maintenance and announcements → Model-server
status**, **Show the model-server offline warning** controls the notice for
all users. It is enabled by default. Users can choose **Hide for 24 hours**
on an offline or backup-server notice. This choice is per account in the
current browser; it survives reloads and applies to other tabs until the
original 24 hours expire. If browser storage is blocked, the interface reports
that the choice lasts for the current page only.

Hiding a notice does not change `/status`, maintenance, access checks, quotas or
model availability. An unavailable local backend still refuses generation;
published, accessible Claude or external models can continue answering.

### Limits

Usage limits have no environment variables: administrators set them in
**Admin → Limits**. Everything is counted in **tokens** (prompt plus answer).

* **Chat token consumption** has two switches: local model chats are unmetered
  by default; cloud model chats consume token allowances by default to protect
  expensive external models. Site-owned Ollama workers on a separate compute
  server still count as local. These switches apply to regular and no-history
  chats. Existing local chat allowances need **Apply token limits to local chats** on
  to keep deducting tokens after an upgrade; their configured amounts are kept.
  Usage is recorded in either mode, and enabling consumption never
  charges earlier unmetered chats. Request rates, model locks and access rules
  remain active. Set token amounts under Chat and Models; each model's
  service-counting choice still decides whether its usage spends the chat
  allowance, its own allowance, or both. API, playground, images and agents
  retain their existing consumption rules.
* **Services** (API, chat, agents), each with:
  * a **request rate**: one or more rules such as “60 requests per minute”
    and “1,000 per day” (per second, minute, hour or day, each with an
    optional burst); every rule must pass. One set of buckets per account and
    service, shared by every API key, the playground, chat and agents;
  * **tokens per 5 hours**: a window opens with the account's first request
    and lasts 5 hours, then the single allowance is full again. Once it is
    spent, further requests that count toward it are refused until the reset
    or an approved increase; there is no slow-token spillover;
  * **tokens per week** (off by default): a rolling week that starts with the
    first request after the previous week ended. Both enabled token limits
    must have tokens left.

  Each limit can *adjust to demand* (dynamic: more when the server is quiet
  and few people are online and for accounts that use it off-peak and
  regularly, less under load, between 0.5× and 2×; the request rate is only
  ever lowered), and the token limits can *rise with tier levels*. Defaults:
  API 1 request per second (bursts of 10), 45k tokens per 5
  hours; chat 1 request per second (bursts of 5) and agents 1 per second
  (bursts of 10), without token limits.
* **Models** - each model has a **weight** (usage counts tokens × weight
  against the service limits: a heavy model ×3 uses the allowance three times
  as fast), whether it **counts toward the service limits** (on by default for
  models served here, off by default for models of other providers), and
  optional limits of its own (request-rate rules, tokens per 5 hours and per
  week, counted over every service, with a demand *sensitivity*: 2 doubles
  every dynamic change). The **strictness presets** fill these in one click:
  *light* (×0.5, no model limits), *standard* (×1) and *heavy* (×3, 6
  requests a minute and 200 a day, 200k tokens per 5 hours, sensitivity 2,
  reasoning up to Low). A model nobody configured follows the preset the
  catalog gives it (`ai_models.limit_preset`), else *standard*.
* **Reasoning effort** - levels `off < low < medium < high < extra < max`, as each
  model supports them. Everyone may use up to the default (Medium, or the
  model's own default); higher levels are unlocked for one account (one model
  or all), by an approved request, or automatically after sustained use (by
  default 30 active days and 2M tokens with that model in the last 60 days and
  no suspension in 90 days; one level at a time, never above High unless you
  raise the ceiling). Gating can be switched off for everyone, one account or
  one model. Automatic unlocks have separate model and account opt-outs. API
  requests may use `xhigh` as an alias for Extra; only model-supported levels
  are shown and accepted.
* **Tiers** - an ordered ladder with a token multiplier and promotion
  requirements (account age, active days and tokens used in the last 30 days,
  days without a suspension). Only usage of services with token limits counts
  (free, unlimited use earns nothing), for tiers and for the dynamic
  adjustment alike. With tiers on, accounts are promoted hourly, never demoted
  automatically.
* **Grants** - unlimited use, a multiplier or extra tokens for one account or
  everyone, for one service, all services or one model, for 1 hour, 24 hours,
  7 days, until a date, or forever. Unlimited use for every service also lifts
  the models' own limits; other grants for services leave them alone.
* **Music program** - Admin → Music can offer participants a token multiplier
  or fixed extra tokens. Fixed bonuses for the five-hour and weekly allowances
  can be set independently. An unset weekly bonus follows seven times the
  five-hour bonus; upgrades preserve the previous weekly bonus separately,
  even when active slow tokens are added to the five-hour bonus.
* **Requests** - users ask for more tokens per 5 hours or per week, a higher
  request rate (one of their rules), a temporary increase, or a higher
  reasoning effort. 5-hour requests (and weekly ones, when an amount is set)
  up to the automatic-approval amounts are approved at once; the others wait
  for an administrator. An amount approved automatically never holds the
  account below its tier's amount (it gets whichever is higher); an amount you
  set or approve yourself is exact. A temporary increase of tokens needs a service with a
  5-hour limit; elsewhere only unlimited use can be asked for (the same
  applies to extra grants).
* **Everyone** - reset everyone's usage (the 5-hour windows, or also the
  weekly ones: open windows end and the next request starts a fresh one; the
  usage history is kept), or restore everyone's default limits (removes
  custom limits, model locks and speed settings; tiers, grants and
  reasoning-effort unlocks stay).

Amounts can be typed as `50000`, `50k` or `1.5M`. Each account's page
(Admin → Users → *account* → Manage limits) shows its effective limits and
why, and sets custom limits per service and per model (and locks a model for
the account), reasoning-effort levels, speed (slow down or speed up), tier
(and a lock), dynamic exemption and usage resets. Every change is recorded in
the audit log.

Accounts can be exempted independently from internal token allowances and
request rates. A per-account model exemption removes only that model's own
token or rate limits; the service allowance and request rate still apply.
Models also have independent token/rate switches globally. Exemptions preserve
usage recording, access rules, model locks, reasoning controls and provider
availability: they never create capacity on an exhausted Claude subscription.
Request-rate exemptions cover these quota policy rules; image generation's
separate safety RPM cap, queue capacity and execution timeouts still apply.

### Claude provider extension

`BC_CLAUDE_EXTENSION` names an operator-installed Python module exporting
`create_adapter(config)` with cancellable chat, discovery and quota callbacks.
It is empty by default, leaving Claude disconnected. Invalid configuration or
adapter initialization clears every callback and keeps the provider disabled.
The pool stores account labels, local budgets and usage, not credentials.

For the built-in official Claude Code subscription connector, set
`BC_CLAUDE_EXTENSION=bananachat.services.claude_code` and
`BC_CLAUDE_CODE_CONFIG` to an absolute path to its private JSON manifest.
The server operator installs the official CLI and authenticates isolated
profiles; web administrators choose profiles and budgets. Shared subscription
routing requires provider approval. New profiles discover models and native
subscription usage automatically. Explicit manual model lists and local-only
usage remain available; failed automatic telemetry never silently selects
local-only mode. Claude has its own enrollment setting, and unchecked existing
models stay excluded when automatic enrollment is enabled.
See [Claude Code setup](claude-code.md)
for credentials, supported flags, text-only chat and usage observations, and
[router design](claude-router.md) for the extension contract. Enabling the
module alone does not prove live authentication or model access.

An account's **slow**, **normal** or **fast** speed setting controls queue
priority separately from its token allowance. Slow accounts can still be
configured; using up tokens no longer changes a request's queue priority.

The upgrade to a single allowance combines each previous regular allocation
with its slow allocation only when the site's slow tokens were enabled.
For example, 30k regular plus 15k enabled slow tokens becomes 45k per five
hours; with slow tokens disabled it stays 30k. Historical regular and slow
usage count together, and open five-hour and weekly windows keep their
start times: the upgrade does not refill or reset usage. Legacy slow-token
columns remain as compatibility data for rollback and are inactive in the
current release. Request rates, weekly and model limits, tiers, dynamic
adjustment, grants, reasoning settings and other customization are preserved.

Automatic approval now compares the requested total against one ceiling.
The upgrade adds the old configured regular and active slow approval caps;
it keeps the enable switch and other approval settings. The former exception
for an unchanged slow allocation no longer applies. For example, an old
50k regular approval cap plus a zero slow cap becomes a 50k total cap, even
if a request could previously keep 15k slow tokens unchanged. Set the new
ceiling to 65k if that is the total you want approved automatically.

For older installations using credits, upgrading first converts amounts at
1 credit = 1,000 tokens: the daily amount
becomes the same amount per **5 hours** (30 credits a day become 30,000 tokens
per 5 hours), which is more generous than a day; administrators may want to
lower it after upgrading. A *requests per minute* setting of the previous release becomes a
per-second rate with bursts of `rpm/6` (an empty API value means the old
default of 60; an empty or zero chat value becomes 1 per second with bursts of
5; an API value of 0 turns the API rate off), written as the rule with the
smallest whole unit (20 per minute, not 0.33 per second). Custom quotas,
tiers, grants, pending requests and the automatic-approval and music-bonus
amounts are converted too; the old credit columns are kept for older releases.
See [Architecture → Limits](architecture.md#limits) for the details.

## Network and security

| Variable | Default | Purpose |
| --- | --- | --- |
| `BC_HOST` | `127.0.0.1` | Address the web server listens on. |
| `BC_PORT` | `8000` | Port the web server listens on. |
| `BC_PROXY_MODE` | `0` | Trust `X-Forwarded-*` headers from a reverse proxy on the same host. Enable only behind a proxy. |
| `BC_PROXY_HOPS` | `1` | Number of trusted proxies in front of the application (1–5). |
| `BC_SECURE_COOKIES` | same as `BC_PROXY_MODE` | Send the session cookie over HTTPS only. |
| `BC_SESSION_COOKIE_NAME` | `bc_session` | Session cookie name (change it when sharing a domain). |
| `BC_SESSION_DAYS` | `7` | Days of inactivity before a sign-in expires. |
| `BC_SECRET_KEY` / `SECRET_KEY` | generated | Signs session cookies. By default a random key is created once in `<instance>/.secret_key` (mode 0600). Changing it signs everyone out. |
| `BC_SETUP_TOKEN` | derived from the key | Token required to create the first administrator. Without it, the token is printed in the log until setup is complete. |
| `BC_PASSWORD_HASH_METHOD` | `auto` | Werkzeug hashing method; `auto` uses scrypt. Older hashes are upgraded at sign-in. |
| `BC_MIN_FORM_SECONDS` | `0.4` | Sign-in and sign-up forms submitted faster than this are treated as bots. |
| `BC_SOURCE_URL` | this project's repository | Where users can obtain the source code (AGPL-3.0 section 13). Point it at your fork when you run modified code. |
| `BC_ENV` | `production` | `development` downgrades some start-up checks (such as key-file permissions) to warnings. |
| `BC_DEFAULT_LANGUAGE` | `it` | Interface language when the browser prefers neither Italian nor English (`it` or `en`). |

## Storage and logs

| Variable | Default | Purpose |
| --- | --- | --- |
| `BC_INSTANCE_DIR` | `instance/` in the checkout | Data directory: database, secret key, uploads, logs. Keep it private to the service account. |
| `BC_DATABASE_PATH` | `<instance>/bananachat.db` | SQLite database. |
| `BC_LOG_FILE` | `<instance>/bananachat.log` | Application log (also written to standard error). |
| `BC_LOGGING_LEVEL` | `verbose` | `off`, `minimal` (errors), `medium` (warnings), `verbose`, `debug`. |
| `BC_DB_BUSY_TIMEOUT_MS` | `5000` | How long a write waits for the database lock (100–30000). |
| `BC_DB_CACHE_KIB` | `4096` | SQLite page cache per connection (256–65536). |
| `BC_DB_SYNCHRONOUS` | `FULL` | `FULL` or `NORMAL` durability. |

## Inference

| Variable | Default | Purpose |
| --- | --- | --- |
| `BC_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama server (or a BananaChat compute node). On a managed web server `bananachat backend connect` sets it. |
| `BC_OLLAMA_API_KEY` | empty | Bearer token for an authenticated compute node or compatible proxy. Sent only over HTTPS or to a loopback address (an SSH tunnel): with a plain `http://` address of another machine it is not sent and a warning is logged. |
| `BC_INFERENCE_LOCAL` | automatic | Whether Ollama runs on this machine. Automatically, a URL with `BC_OLLAMA_API_KEY` (a compute gateway, also through an SSH tunnel on `127.0.0.1`) or a non-loopback host is remote, and only a loopback URL without a key is local. Local means: free memory gates requests, the model folder's free space is checked, GPU statistics come from `nvidia-smi` and no outage probing; remote means the opposite. Set `1` or `0` only for unusual setups, such as a local authenticating proxy. |
| `BC_OLLAMA_SYNC_INTERVAL` | `60` | Seconds between model-list refreshes. |
| `BC_KEEP_ALIVE` | `0` | How long Ollama keeps a model loaded after a request (seconds, `-1` forever). |
| `BC_MAX_CONCURRENT` | `4` | Requests generating at the same time, across all processes. Size it for your hardware. |
| `BC_MAX_QUEUE_DEPTH` | `50` | Requests waiting or running before new ones are refused. |
| `BC_QUEUE_TIMEOUT` | `120` | Seconds a request may wait in the queue. |
| `BC_GENERATION_TIMEOUT` | `300` | Seconds a single answer may take. |
| `BC_FIRST_TOKEN_TIMEOUT` | `180` | Seconds to wait for the first token (model loading, long prompts). |
| `BC_INFERENCE_READ_TIMEOUT` | `30` | Seconds allowed between tokens once an answer started. |
| `BC_MAX_OUTPUT_TOKENS` | `8192` | Upper limit on generated tokens. |
| `BC_MAX_NUM_CTX` | `32768` | Upper limit on the context window users and API clients may request. |
| `BC_MIN_FREE_MEMORY_MB` | `256` | With a local Ollama (see `BC_INFERENCE_LOCAL`), wait for this much free RAM before starting a request (0 disables). |
| `BC_HTTP_THREADS` | `16` | Threads per Gunicorn process (8–64). Four of them are always kept free of generating requests so health checks and pages stay responsive; the rest bound how many answers one process streams at once. `BC_WORKERS` sets the number of processes (default 2). |
| `BC_INFERENCE_OUTAGE_MODE` | `shutdown` | For a remote Ollama that stops answering: `shutdown` keeps the site up with a banner and pauses sending until it is back; `fallback` switches to `BC_INFERENCE_FALLBACK_URL` and shows an informational banner. `fallback` without a fallback URL is reported and runs as `shutdown`. |
| `BC_INFERENCE_FALLBACK_URL` | empty | Backend used in `fallback` mode: an `http(s)` URL you choose explicitly (there is no default, so a web server never falls back to whatever listens on its own port 11434). Only chat and model lists use it; model downloads and deletions always go to `BC_OLLAMA_URL`. |
| `BC_INFERENCE_FALLBACK_API_KEY` | empty | Bearer token sent to the fallback, under the same HTTPS-or-loopback rule. The compute token (`BC_OLLAMA_API_KEY`) is never sent to the fallback. |
| `BC_INFERENCE_HEALTH_INTERVAL` | `15` | Seconds between health probes of a remote backend. |
| `BC_INFERENCE_HEALTH_FAILURES` | `3` | Failed probes before an outage is declared. |
| `BC_WORKERS_ENABLED` | `0` | Allow volunteer worker PCs (see [workers](workers.md)). |
| `BC_WORKER_CLAIM_TIMEOUT` | `20` | Seconds a job may wait for a worker before it runs locally instead. |
| `BC_OLLAMA_MODEL_DIR` | `OLLAMA_MODELS`, else `~/.ollama/models` | Checked for free space before model downloads when Ollama is local (empty disables). |
| `BC_MIN_FREE_DISK_GB` | `2` | Minimum free space for a model download. |

## Chats and uploads

| Variable | Default | Purpose |
| --- | --- | --- |
| `BC_CHAT_MAX_FILES` | `4` | Attachments per message. |
| `BC_CHAT_MAX_REQUEST_MB` | `8` | Largest message upload. |
| `BC_CHAT_MAX_IMAGE_MB` / `BC_CHAT_MAX_IMAGE_PIXELS` | `5` / `20000000` | Image attachment limits. |
| `BC_CHAT_MAX_DOCUMENT_MB` | `5` | PDF attachment limit. |
| `BC_CHAT_MAX_TEXT_KB` | `1024` | Text/code attachment limit. |
| `BC_CHAT_MAX_EXTRACTED_CHARS` | `200000` | Text kept from one document. |
| `BC_CHAT_MAX_SESSION_ATTACHMENT_MB` | `40` | Total attachments per chat. |
| `BC_CHAT_MAX_CONTEXT_CHARS` | `300000` | History characters sent to the model. |
| `BC_CHAT_MAX_CONTEXT_IMAGES` | `8` | Images sent to the model. |
| `BC_CHAT_MAX_HISTORY_MESSAGES` | `100` | Messages sent to the model. |
| `BC_CHAT_MAX_RESPONSE_KB` | `1024` | Longest stored answer. |
| `BC_BACKGROUND_IMAGE_MAX_MB`, `…_MAX_PIXELS`, `…_MAX_DIMENSION` | `4`, `16000000`, `2560` | Custom background images. |

## Retention

| Variable | Default | Purpose |
| --- | --- | --- |
| `BC_NO_HISTORY_TTL_HOURS` | `24` | No-history chats are erased this long after their last message. Copies kept for the administrator audit remain until an administrator deletes them. |
| `BC_DELETED_CHAT_RETENTION_DAYS` | `30` | Deleted chats are erased permanently after this many days (`0` erases immediately). |
| `BC_METRICS_RETENTION_DAYS` | `30` | Request statistics and hardware snapshots older than this are removed. |
| `BC_COMPUTE_SNAPSHOT_INTERVAL` | `30` | Seconds between the hardware snapshots (CPU, memory, GPU, loaded models) shown in Admin → Metrics (5–3600). |

## Image generation

| Variable | Default | Purpose |
| --- | --- | --- |
| `BC_IMAGE_BACKEND` | `disabled` | `comfyui` enables image generation. See [ComfyUI](comfyui.md). |
| `BC_COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI server. |
| `BC_COMFYUI_TIMEOUT`, `BC_COMFYUI_QUEUE_TIMEOUT`, `BC_COMFYUI_GENERATION_TIMEOUT`, `BC_COMFYUI_POLL_INTERVAL` | `30`, `300`, `600`, `1` | Request, queue and generation deadlines. |
| `BC_COMFYUI_SAMPLER`, `BC_COMFYUI_SCHEDULER`, `BC_COMFYUI_STEPS`, `BC_COMFYUI_CFG` | `euler`, `normal`, `20`, `7` | Sampling settings. |
| `BC_IMAGE_GENERATION_RPM` | `6` | Images per user per minute. |
| `BC_IMAGE_TOKENS_PER_GENERATION` | `5000` | Tokens charged per image (× the image model's weight). Defaults to `BC_IMAGE_CREDITS_PER_GENERATION` × 1,000 when only that setting of the previous release is present. |
| `BC_IMAGE_CREDITS_PER_GENERATION` | `5` | The previous release's setting in credits; still read as the default of the one above. |
| `BC_IMAGE_CREDIT_RESERVATION_TTL`, `…_HEARTBEAT` | `1200`, `30` | How long an image job holds its reserved tokens. |
| `BC_CHECKPOINT_AGENT_URL`, `BC_CHECKPOINT_AGENT_TOKEN_FILE` | empty | Optional [checkpoint downloader](compute.md) on the compute host. |
| `BC_CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE` | `0` | Permit plain HTTP to the agent over Tailscale. |

## Agents

Coding agents run commands in sandboxes managed by the sandbox runner on the
compute host, never on the web server. The feature is off until an
administrator enables it in Admin → Agents; limits are set there too. See
[Agents](agents.md).

| Variable | Default | Purpose |
| --- | --- | --- |
| `BC_AGENTS_RUNNER_URL` | empty | The sandbox runner, e.g. `https://runner.example.org` or `http://127.0.0.1:11436` through an SSH tunnel. HTTPS is required unless it is on this host. |
| `BC_AGENTS_RUNNER_TOKEN_FILE` | empty | File (mode 0600) holding the runner's bearer token. Preferred over the next variable. |
| `BC_AGENTS_RUNNER_TOKEN` | empty | The runner's bearer token, when a file is not practical. |
| `BC_AGENTS_RUNNER_ALLOW_INSECURE_TAILSCALE` | `0` | Permit plain HTTP to the runner over Tailscale. |
| `BC_AGENTS_MAX_UPLOAD_MB` | `20` | Largest upload (files or an archive) copied into a task's workspace (1–200). |
| `BC_AGENTS_GIT_PROXY` | `0` | Send Git repository downloads through `HTTPS_PROXY` (the proxy is asked to connect to the address BananaChat checked). Hosts and limits are set in Admin → Agents. |

## Managed installations

The lifecycle tool writes `app.env` for the chosen role and keeps your edits
on updates. It also sets `BANANA_MAINTENANCE_FILE`: while that file exists
(during updates and backups) every page except the health check answers 503.
Repository credentials and the automatic-update policy are managed with
`bananachat source` and `bananachat updates`; see [deployment](deployment.md).

A managed `single` or `compute` server that runs its own Ollama also gets
`OLLAMA_HOST` and `OLLAMA_MODELS` at installation; your later changes to them
are kept. One installed with `--ollama-url` uses an existing Ollama and gets
neither. A web server's compute connection is managed with
`bananachat backend status|test|connect` (see [deployment](deployment.md#check-or-change-the-connection-later)).

The compute node reads `BC_COMPUTE_HOST`, `BC_COMPUTE_PORT`,
`BC_COMPUTE_UPSTREAM` and `BC_COMPUTE_TOKEN_FILE`; the worker daemon has its
own variables (see [workers](workers.md)).

### External API providers

Configure hosted providers through **Administration → Models → External APIs**;
no environment change is required. OpenAI-compatible and native Anthropic
formats support operator-owned base URLs and private server API keys. Model
selection, publication, capabilities and quota controls remain explicit.
Keys live under `BC_INSTANCE_DIR/.provider-keys` and must be preserved with the
instance data during updates/restores. Public endpoints require HTTPS; trusted
private gateways need explicit permission. See [external provider setup](external-providers.md).
