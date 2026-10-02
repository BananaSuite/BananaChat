# Remote workers

A *worker* is a volunteer computer — typically someone's gaming PC — that runs
the BananaChat worker daemon (`worker/` in this repository) and answers text
requests with its own Ollama. Workers connect out to the server, so they need
no open ports and work behind home routers.

Workers are off by default. They add capacity; they never replace the
server's own Ollama, which answers whenever no worker does.

Administrators can hide model-server offline notices in **Site settings →
Maintenance and announcements → Model-server status**. Users can also hide a
notice for 24 hours in their browser. These controls affect presentation only:
offline models stay unavailable and health reporting stays accurate. Hosted
Claude and external API models do not use the volunteer worker pool.

## Privacy: read this first

**A worker's owner can read every request sent to it**: the conversation
history, the new message and any attached images. The daemon is open source,
but nothing stops the owner of a machine from changing it or watching its
memory. Register only machines whose owners you would trust with your users'
conversations, and tell your users that some answers may be produced on
volunteer hardware (for example in your terms or on the sign-in page).

What the server does to limit exposure:

- Administrators' own requests always use the local server.
- A request whose payload is larger than 16 MB never goes to a worker.
- The prompt stored for a job is deleted the moment the job ends (completed,
  failed, stopped or timed out); relayed pieces of the answer are deleted as
  soon as they are delivered; the job record itself (model name, timings,
  token counts) is deleted after an hour.
- No-history chats are excluded from volunteer workers. The chat service marks
  them as `chat_incognito`; the inference core forwards that request type, and
  `services.remote.should_route()` rejects it. This restriction does not change
  administrator audit access or the processing terms of a configured cloud model.

## Turning workers on

Set `BC_WORKERS_ENABLED=1` in the server configuration and restart. Related
settings:

| Variable | Default | Meaning |
| --- | --- | --- |
| `BC_WORKERS_ENABLED` | `0` | Route requests to workers and serve the worker API |
| `BC_WORKER_CLAIM_TIMEOUT` | `20` | Seconds a request waits for a worker to pick it up before the local server answers |
| `BC_FIRST_TOKEN_TIMEOUT` | `180` | Seconds a worker may take to produce the first words (cold model load) |
| `BC_INFERENCE_READ_TIMEOUT` | `30` | A worker silent for `max(60, 2 ×` this`)` seconds loses its job |
| `BC_GENERATION_TIMEOUT` | `300` | Longest answer, as for the local server |
| `BC_CHAT_MAX_RESPONSE_KB` | `1024` | Largest answer a worker may send |

While workers are off, nothing is routed to them and the worker API answers
every request with HTTP 503 `{"error": "Remote workers are disabled on this
server."}`, which the daemon reports in its log. Registered workers and their
tokens are kept.

## Registering a worker

Open **Admin → Workers**, enter a name and choose *Register and create
token*. The token is shown once, in a dialog; copy it to the worker's owner
over a private channel. The server stores only its SHA-256 digest, so a lost
token cannot be recovered: remove the worker and register it again.

The page lists every worker with its status (online, busy = the owner is
gaming, offline = no heartbeat for 45 seconds, disabled), activity, GPU,
advertised models and last heartbeat, and refreshes every 15 seconds.
*Disable* stops sending it jobs (a job it is running is stopped and answered
locally if it had not produced text yet) and makes the worker API refuse it
with 403; *Enable* reverses that; *Remove* deletes the worker and invalidates
its token. Every action is recorded in the audit log
(`admin.workers.register|enable|disable|delete`).

## Installing the daemon

On the worker computer: install Ollama and pull the models the server offers
(same names), copy the `worker/` folder, then

```sh
python bananachat_worker.py install
# edit the settings file it names: BC_SERVER_URL=https://… and BC_WORKER_TOKEN=…
python bananachat_worker.py start
```

The daemon needs only Python 3.9+ on Linux, macOS or Windows. The
[worker README](../worker/README.md) explains the settings, the service on
each platform and how the worker yields to its owner.

## How requests are routed

For each model it tries, the inference core asks `remote.should_route(user,
model)`. A request goes to the worker pool only when:

1. `BC_WORKERS_ENABLED` is on;
2. the requester is not an administrator;
3. an online worker — fresh heartbeat, not disabled, not gaming, not running
   another job — advertises the model (`name` and `name:latest` count as the
   same), and there are more such idle workers than jobs already waiting for
   that model;
4. no worker failed to pick up that model in the last minute.

The request keeps its place in the server's queue while a worker handles it.
If no worker claims the job within `BC_WORKER_CLAIM_TIMEOUT` seconds, or the
worker fails, disconnects or loses its lease before producing any text, the
job is withdrawn and the **local Ollama answers the same model** instead —
users see a slower answer, not an error. Once text has been delivered, a
worker failure ends the answer with an error, exactly like a local failure.
Stop requests reach the worker on its next chunk (and through its heartbeat).

When the owner starts gaming before the first words, the worker hands the job
back (`fail` with `requeue`) and another worker — or, after the claim timeout,
the local server — takes it. Older daemons that silently keep a job while
gaming have it taken back when their heartbeat reports `busy`/`gaming`.

## Protocol

The daemon talks to `/worker/v1` with `Authorization: Bearer <token>`. The
protocol is unchanged since the first worker release, so older daemons keep
working; new fields are optional.

| Request | Body | Reply |
| --- | --- | --- |
| `POST /heartbeat` | `status` (online/busy/offline), `gpu_name`, `gpu_util`, `ollama_version`, `capabilities: {models}`, `activity_state`, optional `platform`, `job_id` | `{"ok": true, "job_stop": bool}` |
| `GET /jobs/poll?models=a,b` | | a job `{job_id, model, messages, options, priority, first_token_timeout, lease_seconds, generation_timeout}` or `204` |
| `POST /jobs/<id>/chunk` | `{seq, content, done}` | `{ok, stop}` |
| `POST /jobs/<id>/complete` | `{tokens_in, tokens_out, finish_reason}` | `{ok, stop}` |
| `POST /jobs/<id>/fail` | `{error}` or `{error: "deferred"}` / `{requeue: true}` | `{ok, stop}` |

- Polls wait up to 25 seconds for a job; each server process holds at most
  four waiting polls and answers further ones with 204 at once, so polls never
  occupy many server threads.
- A heartbeat renews the lease of the worker's running job. The first chunk
  may take `BC_FIRST_TOKEN_TIMEOUT` seconds; after that, a job without a
  chunk or heartbeat for `max(60, 2 × BC_INFERENCE_READ_TIMEOUT)` seconds
  times out.
- Chunks are accepted strictly in order and exactly once (a retry of an
  accepted chunk is acknowledged; a gap or a conflicting retry fails the job).
  Each is at most 16 KB. An answer that reaches `BC_CHAT_MAX_RESPONSE_KB` is
  cut there and ended with finish reason `length` (`stop: true`).
- `complete` is accepted only after the chunk with `done: true`.
- `stop: true` means the job ended on the server (the user pressed Stop, it
  timed out or the worker was disabled): stop generating and do not report it.
- Failed sign-ins are rate-limited per address (30 per 5 minutes).

A background job (`worker-jobs`, every minute) times out jobs nobody picked up
or whose worker went silent, erases any prompt left in a finished job and
deletes finished jobs after an hour.
