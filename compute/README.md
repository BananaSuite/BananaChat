# BananaChat Checkpoint Agent

For the main split-server inference role, use `sudo ./banana install --mode compute` from the repository root. It manages Ollama, an authenticated streaming API, optional repository updates, backup, restore, and uninstall. See [deployment](../docs/deployment.md#two-servers). The optional ComfyUI checkpoint downloader described below is a separate component.

This standalone compute-side service downloads Hugging Face safetensors checkpoints into a ComfyUI checkpoint directory. It uses only the Python standard library, one worker, a bounded queue, persistent metadata, bearer authentication, and atomic no-clobber installation.

## Setup

Run the agent as the ComfyUI account (or an account whose file group ComfyUI can read), then create checkpoint/state directories and a random bearer token. The bearer token file and optional Hugging Face token file must be regular, non-symlink files with mode `0600`. Keep the state directory outside the publicly served checkpoint tree.

```sh
install -d -o comfyui -g comfyui -m 0755 /srv/comfyui/models/checkpoints
install -d -o comfyui -g comfyui -m 0700 /var/lib/bananachat-checkpoint-agent
install -o comfyui -g comfyui -m 0600 /dev/null /var/lib/bananachat-checkpoint-agent/api.token
openssl rand -hex 32 > /var/lib/bananachat-checkpoint-agent/api.token
chmod 0600 /var/lib/bananachat-checkpoint-agent/api.token
```

Copy `checkpoint-agent.env.sample` to `/etc/bananachat/checkpoint-agent.env`, adjust it, then run:

```sh
python -m compute.checkpoint_agent
```

The compute host only needs this `compute/` package, not the BananaChat web
application. The example service expects it under
`/opt/bananachat-checkpoint-agent/compute` with a virtual environment at
`/opt/bananachat-checkpoint-agent/.venv`.

See [`docs/comfyui.md`](../docs/comfyui.md) for the complete ComfyUI,
Tailscale, web-host, and systemd deployment procedure.

## API

`GET /healthz` is unauthenticated. Every `/v1/` route requires `Authorization: Bearer <token>`.

- `GET /v1/status`
- `POST /v1/downloads`
- `GET /v1/downloads/{id}`
- `DELETE /v1/downloads/{id}`
- `GET /v1/checkpoints`
- `DELETE /v1/checkpoints?name=models/example.safetensors` with `If-Match: <sha256>`

Example request:

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

The minimal implementation always requires `expected_sha256`. Obtain it through a separately trusted control-plane path; the agent does not trust redirect or ordinary HTTP metadata as an integrity assertion. `expected_size` is optional, but configured byte and free-disk limits always apply.

Targets and source filenames may contain conservative relative subdirectories. Absolute paths, dot components, hidden components, backslashes, symlink traversal, non-safetensors names, arbitrary URLs, and unallowlisted redirects are rejected. Hugging Face credentials are attached only to the initial `huggingface.co` request and stripped from redirects.

Jobs and managed-file records are atomically stored in `state.json`. An interrupted active job is queued again after restart; partial files are replaced on retry. A crash after file installation but before state persistence can leave a valid but deliberately unmanaged file, which the agent will never delete.

For a web server on another host, expose the agent through an HTTPS reverse
proxy and configure that HTTPS URL in BananaChat. Loopback HTTP is accepted. A
direct Tailscale HTTP URL requires the web-side
`BC_CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE=1` opt-in and sends bearer
credentials without application-layer TLS.
