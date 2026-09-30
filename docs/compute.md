# Compute servers

BananaChat can keep accounts and chats on one machine (the *web* server) and
run the models on another (the *compute* server). Two stdlib-only programs in
`compute/` run on compute machines:

- the **inference gateway** (`compute/inference_proxy.py`), which puts
  authentication in front of Ollama for the `compute` role; and
- the optional **checkpoint agent** (`compute/checkpoint_agent.py`), which
  installs verified image-model checkpoints for ComfyUI.

Neither needs the web application, its database or any Python package beyond
the standard library. Volunteer PCs are a different mechanism: see
[workers](workers.md).

## Split deployment with the inference gateway

Two servers are the default layout. Ollama has no authentication of its own,
so a compute server never exposes it.
`sudo ./banana install --mode compute` (see [deployment](deployment.md#two-servers-default))
runs Ollama on `127.0.0.1:11434` and the gateway on `127.0.0.1:11435`
(started as `python -m compute.inference_proxy`), generates a random token in
`/opt/bananachat/data/.compute-api-token` (mode 0600), can print a Caddy
configuration for HTTPS and prints a **pairing code** (`bcpair1.…`, the
gateway address and token; `sudo bananachat compute pairing-code` shows it
again). With `--ollama-url http://127.0.0.1:11434` it uses an Ollama that
already runs on the machine instead (`BC_COMPUTE_UPSTREAM`) and never
manages it. The web server is installed with `--pair` (or later
`sudo bananachat backend connect`), which tests the connection and sets
`BC_OLLAMA_URL` and `BC_OLLAMA_API_KEY`; BananaChat then sends the token as
`Authorization: Bearer …` on every Ollama request, and treats the compute
server as remote (outage probing, no local memory, disk or GPU checks).

```text
browser ──HTTPS──> web server (BananaChat) ──HTTPS + bearer token──> Caddy ──> gateway :11435 ──> Ollama :11434
                                                                     (compute server)
```

What the gateway does:

- `GET /healthz` (and `/health`) answers without a token: 200 only while
  Ollama answers, which is what the lifecycle manager waits for after an
  update; `GET /source` offers the corresponding source (AGPL).
- Everything else requires the token (constant-time comparison) and must be
  one of the Ollama/OpenAI paths the web server and API clients use: tags,
  ps, version, show, chat, generate, pull, delete, create, copy, embeddings
  and the OpenAI-compatible chat, completions, models and embeddings routes.
  Other paths get 404; wrong methods 405; query strings are refused.
- While the maintenance file exists (updates, restores), model requests get 503
  but health checks keep working.
- Bodies are limited to 32 MB with a `Content-Length` (chunked uploads are
  refused); answers stream through as Ollama produces them; idle clients are
  disconnected after 30 seconds; at most 32 requests run at once and the rest
  get a JSON 503 with `Retry-After`; every response closes its connection.
- The upstream must be a loopback `http://` address, and the token file must
  be private and at least 32 characters, or the gateway refuses to start.

All settings are listed in [compute/README.md](../compute/README.md). To
rotate the token, run `sudo bananachat compute rotate-token` on the compute
server (it restarts the gateway and prints a new pairing code), then
`sudo bananachat backend connect` on the web server with that code. The web
server keeps the token in its `config/app.env` (`BC_OLLAMA_API_KEY`), not in
a token file; `backend status` on the web server and `status` on the compute
server show the token's fingerprint to compare.

For an SSH tunnel instead of HTTPS, install the compute server without
`--domain` (its pairing code then names `http://127.0.0.1:11435`), forward
the gateway's loopback port to a loopback port on the web server and pair;
`--url http://127.0.0.1:<port>` selects another local port.

The compute server's operator can see prompts in Ollama's memory and logs;
the split protects the web server's database, not the conversations
themselves from whoever runs the compute machine.

## Checkpoint agent (image models)

When image generation uses ComfyUI (`BC_IMAGE_BACKEND=comfyui`), administrators
can install checkpoints from Hugging Face without shell access to the compute
machine. The agent downloads one file at a time, verifies its SHA-256 and
safetensors header, installs it atomically without overwriting anything, and
deletes only files it installed (and only when their content still matches).

Install it next to ComfyUI following [ComfyUI deployment](comfyui.md#2-install-the-checkpoint-agent)
and the API reference in [compute/README.md](../compute/README.md). In short:

1. copy the `compute/` folder to `/opt/bananachat-checkpoint-agent/compute`
   and create `/opt/bananachat-checkpoint-agent/.venv` with `python3 -m venv`
   (no packages are installed);
2. create the state directory (0700) and a random token file (0600) owned by
   the ComfyUI account;
3. install `checkpoint-agent.env.sample` as `/etc/bananachat/checkpoint-agent.env`
   and `bananachat-checkpoint-agent.service` as a systemd unit, then
   `systemctl enable --now bananachat-checkpoint-agent`;
4. on the web server set `BC_CHECKPOINT_AGENT_URL` and
   `BC_CHECKPOINT_AGENT_TOKEN_FILE`.

Keep the agent on loopback or a private network (Tailscale with ACLs) behind
HTTPS; a plain-HTTP Tailscale address needs the web server's explicit
`BC_CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE=1`.

Operational notes:

- The queue holds `BC_CHECKPOINT_AGENT_QUEUE_SIZE` waiting downloads behind
  the running one; cancelling a waiting download frees its place.
- `state.json` in the state directory records jobs and installed files. It
  keeps finished jobs for 30 days (at most 500), so it stays small; an
  idempotency key can be reused once its job has been pruned. Back up the state
  directory together with the checkpoint directory.
- The API serves at most 16 requests at once and disconnects clients idle for
  `BC_CHECKPOINT_AGENT_REQUEST_TIMEOUT` seconds.
- A download interrupted by a restart starts again from the beginning.
