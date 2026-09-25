# Configuration

Set environment variables before starting BananaChat. Runtime administrator settings are stored in SQLite. See [config.py](../config.py) for the complete list and current defaults.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BC_HOST` | `127.0.0.1` | Application listener |
| `BC_PORT` | `8000` | Application port |
| `BC_PROXY_MODE` | `0` | Trust forwarded headers on a protected proxy listener |
| `BC_PROXY_HOPS` | `1` | Number of trusted forwarding proxies |
| `BC_SECURE_COOKIES` | Enabled with proxy mode | Require HTTPS for login cookies |
| `BC_INSTANCE_DIR` | `instance/` | Persistent application data |
| `BC_DATABASE_PATH` | `bananachat.db` under the instance directory | SQLite database |
| `SECRET_KEY` | Generated and persisted | Optional operator-supplied session key |
| `BC_SETUP_TOKEN` | Derived from the session key | First-administrator installation token |
| `BC_OLLAMA_URL` | `http://127.0.0.1:11434` | Inference backend |
| `BC_OLLAMA_API_KEY` | Empty | Bearer credential for an authenticated proxy or compatible API |
| `BC_LOG_FILE` | Under `instance/` | Application log path |
| `BC_SOURCE_URL` | Public BananaChat repository | Corresponding source shown in the interface |

A few more that a plain installation may want:

| Variable | Default | Purpose |
| --- | --- | --- |
| `BC_ENV` | `production` | `development` relaxes the checks meant for a real server |
| `BC_LOGGING_LEVEL` | `verbose` | Application log verbosity |
| `BC_SESSION_COOKIE_NAME` | `bc_session` | Rename the session cookie when sharing a domain |
| `BC_PASSWORD_HASH_METHOD` | `auto` | Password hashing algorithm |
| `BC_NO_HISTORY_TTL_HOURS` | `24` | How long an incognito chat survives before deletion |
| `BC_MIN_FREE_DISK_GB` | `2` | Refuse model downloads below this much free space |
| `BC_MAX_BACKUP_UPLOAD_MB` | `512` | Largest backup archive accepted on restore |
| `BC_INFERENCE_OUTAGE_MODE` | `shutdown` | What to do when the backend stops answering |
| `BC_INFERENCE_FALLBACK_URL` | local Ollama | Backend to fall back to |

The remaining groups are tuning knobs and are documented beside their defaults
in `config.py` and `worker/config.py`: `BC_CHAT_MAX_*` for what a single
message may carry, `BC_BACKGROUND_IMAGE_*` for interface image uploads,
`BC_COMFYUI_*` for image generation sampling and timeouts (see
[ComfyUI](comfyui.md)), `BC_IMAGE_CREDIT_RESERVATION_*` for how long an image
job holds its credits, `BC_INFERENCE_HEALTH_*` for backend health checks, and
the `BC_OLLAMA_*`, `BC_WORKER_*`, `BC_GPU_*` and interval settings for the
split compute deployment (see [deployment](deployment.md)). The web side
reads `BC_OLLAMA_URL` while a compute worker reads `BC_OLLAMA_HOST`; they are
separate processes and separate settings.

Keep the data directory writable only by the application account and trusted operators. Do not rotate the persistent session key on every restart; changing it invalidates sessions.

Ollama itself does not validate a local server API key. Use the authenticated compute gateway or another protected backend connection as described in [deployment](deployment.md#two-servers). Optional image generation has its own [ComfyUI configuration](comfyui.md).

Administrators manage models, user roles, invitations, per-user quotas, retention, and interface settings in the browser. Incognito chats are excluded from user history but may remain in the administrator audit log. Model providers receive the conversation content needed for inference.

Managed installations generate a private `/opt/bananachat/config/app.env` with persistent data paths and the settings for the selected role. Apply environment changes with `sudo bananachat restart`. Repository credentials and the opt-in update policy are configured separately through `bananachat source` and `bananachat updates`; see [deployment](deployment.md#update-policy-and-private-repositories).

### Inference capacity and deadlines

The default is two HTTP processes, each with 16 threads (`BC_HTTP_THREADS`, 8–64). Each process admits at most its thread count minus four concurrent inference responses, preserving capacity for Stop, status and navigation. The shared inference queue separately defaults to 4 running jobs and 50 total jobs (`BC_MAX_CONCURRENT`, `BC_MAX_QUEUE_DEPTH`). A non-admin user can have one active chat and at most three API/playground requests, with one API request running at a time. Size the running-job limit for the actual inference hardware.

Generation defaults to a 300-second deadline (`BC_GENERATION_TIMEOUT`, 10–1800), a 30-second idle backend read deadline (`BC_INFERENCE_READ_TIMEOUT`, 5–60), and a 1 MiB response budget (`BC_CHAT_MAX_RESPONSE_KB`, 16–8192). The output-token cap is 8,192 (`BC_MAX_OUTPUT_TOKENS`, 128–65536). Chat context uses the most recent 100 messages within 300,000 characters; full saved history remains available. Stalled HTTP socket reads/writes time out after 30 seconds. A streaming client should reconnect to chat status after losing transport.

Chat responses that stop without provider usage preserve their partial text and label an output-token estimate in the stored usage. API estimates also include input text. These estimates use one token per four characters; they are not exact tokenizer counts.

Application and error logs rotate at 5 MiB, keeping five older files per log. Logging failures do not replace successful application actions with errors. Query diagnostics retain bounded buckets and redact SQL literals. Server journals have their own host retention policy.
