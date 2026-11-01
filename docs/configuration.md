# Configuration

BananaChat reads its configuration from environment variables when it
starts. Managed installations keep them in `/opt/bananachat/config/app.env`
(apply changes with `sudo bananachat restart`); for a manual installation,
export them or use your process supervisor's environment file.
[`.env.example`](../.env.example) lists every variable of the web server and
the compute gateway with its default (the sandbox runner and the worker
daemon are described in [agents](agents.md) and [workers](workers.md)).

Everything has a safe default, so an update never requires new settings. An
invalid value does not stop the server: it is replaced by the default (or
clamped into its range) and reported as a warning in the log at start-up.
Malformed Ollama, ComfyUI and fallback URLs disable that endpoint instead of
redirecting requests to a default server. Fix the named environment variable
and restart to restore it. URLs require HTTP(S), a valid port, and no embedded
credentials, whitespace, query or fragment; encode non-ASCII path characters.
Invalid bearer tokens are omitted, and warnings never include their values.
The database settings (`BC_DB_*`) are the exception: an invalid or
out-of-range value stops the start with an error naming the variable.
Booleans accept `1/0`, `true/false`, `yes/no` and `on/off`.

Settings that administrators change in the browser (site name, sign-up,
maintenance mode, theme, quotas, access policies, the music programme) are
stored in the database, not here. Maintenance mode and model-server outages
do not close the site: users can sign in and read their chats. Maintenance
pauses new answers. While the local model server is down, hosted models keep
answering. Monitors should poll `GET /status` (always HTTP 200, `status` is `ok`, `degraded`, `maintenance`,
`outage` or `updating`); `GET /health` is the readiness probe.

## Offline model-server notices

In **Admin → Site settings → Maintenance and announcements → Model-server
status**, **Show the model-server offline warning** turns the notice on or off
for all users (on by default). Users can choose **Hide for 24 hours** on an
offline or backup-server notice; the choice is kept in that browser for that
account, or for the current page only when browser storage is blocked. Hiding
a notice changes only what users see, not `/status` or which models answer.

## Limits

Usage limits have no environment variables: administrators set them in
**Admin → Limits**. Everything is counted in **tokens** (prompt plus answer).

* **Chat token consumption**: two switches decide whether chats use token
  allowances. **Apply token limits to local chats** is off by default (models
  on your own compute servers count as local); **Apply token limits to cloud
  chats** is on. Both apply to regular and no-history chats. Usage is always
  recorded, but turning a switch on does not charge earlier chats. Request
  rates and access rules apply either way, and each model's counting setting
  (below) decides which allowance its usage spends.
* **Services** (API, chat, agents), each with:
  * a **request rate**: one or more rules such as “60 requests per minute”
    and “1,000 per day” (per second, minute, hour or day, each with an
    optional burst); every rule must pass. One set of buckets per account and
    service, shared by every API key, the playground, chat and agents;
  * **tokens per 5 hours**: a window opens with the account's first request
    and lasts 5 hours, then the single allowance is full again. Once it is
    spent, further requests that count towards it are refused until the reset
    or an approved increase;
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
  as fast), whether it **counts towards the service limits** (on by default,
  also for external API models), and
  optional limits of its own (request-rate rules, tokens per 5 hours and per
  week, counted over every service, with a demand *sensitivity*: 2 doubles
  every dynamic change). The **strictness presets** fill these in one click:
  *light* (×0.5, no model limits), *standard* (×1) and *heavy* (×3, 6
  requests a minute and 200 a day, 200k tokens per 5 hours, sensitivity 2,
  reasoning up to Low). A model nobody configured follows the preset the
  catalogue gives it (`ai_models.limit_preset`), else *standard*.
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
* **Music programme** - Admin → Music can offer participants a token multiplier
  or fixed extra tokens. Fixed bonuses for the five-hour and weekly allowances
  can be set independently. An unset weekly bonus is seven times the
  five-hour bonus.
* **Requests** - users ask for more tokens per 5 hours or per week, a higher
  request rate (one of their rules), a temporary increase, or a higher
  reasoning effort. 5-hour requests (and weekly ones, when an amount is set)
  whose requested total is within the automatic-approval amount are approved
  at once; the others wait for an administrator. An amount approved automatically never holds the
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
the account), reasoning-effort levels, speed (slow, normal or fast: the
account's queue priority, which does not depend on tokens used), tier (and a
lock), dynamic exemption and usage resets. Every change is recorded in the
audit log.

An account can be exempted from token limits, from request rates, or from one
model's own limits (the service limits then still apply); a model can be
exempted for everyone. Exempt usage is still recorded, and access rules, model
locks, reasoning controls and image generation's own per-minute cap still
apply.

Installations upgraded from releases that counted credits had their amounts
converted at 1 credit = 1,000 tokens, with a daily amount becoming the same
amount per five hours; see [Architecture → Limits](architecture.md#limits).

## Network and security

| Variable | Default | Purpose |
| --- | --- | --- |
| `BC_HOST` | `127.0.0.1` | Address the web server listens on. |
| `BC_PORT` | `8000` | Port the web server listens on. |
| `BC_PROXY_MODE` | `0` | Trust `X-Forwarded-*` headers from a reverse proxy on the same host. They are read only on connections from a loopback address; any other peer keeps its own address. Without it, every visitor behind a same-host proxy shares the proxy's address for sign-in and other per-address limits; a warning is logged once when such forwarded requests arrive. |
| `BC_PROXY_HOPS` | `1` | Number of trusted proxies in front of the application (1–5). |
| `BC_SECURE_COOKIES` | same as `BC_PROXY_MODE` | Send the session cookie over HTTPS only, named with the `__Host-` prefix (`__Host-bc_session`) so other subdomains of the same domain cannot set it. A warning is logged when proxy mode is on but secure cookies are off. |
| `BC_SESSION_COOKIE_NAME` | `bc_session` | Session cookie name (change it when sharing a domain). Do not include `__Host-`: it is added for secure cookies. |
| `BC_LOGIN_ATTEMPTS_PER_MINUTE` | `60` | Sign-in requests per client address (an IPv4 address or an IPv6 /64) and minute. A browser that signed in to the account before has its own allowance; such browsers together get ten times this per address. |
| `BC_LOGIN_FAILURES_PER_ADDRESS` | `20` | Failed sign-ins per client address in 15 minutes before that address is paused. Browsers that signed in to the account before are counted apart (ten times this per address), so strangers on a shared network (a school or office NAT, mobile CGNAT, a VPN) cannot lock their owners out. Raise it when many people share one public address. |
| `BC_LOGIN_FAILURES_PER_ACCOUNT` | `10` | Failed sign-ins per account in 15 minutes (counted apart for each browser that signed in to it before). Successful sign-ins never count. |
| `BC_SIGNUPS_PER_HOUR` | `30` | Accounts created per client address and hour. Raise it when many people sign up from one network. |
| `BC_SIGNUP_FAILURES_PER_HOUR` | `20` | Failed sign-up attempts (wrong invitation code, taken name) per client address and hour. |
| `BC_SESSION_DAYS` | `7` | Days of inactivity before a sign-in expires. |
| `BC_SECRET_KEY` / `SECRET_KEY` | generated | Signs session cookies, and derives the keys that encrypt external provider API keys and authenticator-app secrets. By default a random key is created once in `<instance>/.secret_key` (mode 0600). Changing it signs everyone out, makes stored provider keys unreadable (enter them again) and stops authenticator-app codes from working (those accounts sign in with a recovery code and set two-step verification up again); keep it with the instance data in backups. |
| `BC_SETUP_TOKEN` | derived from the key | Token required to create the first administrator. Without it, the token is printed in the log until setup is complete. |
| `BC_PASSWORD_HASH_METHOD` | `auto` | Werkzeug hashing method; `auto` uses scrypt. Older hashes are upgraded at sign-in. |
| `BC_MIN_FORM_SECONDS` | `0.4` | Sign-in and sign-up forms submitted faster than this are treated as bots. |
| `BC_SOURCE_URL` | this project's repository | Where users can obtain the source code (AGPL-3.0 section 13). Point it at your fork when you run modified code. |
| `BC_ENV` | `production` | `development` downgrades some start-up checks (such as key-file permissions) to warnings. |
| `BC_DEFAULT_LANGUAGE` | `it` | Interface language when the browser prefers none of Italian, English or German (`it`, `en` or `de`). |

## Storage and logs

| Variable | Default | Purpose |
| --- | --- | --- |
| `BC_INSTANCE_DIR` | `instance/` in the checkout | Data directory: database, secret key, uploads, logs. Keep it private to the service account. |
| `BC_DATABASE_PATH` | `<instance>/bananachat.db` | SQLite database. |
| `BC_LOG_FILE` | `<instance>/bananachat.log` | Application log (also written to standard error). |
| `BC_LOGGING_LEVEL` | `verbose` | `off`, `minimal` (errors), `medium` (warnings), `verbose`, `debug`. |
| `BC_LOG_FORMAT` | `text` | `text`, or `json` for one JSON object per line with `time`, `level`, `logger`, `message` and, for errors, `exception`. |
| `BC_DB_BUSY_TIMEOUT_MS` | `5000` | How long a write waits for the database lock (100–30000). |
| `BC_DB_CACHE_KIB` | `4096` | SQLite page cache per connection (256–65536). |
| `BC_DB_SYNCHRONOUS` | `FULL` | `FULL` or `NORMAL` durability. |

## Inference

| Variable | Default | Purpose |
| --- | --- | --- |
| `BC_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama server (or a BananaChat compute server). On a managed web server `bananachat backend connect` sets it. |
| `BC_OLLAMA_API_KEY` | empty | Bearer token for an authenticated compute server or compatible proxy. Sent only over HTTPS or to a loopback address (an SSH tunnel): with a plain `http://` address of another machine it is not sent and a warning is logged. |
| `BC_OLLAMA_MANAGEMENT_API_KEY` | empty | Sent instead of `BC_OLLAMA_API_KEY` for model downloads and deletions, when the compute gateway requires a separate management token (`BC_COMPUTE_MANAGEMENT_TOKEN_FILE`, see [compute/README.md](../compute/README.md)). On a managed web server, `install --pair` and `bananachat backend connect` set it from the pairing code after checking it against the gateway, and remove it when the code has none. Same HTTPS-or-loopback rule. |
| `BC_INFERENCE_LOCAL` | automatic | Whether Ollama runs on this machine. Automatically, a URL with `BC_OLLAMA_API_KEY` (a compute gateway, also through an SSH tunnel on `127.0.0.1`) or a non-loopback host is remote, and only a loopback URL without a key is local. Local means: free memory gates requests, the model folder's free space is checked, GPU statistics come from `nvidia-smi` and no outage probing; remote means the opposite. Set `1` or `0` only for unusual setups, such as a local authenticating proxy. |
| `BC_OLLAMA_SYNC_INTERVAL` | `60` | Seconds between model-list refreshes. |
| `BC_KEEP_ALIVE` | `0` | How long Ollama keeps a model loaded after a request (seconds, `-1` forever). |
| `BC_MAX_CONCURRENT` | `4` | Requests generating at the same time, across all processes. Size it for your hardware. |
| `BC_MAX_CONCURRENT_HOSTED` | `16` | Requests to hosted models (external API providers, Claude and Codex pools) generating at the same time, across all processes (1–256). They wait and run in a lane of their own, so an unreachable Ollama or worker never holds them up, and they never take `BC_MAX_CONCURRENT`'s slots. |
| `BC_MAX_QUEUE_DEPTH` | `50` | Requests waiting or running before new ones are refused. |
| `BC_QUEUE_TIMEOUT` | `120` | Seconds a request may wait in the queue. |
| `BC_GENERATION_TIMEOUT` | `300` | Seconds a single answer may take. |
| `BC_FIRST_TOKEN_TIMEOUT` | `180` | Seconds to wait for the first token (model loading, long prompts). |
| `BC_INFERENCE_READ_TIMEOUT` | `30` | Seconds allowed between tokens once an answer started. |
| `BC_MAX_OUTPUT_TOKENS` | `8192` | Upper limit on generated tokens. |
| `BC_MAX_NUM_CTX` | `32768` | Upper limit on the context window users and API clients may request. |
| `BC_MIN_FREE_MEMORY_MB` | `256` | With a local Ollama (see `BC_INFERENCE_LOCAL`), wait for this much free RAM before starting a request (0 disables). |
| `BC_HTTP_THREADS` | `16` | Threads per Gunicorn process (8–64). Four of them are always kept free of generating requests so health checks and pages stay responsive; the rest bound how many answers one process streams at once. `BC_WORKERS` sets the number of processes (default 2). |
| `BC_DOWNLOAD_THREADS` | `8` | Threads per process, on top of `BC_HTTP_THREADS`, for large responses (files, attachments, exports, chat downloads, pages over 64 KB), which hold a thread for as long as their reader takes. At most this many are written at once per process (503 with Retry-After beyond), at most a quarter of them for one person or address (429). A worker's job over 64 KB takes one (or a long-request place when none is left) before it is claimed, one per worker at a time. |
| `BC_WRITE_TIMEOUT` | `120` | Seconds a response may wait for a client that stopped reading it (0–3600, `0` waits forever) before Gunicorn gives up and frees the thread. Answers never wait for the client: a streamed chat, API, playground or image answer keeps generating, ends at `BC_GENERATION_TIMEOUT` and frees its inference place whether or not anyone reads it. |
| `BC_BODY_STALL_TIMEOUT` | `20` | Seconds a request body may stop arriving (0–600, `0` turns both body limits off) before the request ends with `408` and its server thread is freed. A client sending a body very slowly otherwise holds a thread (AUDIT.md LC1-1). |
| `BC_BODY_MIN_KBPS` | `32` | Slowest accepted upload speed in KB/s (0–100000, `0` allows any speed): a body may take 10 seconds plus its size at this speed in total (a body of unknown length, 2 minutes). The generated Caddyfile's `read_body 2m` still applies on top. |
| `BC_INFERENCE_OUTAGE_MODE` | `shutdown` | For a remote Ollama that stops answering: `shutdown` keeps the site up with a banner and pauses sending until it is back; `fallback` switches to `BC_INFERENCE_FALLBACK_URL` and shows an informational banner. `fallback` without a fallback URL is reported and runs as `shutdown`. |
| `BC_INFERENCE_FALLBACK_URL` | empty | Backend used in `fallback` mode: an `http(s)` URL you choose explicitly (there is no default, so a web server never falls back to whatever listens on its own port 11434). Only chat and model lists use it; model downloads and deletions always go to `BC_OLLAMA_URL`. |
| `BC_INFERENCE_FALLBACK_API_KEY` | empty | Bearer token sent to the fallback, under the same HTTPS-or-loopback rule. The compute token (`BC_OLLAMA_API_KEY`) is never sent to the fallback. |
| `BC_INFERENCE_HEALTH_INTERVAL` | `15` | Seconds between health probes of a remote backend. |
| `BC_INFERENCE_HEALTH_FAILURES` | `3` | Failed probes before an outage is declared. |
| `BC_WORKERS_ENABLED` | `0` | Allow volunteer worker PCs (see [workers](workers.md)). |
| `BC_WORKER_CLAIM_TIMEOUT` | `20` | Seconds a job may wait for a worker before it runs locally instead. |
| `BC_WORKER_REMOTE_ACCESS` | `0` | Maintenance access to worker PCs whose owners allow it: the built-in terminal and VNC viewer in Administration → Workers, and native SSH, VNC and RDP clients through the connect helper. Every session needs a recent confirmation and is logged ([workers](workers.md#maintenance-access)). |
| `BC_WORKER_SUBNET` | `10.77.0.0/16` | The virtual network that gives each worker an address for the connect helper (a private IPv4 network from /8 to /24). It is never routed by any operating system; changing it gives every worker a new address. |
| `BC_WORKER_TUNNELS_PER_PROCESS` | `4` | Remote-session WebSockets one Gunicorn process relays at once (1–64). Each holds an HTTP thread for as long as the session is open: raise `BC_HTTP_THREADS` with it. |
| `BC_OLLAMA_MODEL_DIR` | `OLLAMA_MODELS`, else `~/.ollama/models` | Checked for free space before model downloads when Ollama is local (empty disables). |
| `BC_MIN_FREE_DISK_GB` | `2` | Minimum free space for a model download. |

## Chats and uploads

| Variable | Default | Purpose |
| --- | --- | --- |
| `BC_CHAT_MAX_FILES` | `4` | Attachments per message. |
| `BC_CHAT_MAX_REQUEST_MB` | `8` | Largest message upload. |
| `BC_CHAT_MAX_IMAGE_MB` / `BC_CHAT_MAX_IMAGE_PIXELS` | `5` / `20000000` | Image attachment limits. |
| `BC_CHAT_MAX_IMAGE_DIMENSION` | `2048` | Longest side, in pixels, an image attachment is stored and sent at; larger images are scaled down (large photos are decoded at a reduced scale, which bounds the memory an upload takes). |
| `BC_CHAT_MAX_DOCUMENT_MB` | `5` | PDF attachment limit. |
| `BC_CHAT_MAX_TEXT_KB` | `1024` | Text/code attachment limit. |
| `BC_CHAT_MAX_EXTRACTED_CHARS` | `200000` | Text kept from one document. |
| `BC_CHAT_MAX_SESSION_ATTACHMENT_MB` | `40` | Total attachments per chat, counted as stored: a document counts the text extracted from it (a small compressed PDF can hold far more), an image its stored size. |
| `BC_CHAT_MAX_ACCOUNT_ATTACHMENT_MB` | `1024` | Total attachments one account may store across its chats, counted as stored (as above); deleted chats count until they are erased. Administrators are exempt. |
| `BC_CHAT_MAX_ACCOUNT_TEXT_MB` | `1024` | Total message text (questions and answers) one account may store across its chats, with the copies of its no-history chats kept for administrators; deleted chats count until they are erased. A message over it is refused (413 `storage_limit`). Administrators are exempt. |
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
| `BC_IMAGE_TOKENS_PER_GENERATION` | `5000` | Tokens charged per image (× the image model's weight). Defaults to `BC_IMAGE_CREDITS_PER_GENERATION` × 1,000 when only that one is set. |
| `BC_IMAGE_CREDITS_PER_GENERATION` | `5` | Deprecated: the same charge in credits, from releases before 1.6. Use `BC_IMAGE_TOKENS_PER_GENERATION`. |
| `BC_IMAGE_CREDIT_RESERVATION_TTL`, `…_HEARTBEAT` | `1200`, `30` | How long an image job holds its reserved tokens. |
| `BC_CHECKPOINT_AGENT_URL`, `BC_CHECKPOINT_AGENT_TOKEN_FILE` | empty | Optional [checkpoint downloader](compute.md) on the compute server. HTTPS is required unless it is on this host or its address is in the next setting. |
| `BC_CHECKPOINT_AGENT_INSECURE_NETWORKS` | empty | Comma-separated private networks (CIDR, or single addresses) where plain HTTP to the agent is accepted, e.g. `192.168.10.0/24`. See [plain HTTP on private networks](#plain-http-on-private-networks). |
| `BC_CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE` | `0` | Deprecated alias: `1` adds Tailscale's ranges (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) to `BC_CHECKPOINT_AGENT_INSECURE_NETWORKS` and also accepts MagicDNS names ending in `.ts.net`. |

## Cloud sessions

Cloud sessions run commands in sandboxes managed by the sandbox runner on the
compute server, never on the web server. The feature is off until an
administrator enables it in Admin → Cloud sessions; limits are set there too.
See [cloud sessions](agents.md).

| Variable | Default | Purpose |
| --- | --- | --- |
| `BC_AGENTS_RUNNER_URL` | empty | The sandbox runner, e.g. `https://runner.example.org` or `http://127.0.0.1:11436` through an SSH tunnel. HTTPS is required unless it is on this host or its address is in `BC_AGENTS_RUNNER_INSECURE_NETWORKS`. |
| `BC_AGENTS_RUNNER_TOKEN_FILE` | empty | File (mode 0600) holding the runner's bearer token. Preferred over the next variable. |
| `BC_AGENTS_RUNNER_TOKEN` | empty | The runner's bearer token, when a file is not practical. |
| `BC_AGENTS_RUNNER_INSECURE_NETWORKS` | empty | Comma-separated private networks (CIDR, or single addresses) where plain HTTP to the runner is accepted. See [plain HTTP on private networks](#plain-http-on-private-networks). |
| `BC_AGENTS_RUNNER_ALLOW_INSECURE_TAILSCALE` | `0` | Deprecated alias: `1` adds Tailscale's ranges to `BC_AGENTS_RUNNER_INSECURE_NETWORKS` and also accepts MagicDNS names ending in `.ts.net`. |
| `BC_AGENTS_MAX_UPLOAD_MB` | `20` | Largest upload (files or an archive) copied into a task's workspace (1–200). |
| `BC_AGENTS_MAX_STORED_UPLOAD_MB` | `100` | Files one account added to cloud-session tasks that the web server keeps until they are copied into a sandbox (never below `BC_AGENTS_MAX_UPLOAD_MB`). A start with files over it gets 413 `storage_limit`. Administrators are exempt. |
| `BC_AGENTS_GIT_PROXY` | `0` | Send Git repository downloads through `HTTPS_PROXY` (the proxy is asked to connect to the address BananaChat checked). Hosts and limits are set in Admin → Cloud sessions. |

## Plain HTTP on private networks

The checkpoint agent and the sandbox runner receive a bearer token with every
request, so BananaChat only sends it over HTTPS, to a service on this host
(`localhost` or a loopback address, such as an SSH tunnel), or over plain HTTP
to an address inside a network you list in
`BC_CHECKPOINT_AGENT_INSECURE_NETWORKS` or `BC_AGENTS_RUNNER_INSECURE_NETWORKS`.
Each service has its own list. Use it when the compute server is reached over a
network you trust or that encrypts traffic itself, for example `192.168.10.0/24`
for a LAN, `10.8.0.0/24` for a WireGuard tunnel,
`100.64.0.0/10,fd7a:115c:a1e0::/48` for Tailscale or `172.18.0.0/16` for a
Docker network:

```sh
BC_AGENTS_RUNNER_URL=http://10.8.0.2:11436
BC_AGENTS_RUNNER_INSECURE_NETWORKS=10.8.0.0/24
```

- Only an IP address in the URL can match (`http://192.168.10.5:8765`). Host
  names are not resolved for this check, so a DNS change cannot move the token
  onto another network: use the address, or HTTPS with the name.
- Entries must lie inside loopback (`127.0.0.0/8`, `::1`), the private ranges
  `10.0.0.0/8`, `172.16.0.0/12` and `192.168.0.0/16`, the shared address space
  `100.64.0.0/10` (carrier-grade NAT and overlay VPNs such as Tailscale) or
  IPv6 unique local addresses (`fc00::/7`). Public ranges, `0.0.0.0/0`,
  `::/0` and link-local ranges are refused: an IPv6 link-local address needs a
  zone index that these URLs cannot carry, and `169.254.0.0/16` is rarely a
  network chosen for a service (cloud metadata services answer there). Write
  networks without host bits (`10.0.0.0/8`, not `10.0.0.1/8`), and list the
  narrowest one that holds the compute server (its own address is enough, such
  as `100.101.102.103` rather than all of `100.64.0.0/10`, where some cloud
  and VPN services answer too). A refused or malformed entry is logged as a warning at
  start-up and ignored; the other entries still apply.
- BananaChat adds no encryption of its own. A WireGuard or Tailscale tunnel
  encrypts the traffic; a LAN or a container network does not, so anyone on it
  can read the token. Restrict the service's port to the web server with a
  firewall either way.
- `BC_CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE=1` and
  `BC_AGENTS_RUNNER_ALLOW_INSECURE_TAILSCALE=1`, the settings of earlier
  releases, still work as deprecated aliases: they add Tailscale's ranges to
  the list and keep accepting MagicDNS names ending in `.ts.net`, which the
  list alone does not. Replace them with the networks when you can (and use
  the address, or HTTPS, instead of a MagicDNS name).

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

The compute server reads `BC_COMPUTE_HOST`, `BC_COMPUTE_PORT`,
`BC_COMPUTE_UPSTREAM`, `BC_COMPUTE_TOKEN_FILE`, the model management settings
`BC_COMPUTE_MANAGEMENT_TOKEN_FILE` (created by the installer on new compute
servers) and `BC_COMPUTE_MODEL_MANAGEMENT`, and its connection limits; see
[compute/README.md](../compute/README.md). The sandbox runner reads
`BC_SANDBOX_*` (see [agents](agents.md)) and the worker daemon has its own
variables (see [workers](workers.md)).

## External API providers

Hosted providers are set up in **Admin → Models → External APIs**, not in the
environment; see [external providers](external-providers.md). Their keys are
stored encrypted under `BC_INSTANCE_DIR/.provider-keys` with a key derived from
`BC_SECRET_KEY` (or `.secret_key`), so back up both together.

### Subscription pools

`BC_CLAUDE_EXTENSION` and `BC_CODEX_EXTENSION` each name an operator-installed
Python module exporting `create_adapter(config)` with cancellable chat,
discovery and quota callbacks. Both are empty by default, leaving that provider
disconnected. Invalid configuration or adapter initialisation clears every
callback of that provider and keeps it disabled; the other provider is not
affected. The pools store account labels, caps, budgets and usage, not
credentials.

For the built-in connectors, which run the official command-line tools under
private profiles signed in to their own subscriptions, set
`BC_CLAUDE_EXTENSION=bananachat.services.claude_code` with
`BC_CLAUDE_CODE_CONFIG`, and `BC_CODEX_EXTENSION=bananachat.services.codex_cli`
with `BC_CODEX_CLI_CONFIG`, each an absolute path to a private JSON manifest.
The server operator installs the tools; `sudo python3
/opt/bananachat/current/scripts/setup_subscriptions.py` then writes these
settings and manifests (with a `managed_root`), so administrators can create
private profiles and sign them in from the pages. Profiles signed in on the
command line keep working. Web administrators choose profiles, caps and budgets in
**Administration → Models → Claude** and **Codex**: an account can stop at a
share of the provider's own five-hour or weekly allowance (for example 70 % and
90 %), and per model family at a share of that family's allowance (such as
Fable). A cap on an allowance the provider does not report keeps the account
out of rotation. `BC_SUBSCRIPTION_PROBE_INTERVAL` (seconds, default 120) sets
how often usage is read again and resting accounts are returned to the pool.

Shared subscription routing requires the provider's approval; read its terms
before enabling a pool. See [Claude Code setup](claude-code.md),
[Codex setup](codex-cli.md) and the [router design](subscription-router.md)
for the adapter contract and the routing rules. Enabling a module alone does
not prove live sign-in or model access.
