# Compute-side programs

Small programs that run on a compute server, next to Ollama or ComfyUI.
They use only the Python standard library, so they run on a machine that has
none of the web application's packages. [docs/compute.md](../docs/compute.md)
explains the deployments they belong to.

| Program | Started by | Purpose |
| --- | --- | --- |
| `inference_proxy.py` | `python -m compute.inference_proxy` (the `compute` role of `banana install`) | Authenticated streaming gateway in front of a loopback Ollama |
| `checkpoint_agent.py` | `python -m compute.checkpoint_agent` (its own systemd unit) | Verified Hugging Face checkpoint downloads for ComfyUI |
| `sandbox_runner.py` | `python -m compute.sandbox_runner` (its own systemd unit) | Locked-down containers for agents (Podman or Docker) |

## Inference gateway

The lifecycle tool installs and configures it; you normally never start it by
hand. Settings (environment variables):

| Variable | Default | Meaning |
| --- | --- | --- |
| `BC_COMPUTE_HOST`, `BC_COMPUTE_PORT` | `127.0.0.1`, `11435` | Listen address |
| `BC_COMPUTE_UPSTREAM` | `http://127.0.0.1:11434` | The Ollama it protects; loopback `http://` only (an existing Ollama chosen with `install --ollama-url`, or the managed one) |
| `BC_COMPUTE_TOKEN_FILE` | (required) | Bearer token: a regular file, mode 0600, at least 32 characters |
| `BANANA_MAINTENANCE_FILE` | | While this file exists, model requests answer 503 |
| `BC_SOURCE_URL` | the BananaChat repository | Corresponding source, offered at `/source` |
| `BC_COMPUTE_MAX_CONNECTIONS` | `32` | Requests served at once; more get a JSON 503 |
| `BC_COMPUTE_UPSTREAM_TIMEOUT` | `900` | Seconds to wait for Ollama between bytes |

- `GET /health` and `GET /healthz` need no token and answer 200 only while
  Ollama answers `/api/version` (the lifecycle manager checks `/healthz`).
- `GET /source` needs no token: JSON with the source URL, or a redirect for browsers.
- Everything else needs `Authorization: Bearer <token>` (the scheme is
  case-insensitive; the token is compared in constant time) and must be one of
  these, without a query string: `GET /api/tags`, `/api/ps`, `/api/version`,
  `/v1/models`; `POST /api/chat`, `/api/generate`, `/api/show`, `/api/pull`,
  `/api/create`, `/api/copy`, `/api/embed`, `/api/embeddings`,
  `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`;
  `DELETE /api/delete`. Other paths get 404, other methods 405.
- Request bodies need `Content-Length` and may be up to 32 MB; chunked uploads
  get 411. Responses stream through; every response closes its connection.
  Clients that stall for 30 seconds are disconnected.
- The token never reaches Ollama.

## Checkpoint agent

Downloads Hugging Face `.safetensors` checkpoints into ComfyUI's checkpoint
directory: one download at a time, a bounded queue, SHA-256 verification,
safetensors header validation and atomic no-clobber installation.

### Setup

Run the agent as the ComfyUI account (or one whose group ComfyUI can read).
The token file and the optional Hugging Face token file must be regular,
non-symlink files with mode 0600. Keep the state directory outside the
checkpoint tree.

```sh
install -d -o comfyui -g comfyui -m 0755 /srv/comfyui/models/checkpoints
install -d -o comfyui -g comfyui -m 0700 /var/lib/bananachat-checkpoint-agent
install -o comfyui -g comfyui -m 0600 /dev/null /var/lib/bananachat-checkpoint-agent/api.token
openssl rand -hex 32 > /var/lib/bananachat-checkpoint-agent/api.token
```

Copy `checkpoint-agent.env.sample` to `/etc/bananachat/checkpoint-agent.env`,
adjust it, install `bananachat-checkpoint-agent.service` and start it. The unit
expects this `compute/` folder under `/opt/bananachat-checkpoint-agent/compute`
and a Python at `/opt/bananachat-checkpoint-agent/.venv/bin/python` (a plain
`python3 -m venv`; no packages are installed into it).

### API

`GET /healthz` needs no token. Every `/v1/` route needs
`Authorization: Bearer <token>`.

- `GET /v1/status`
- `POST /v1/downloads` (JSON below; 202 when queued, 200 for a repeated idempotency key)
- `GET /v1/downloads/{id}`
- `DELETE /v1/downloads/{id}` (cancel)
- `GET /v1/checkpoints`
- `DELETE /v1/checkpoints?name=vendor/model.safetensors` with `If-Match: <sha256>`

```json
{
  "source": {
    "type": "huggingface",
    "repo_id": "owner/repository",
    "filename": "weights/model.safetensors",
    "revision": "0123456789abcdef0123456789abcdef01234567"
  },
  "target_name": "vendor/model.safetensors",
  "expected_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "expected_size": 123456789,
  "idempotency_key": "deployment-2026-07-model"
}
```

`expected_sha256` is required: obtain it through a separately trusted path;
the agent never trusts HTTP metadata for integrity. Targets and source file
names may contain conservative relative subdirectories; absolute paths, dot or
hidden components, backslashes, symlinks, non-safetensors names, arbitrary URLs
and redirects outside Hugging Face's download hosts are rejected. A Hugging
Face token is sent only to `huggingface.co` and dropped on redirects.

### Behaviour

- **Queue.** One download runs; up to `BC_CHECKPOINT_AGENT_QUEUE_SIZE` wait,
  in arrival order. Cancelling a waiting job frees its place immediately.
- **State.** Jobs and managed files are stored atomically in `state.json`
  (format version 1). A job interrupted by a restart is queued again and its
  partial file replaced. Finished jobs are kept for 30 days and at most 500 of
  them, so the file stays well below its 8 MB limit; an idempotency key is
  remembered only while its job is kept. Records of installed checkpoints are
  never pruned.
- **Deletion** removes only checkpoints the agent installed, and only when
  `If-Match` and the file's content both match. The file is hashed without
  blocking the rest of the API, and deletion is refused (409) if the file
  changed meanwhile.
- **HTTP.** Every connection has a socket timeout
  (`BC_CHECKPOINT_AGENT_REQUEST_TIMEOUT`), at most 16 requests are handled at
  once (others get 503), and tokens are compared in constant time (a
  malformed header is a 401).
- A crash after installing a file but before saving the state leaves a valid
  file the agent does not manage and will never delete.

For a web server on another host, expose the agent through an HTTPS reverse
proxy and configure that URL in BananaChat. Loopback HTTP is accepted. A
direct Tailscale HTTP URL requires the web side's
`BC_CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE=1` and sends the bearer token
without TLS.

## Sandbox runner

The only component that talks to the container engine for
[agents](../docs/agents.md): it creates one disposable container per agent
task and runs the model's commands and file operations inside it, over an
authenticated JSON API on `127.0.0.1:11436`. Every limit is enforced here.

- Every sandbox: no network, read-only root, `/workspace` as a size-capped
  tmpfs, uid 1000, all capabilities dropped, `no-new-privileges`, default
  seccomp, memory (no swap), CPU, pids and file limits, no host mounts or
  devices, `--init`; verified with `inspect` after start.
- Refuses to start with a rootful engine unless `BC_SANDBOX_ALLOW_ROOTFUL=1`,
  with an engine that cannot enforce limits, or with an allowed image that is
  not already pulled (it never pulls).
- Commands time out and are killed with everything they started; output is
  capped; uploads are validated on the host and extracted inside the
  container; idle and old sandboxes are reaped; a restart removes all
  sandboxes.

Install, configuration (`BC_SANDBOX_*`), the HTTP API, a hardened systemd unit,
the Caddy snippet and the threat model are in [docs/agents.md](../docs/agents.md).
Tests: `tests/test_sandbox_runner.py` (fake engine) and
`tests/test_sandbox_runner_docker.py` (real Docker; skipped without it, set
`BC_SANDBOX_REQUIRE_DOCKER=1` to require it).
