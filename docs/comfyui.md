# ComfyUI Image Deployment

BananaChat uses Ollama for text and an optional ComfyUI server for image
generation. ComfyUI runs on the Linux/NVIDIA compute machine and BananaChat calls
it over a private Tailscale connection. The compute machine does not need the
BananaChat web application, database, session keys, or API-token database.

Image generation is disabled by default. A chat-only installation needs none
of the components in this guide.

## Architecture

```text
Browser or API client
        |
        v
BananaChat VPS
  Flask, SQLite, permissions, credits
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

## Supported Models

The built-in workflow supports standard all-in-one SD 1.x and SDXL
`.safetensors` checkpoints that provide MODEL, CLIP, and VAE through ComfyUI's
`CheckpointLoaderSimple` node. Flux, SD3, split-component models, custom nodes,
LoRA workflows, ControlNet, and arbitrary uploaded workflows are not supported
by this first implementation.

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
BC_IMAGE_CREDITS_PER_GENERATION=5
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
the pull complete.

New checkpoints are restricted and flagged as image models automatically. Use
**Edit** for display metadata/categories, **Roll Out** to expose the model, and
**Admin -> Access Policies** for image, category, and per-model permissions.

## Optional And Failure Behavior

With `BC_IMAGE_BACKEND=disabled` or unset:

- Chat, Ollama, API tokens, and text workers operate normally.
- No ComfyUI requests are made.
- Image navigation is hidden.
- Direct image API requests return HTTP 503.
- Existing image policies and catalog records remain stored.

With ComfyUI enabled but no installed or rolled-out checkpoint:

- Text chat remains normal.
- Image Lab shows that no models are available.
- Image API requests fail before credit reservation.

With ComfyUI enabled but no checkpoint agent:

- Manually installed checkpoints can still be synchronized and used.
- The admin checkpoint pull form is hidden.

ComfyUI outages do not activate BananaChat's global Ollama outage page and do not
affect text inference.

## Backup And Removal

BananaChat database backups include catalog metadata, access policies, pull
history, and credits. They do not include Ollama weights, ComfyUI checkpoints,
agent tokens, or generated images.

Removing a ComfyUI model from the BananaChat catalog does not delete its
checkpoint. Use the managed agent API with its digest precondition or remove
the file deliberately on the compute host, then synchronize ComfyUI again.
