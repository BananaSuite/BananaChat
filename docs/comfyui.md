# ComfyUI Image Generation

BananaChat uses Ollama for text and an optional ComfyUI server for image
generation. ComfyUI runs on the Linux/NVIDIA compute machine and BananaChat calls
it over a private Tailscale connection. The compute machine does not need the
BananaChat web application, database, session keys, or API-token database.

Image generation is disabled by default (`BC_IMAGE_BACKEND=disabled`). A
chat-only installation needs none of the components in this guide.

## Architecture

```text
Browser or API client
        |
        v
BananaChat VPS
  Flask, SQLite, permissions, token limits, inference queue
        |
        | Tailscale only
        +--------------------+
        |                    |
        v                    v
ComfyUI :8188       Checkpoint agent :8765
  generation          verified HF downloads
        |                    |
        +---------+----------+
                  v
       /srv/comfyui/models/checkpoints
```

ComfyUI and the checkpoint agent must never be exposed through a public IP,
Cloudflare Tunnel, Tor, or a public nginx virtual host. Anyone who can reach the
ComfyUI API can consume GPU resources. Use both Tailscale ACLs and a host
firewall.

The compute-machine operator can inspect prompts and generated images. This
topology protects the VPS database and web source but does not make an unowned
compute machine private.

## How BananaChat Uses ComfyUI

BananaChat (`bananachat/services/comfyui.py`) talks to the standard ComfyUI
HTTP API. It never uses proxy environment variables and never follows
redirects; every request has a connect, read and total deadline and every
response a size limit (8 MB for JSON, 30 MB for an image).

**Checkpoint discovery.** `GET /object_info/CheckpointLoaderSimple` lists the
checkpoint files. A background job reconciles them with the model catalog
every ten minutes while image generation is enabled, and administrators can
sync immediately with **Sync models** in Admin -> Models. New checkpoints are
added as image models named `comfyui:<checkpoint file>`, **not rolled out**;
checkpoints that disappeared are marked unavailable (their catalog entries,
policies and edits are kept). If ComfyUI cannot be reached the catalog is left
as it was. The outcome of the last sync is kept for the administrator status
view.

**The workflow.** Users never send workflows. For each image BananaChat submits
one fixed seven-node workflow to `POST /prompt`:

| Node | Class | Settings |
|---|---|---|
| 1 | `CheckpointLoaderSimple` | the model's checkpoint |
| 2 | `CLIPTextEncode` | the prompt |
| 3 | `CLIPTextEncode` | empty negative prompt |
| 4 | `EmptyLatentImage` | width, height, batch size 1 |
| 5 | `KSampler` | random seed; steps, CFG, sampler and scheduler from `BC_COMFYUI_STEPS`, `BC_COMFYUI_CFG`, `BC_COMFYUI_SAMPLER`, `BC_COMFYUI_SCHEDULER`; denoise 1.0 |
| 6 | `VAEDecode` | |
| 7 | `PreviewImage` | temporary output |

**Generation.** BananaChat polls `GET /history/<prompt id>` every
`BC_COMFYUI_POLL_INTERVAL` seconds (and `GET /queue` to report whether the
prompt is waiting or running), validates the output reference (a plain file
name of type `temp`/`output`, no path traversal), downloads the image with
`GET /view`, checks that it really is a PNG, JPEG or WebP file, and finally
deletes the prompt's history entry (`POST /history {"delete": [id]}`).

**Cancellation.** When an image is not ready within
`BC_COMFYUI_GENERATION_TIMEOUT`, when ComfyUI reports an error, or when the
browser leaves the Images page while the image is being made, BananaChat stops
its prompt: if `GET /queue` shows it as the running prompt it calls
`POST /interrupt` (with the prompt id, which recent ComfyUI versions use to
interrupt only that prompt); if it is still waiting it removes it with
`POST /queue {"delete": [id]}`. Other users' prompts are never interrupted.
The history entry is deleted in every case.

**Queue and tokens.** Image requests wait in the same inference queue as
text requests (one running and up to three waiting per account, at most
`BC_COMFYUI_QUEUE_TIMEOUT` seconds). The fixed charge
(`BC_IMAGE_TOKENS_PER_GENERATION` tokens × the image model's weight, counted
against the API pool; image models can also have limits of their own, Admin →
Limits → Models) is reserved before the request enters the
queue and refreshed every `BC_IMAGE_CREDIT_RESERVATION_HEARTBEAT` seconds while
it runs. When the image is delivered the reservation becomes a ledger charge in
the same transaction as the metrics row - even if the reservation had expired
meanwhile. On any failure (ComfyUI error, timeout, cancelled or abandoned
request, unexpected server error) the reservation is released, so nothing is
charged. Reservations older than `BC_IMAGE_CREDIT_RESERVATION_TTL` are cleaned
up automatically.

**Access.** Image models follow the same rules as text models on the API
surface: they must be rolled out and allowed by the *image generation*
capability policy, their categories (scoped to the API or both) and their own
policy. The Images page and `POST /v1/images/generations` (see
[api.md](api.md)) share one implementation, limits
(`BC_IMAGE_GENERATION_RPM` per account and minute, administrators exempt) and
price.

## Supported Models

The built-in workflow supports standard all-in-one SD 1.x and SDXL
`.safetensors` checkpoints that provide MODEL, CLIP, and VAE through ComfyUI's
`CheckpointLoaderSimple` node. Flux, SD3, split-component models, custom nodes,
LoRA workflows, ControlNet, and arbitrary uploaded workflows are not supported.

For a 10 GB RTX 3080, begin with a standard SDXL checkpoint known to fit the
machine. Test at `512x512` before moving to `1024x1024`. Check the model license
and obtain its SHA-256 from a trusted source before installing it.

## 1. Install ComfyUI

Install a pinned stable ComfyUI release under a dedicated account. Select the
CUDA-enabled PyTorch build compatible with the NVIDIA driver on the compute
host rather than copying a wheel command blindly.

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin comfyui
sudo install -d -o comfyui -g comfyui -m 0755 /opt/ComfyUI
sudo install -d -o comfyui -g comfyui -m 0755 /srv/comfyui/models/checkpoints
sudo install -d -o comfyui -g comfyui -m 0700 /srv/comfyui/temp
```

Clone and pin ComfyUI, create its virtual environment, install the appropriate
CUDA PyTorch packages, and install ComfyUI's pinned requirements. Verify
`nvidia-smi` and `GET /system_stats` before continuing.

Configure `/opt/ComfyUI/extra_model_paths.yaml`:

```yaml
bananachat:
  base_path: /srv/comfyui
  checkpoints: models/checkpoints
```

Run ComfyUI with core nodes only and bind it to the compute node's Tailscale
address:

```bash
sudo -u comfyui /opt/ComfyUI/.venv/bin/python /opt/ComfyUI/main.py \
  --listen 100.x.y.z \
  --port 8188 \
  --disable-all-custom-nodes \
  --disable-api-nodes \
  --temp-directory /srv/comfyui/temp
```

Create a hardened systemd unit appropriate for the local ComfyUI installation
and pin the tested ComfyUI version. Generated images use `PreviewImage` and are
temporary. Configure tmpfiles or a timer to remove files from
`/srv/comfyui/temp` after one hour.

## 2. Install The Checkpoint Agent

Copy only the small `compute/` package to the compute machine:

```bash
sudo install -d -o root -g root -m 0755 /opt/bananachat-checkpoint-agent
sudo cp -R compute /opt/bananachat-checkpoint-agent/compute
sudo python3 -m venv /opt/bananachat-checkpoint-agent/.venv
sudo install -d -o comfyui -g comfyui -m 0700 /var/lib/bananachat-checkpoint-agent
sudo install -d -o root -g root -m 0755 /etc/bananachat
sudo install -o comfyui -g comfyui -m 0600 /dev/null \
  /var/lib/bananachat-checkpoint-agent/api.token
sudo sh -c 'openssl rand -hex 32 > /var/lib/bananachat-checkpoint-agent/api.token'
sudo chown comfyui:comfyui /var/lib/bananachat-checkpoint-agent/api.token
sudo chmod 0600 /var/lib/bananachat-checkpoint-agent/api.token
```

Install `compute/checkpoint-agent.env.sample` as
`/etc/bananachat/checkpoint-agent.env`. Set the listen address to the compute
node's Tailscale IP and keep the checkpoint root aligned with ComfyUI:

```ini
BC_CHECKPOINT_AGENT_HOST=100.x.y.z
BC_CHECKPOINT_AGENT_PORT=8765
BC_CHECKPOINT_ROOT=/srv/comfyui/models/checkpoints
BC_CHECKPOINT_AGENT_STATE_DIR=/var/lib/bananachat-checkpoint-agent
BC_CHECKPOINT_AGENT_TOKEN_FILE=/var/lib/bananachat-checkpoint-agent/api.token
```

Install and start the supplied service:

```bash
sudo cp compute/bananachat-checkpoint-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bananachat-checkpoint-agent
curl http://100.x.y.z:8765/healthz
```

For gated Hugging Face repositories, create a separate mode-0600 read-only
fine-grained token file and configure
`BC_CHECKPOINT_AGENT_HF_TOKEN_FILE`. Never place that token in the BananaChat
database or admin form.

## 3. Restrict The Network

Allow only the BananaChat VPS Tailscale identity to reach compute ports 8188 and
8765. A tailnet without explicit ACLs may allow every peer.

Example policy shape:

```json
{
  "tagOwners": {
    "tag:banana-web": ["autogroup:admin"],
    "tag:banana-compute": ["autogroup:admin"]
  },
  "acls": [{
    "action": "accept",
    "src": ["tag:banana-web"],
    "dst": ["tag:banana-compute:8188", "tag:banana-compute:8765"]
  }]
}
```

Also use the compute host firewall to permit both ports only on `tailscale0`.

The checkpoint agent requires HTTPS for a non-loopback URL by default. Direct
Tailscale HTTP remains encrypted by Tailscale but has no application-layer TLS.
To use it, the VPS must explicitly set:

```ini
BC_CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE=1
```

Prefer an authenticated HTTPS reverse proxy reachable only through Tailscale
when feasible.

## 4. Configure The BananaChat VPS

Copy the agent API token to a separate root-owned file readable by the
BananaChat service account:

```bash
sudo install -o bananachat -g bananachat -m 0600 /dev/stdin \
  /opt/bananachat/instance/checkpoint-agent.token
```

Add these settings to the BananaChat service environment:

```ini
BC_IMAGE_BACKEND=comfyui
BC_COMFYUI_URL=http://100.x.y.z:8188
BC_CHECKPOINT_AGENT_URL=http://100.x.y.z:8765
BC_CHECKPOINT_AGENT_TOKEN_FILE=/opt/bananachat/instance/checkpoint-agent.token
BC_CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE=1
BC_IMAGE_GENERATION_RPM=6
BC_IMAGE_TOKENS_PER_GENERATION=5000
```

Restart BananaChat and verify both private services from the VPS. The ComfyUI API
does not use a bearer token, so Tailscale ACLs and firewall rules are mandatory.
The bundled Gunicorn configuration derives one end-to-end timeout from the
ComfyUI queue, generation, network, and cleanup budgets. Generated nginx
configurations allow up to 3600 seconds, and update mode reconciles older
generated nginx configurations. External proxies such as Cloudflare may impose
a shorter limit of their own.

## 5. Install And Roll Out A Checkpoint

Open **Admin -> Models**. The existing Ollama pull form remains unchanged for
text models. The separate ComfyUI checkpoint form requires:

- Hugging Face repository in `owner/repo` format.
- Source `.safetensors` filename or relative path.
- Immutable commit SHA or reviewed tag.
- Target path under ComfyUI's checkpoint directory.
- Trusted 64-character SHA-256 digest.
- Optional expected byte size.

The compute agent downloads to a temporary file, enforces size and free-space
limits, verifies SHA-256 and safetensors structure, and installs atomically.
BananaChat then confirms that ComfyUI can discover the checkpoint before marking
the pull complete. Checkpoints copied into the folder by hand appear after the
next automatic sync (every ten minutes) or after **Sync models** in Admin -> Models.

New checkpoints are restricted and flagged as image models automatically. Use
**Edit** for display metadata/categories, **Roll Out** to expose the model, and
**Admin -> Access Policies** for image, category, and per-model permissions.

## Configuration Reference

| Variable | Default | Meaning |
|---|---|---|
| `BC_IMAGE_BACKEND` | `disabled` | `comfyui` enables image generation. |
| `BC_COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI base URL (no credentials in the URL). |
| `BC_COMFYUI_TIMEOUT` | `30` | Seconds allowed for each ComfyUI request (1-60). |
| `BC_COMFYUI_QUEUE_TIMEOUT` | `300` | Longest wait in BananaChat's inference queue. |
| `BC_COMFYUI_GENERATION_TIMEOUT` | `600` | Longest time from submission to a finished image. |
| `BC_COMFYUI_POLL_INTERVAL` | `1` | Seconds between history polls. |
| `BC_COMFYUI_STEPS`, `BC_COMFYUI_CFG` | `20`, `7` | Sampler steps and CFG scale. |
| `BC_COMFYUI_SAMPLER`, `BC_COMFYUI_SCHEDULER` | `euler`, `normal` | ComfyUI sampler and scheduler names. |
| `BC_IMAGE_GENERATION_RPM` | `6` | Images per account and minute (`0`: no limit). |
| `BC_IMAGE_TOKENS_PER_GENERATION` | `5000` | Tokens charged per image (`BC_IMAGE_CREDITS_PER_GENERATION` × 1,000 when only that setting of the previous release is set). |
| `BC_IMAGE_CREDIT_RESERVATION_TTL` | `1200` | Lifetime of an unrefreshed reservation (at least the generation timeout + 360 s). |
| `BC_IMAGE_CREDIT_RESERVATION_HEARTBEAT` | `30` | How often a running request refreshes its reservation. |

## Optional And Failure Behavior

With `BC_IMAGE_BACKEND=disabled` or unset:

- Chat, Ollama, API tokens, and text workers operate normally.
- No ComfyUI requests are made and no checkpoint sync runs.
- The Images navigation link is hidden and `/images` returns 404.
- `POST /v1/images/generations` returns 404 with `code: "images_disabled"`.
- Existing image policies and catalog records remain stored.

With ComfyUI enabled but no installed or rolled-out checkpoint:

- Text chat remains normal.
- The Images page says that no image model is available.
- Image API requests fail with 503 (`model_unavailable`) before any token is
  reserved.

With ComfyUI enabled but unreachable:

- The periodic sync logs the problem and leaves the catalog untouched.
- Image requests fail with 502 and are not charged.
- ComfyUI outages do not trigger BananaChat's AI-server outage notice and do
  not affect text inference.

With ComfyUI enabled but no checkpoint agent:

- Manually installed checkpoints can still be synchronized and used.
- The admin checkpoint pull form is hidden.

During maintenance mode or an outage of the text inference server, starting
new images is paused like any other generation (HTTP 503 with
`code: "maintenance"` or `"outage"`), see [api.md](api.md#service-status).

## Backup And Removal

BananaChat database backups include catalog metadata, access policies, pull
history, and usage. They do not include Ollama weights, ComfyUI checkpoints,
agent tokens, or generated images (BananaChat never stores generated images).

Removing a ComfyUI model from the BananaChat catalog does not delete its
checkpoint, and the next sync adds it again (not rolled out) while the file
exists. Use the managed agent API with its digest precondition or remove the
file deliberately on the compute host, then synchronize again.
