# Cloud sessions

Agents let a model work on a task in a disposable Linux container: it runs
commands, reads and writes files in `/workspace` and finishes with a summary.
Administrators turn the feature on; it is off by default. In **Admin → Cloud
sessions**, choose **Administrators only**, **Everyone signed in**, or
**Custom access lists**. Fresh installations select administrators only when enabled.
Existing explicitly configured installations keep their previous Agents capability policy
through the access-list mode until an administrator changes it. An invalid
stored audience disables the feature rather than granting broader access.

Sessions use the existing `/agents` routes and retain their history. Model
policies, five-hour token budgets, optional weekly limits and per-model rates
remain independent of the audience. Native Claude Code subscription profiles
serve chat; cloud sessions require an Ollama or external API model with tool
support. Configure the sandbox runner on the compute host before enabling
sessions. Provider credentials stay on the web server and are not copied into
workspaces.

```text
browser ──> web server (BananaChat)                              cluster (compute host)
            agent loop: model calls + tool calls ──HTTPS + token──> sandbox runner ──> Podman / Docker
                                                                     (compute/sandbox_runner.py)
```

The model never runs anything on the web server. Every tool call becomes a
request to the **sandbox runner**, a small standard-library program on the
cluster that is the only component allowed to talk to the container engine.
All sandbox limits are enforced by the runner, so a compromised web server
cannot loosen them.

This page has three parts:

1. [Runner API](#runner-api): the HTTP contract between the web server and the runner.
2. [Operator guide](#operator-guide-the-sandbox-runner): installing and running the runner on the cluster.
3. [Threat model](#threat-model): what is protected and what is not.

Using agents and the admin settings are described in
[Using agents](#using-agents) and [Administering agents](#administering-agents);
starting tasks from a public Git repository and downloading the changes as a
patch in [Git repositories](#git-repositories).

## Runner API

JSON over HTTP. Every `/v1/` request needs `Authorization: Bearer <token>`
(compared in constant time). Errors are always
`{"error": {"code": "...", "message": "..."}}`. Unknown JSON fields are
ignored; wrong types are a 400. Request bodies need a `Content-Length`
(chunked bodies get 411). Every response closes its connection.

**Differences from the original spec** (all additive or clarifications; the
web client in `bananachat/services/agents/runner.py` already follows them):

- `GET /v1/sandboxes` answers `{"sandboxes": [...]}` (not a bare list) and
  accepts `?session=<id>` as a filter; items also carry `expires_at`, `busy`
  and `limits`.
- New: `GET /v1/sandboxes/{id}` (one sandbox) and
  `POST /v1/sandboxes/{id}/interrupt` (kill the running command but keep the
  workspace).
- `exec` also returns `interrupted` and `sandbox_removed`; a timed-out
  command reports `exit_code` 124.
- Directory listings use the types `file`, `dir`, `symlink` and `other`, and
  the listing itself has `"type": "dir"`; files have `"type": "file"` and
  always an `encoding`. A file larger than 1 MB returns its first 1 MB with
  `"truncated": true` instead of an error.
- `PUT .../archive` accepts `?path=` (the target directory, default
  `/workspace`) and answers `{"path", "files", "directories", "skipped", "size"}`.
- Limits above the runner's caps are lowered to the caps (the response shows
  the limits actually applied); limits below the minimum are a 400.

### Endpoints

`GET /healthz`
: Without a token: `{"ok": true, "engine": "podman"}` (503 when the engine does
  not answer). With the token: also `rootless`, `runtime`, `sandboxes`, `max`,
  `images`, `network` and the default `limits`.

`POST /v1/sandboxes`
: Body `{"session": "<1-64 of A-Z a-z 0-9 _ ->", "image": optional, "memory_mb", "cpus", "workspace_mb": optional}`.
  201 `{"id", "session", "image", "limits", "created_at", "expires_at"}`.
  400 `invalid_session`, `image_not_allowed` (not in `BC_SANDBOX_IMAGES`),
  `invalid_limits`; 429 `capacity` when `BC_SANDBOX_MAX` sandboxes exist.
  `id` is 32 lowercase hex characters.

`GET /v1/sandboxes[?session=...]`
: `{"sandboxes": [{"id", "session", "image", "created_at", "last_used_at", "expires_at", "busy", "limits"}]}`.

`GET /v1/sandboxes/{id}`
: One of those objects, or 404 (410 `sandbox_gone` for one the runner removed recently: every
  route answers 410 for the last 4096 removed ids, so callers can tell a lost sandbox from a wrong id).

`DELETE /v1/sandboxes/{id}`
: 204, also when it no longer exists. Kills anything running in it.

`POST /v1/sandboxes/{id}/exec`
: Body `{"command": "...", "timeout": seconds, "cwd": "/workspace/..."}`.
  `command` is at most 64 KB and runs as `sh -c <command>` inside the
  container, with stdin closed. `timeout` defaults to min(120,
  `BC_SANDBOX_EXEC_TIMEOUT`) and is lowered to that cap. Answer:
  `{"exit_code", "stdout", "stderr", "truncated", "timed_out", "duration_ms", "interrupted", "sandbox_removed"}`.
  Each stream keeps `BC_SANDBOX_OUTPUT_KB` and ends with
  `[output truncated: N more bytes]` when cut; a command that writes more than
  16 MB to one stream is stopped. On timeout every process the command started
  (including ones that detached with `setsid` or `nohup`) is killed; if that is
  impossible (for example a fork bomb filled the process table) the sandbox is
  removed and `sandbox_removed` is true. 409 `busy` while another command runs
  in the same sandbox; 503 `busy` when `BC_SANDBOX_MAX_EXECS` engine
  operations are already running; 410 `sandbox_gone` when the sandbox died
  (for example the command ran `kill -9 -1`). Allow the HTTP request
  `timeout` + 60 seconds.

`POST /v1/sandboxes/{id}/interrupt`
: Kills every process in the sandbox except its idle main process; the running
  `exec` (if any) returns with `"interrupted": true`. `{"ok": true, "sandbox_removed": false}`.

`GET /v1/sandboxes/{id}/files?path=/workspace/x`
: A file: `{"path", "type": "file", "size", "content", "encoding": "utf-8"|"base64", "truncated"}`
  (at most 1 MB of content). A directory: `{"path", "type": "dir", "entries": [{"name", "type", "size"}], "truncated"}`
  (at most 1000 entries). 400 `invalid_path`, 403 `outside_workspace` (a
  symlink leads out of `/workspace`), 404 `not_found`, 400 `not_regular`
  (FIFOs, devices).

`PUT /v1/sandboxes/{id}/files?path=/workspace/x`
: Raw body, at most 10 MB. Creates parent directories. `{"path", "size"}`.
  409 `is_a_directory`/`not_a_directory`, 507 `write_failed` (workspace full).

`GET /v1/sandboxes/{id}/archive`
: `application/gzip` tar of `/workspace`, at most 50 MB (413 `too_large`).
  The archive holds whatever the agent created, symlinks included: treat it as
  untrusted and never extract it on the web server.

`PUT /v1/sandboxes/{id}/archive[?path=/workspace/dir]`
: A `.tar.gz`, `.tar` or `.zip` body of at most 50 MB (415 for anything else),
  extracted into `path` (created when missing). See [archive uploads](#archive-uploads).
  The web server uses `path` for repository imports (a scratch folder the
  import script then moves into place).

Paths may be absolute (`/workspace/...`) or relative to `/workspace`. They are
normalised; NUL and control characters, components over 255 bytes, paths over
4096 bytes and anything outside `/workspace` are refused before the engine is
called. Other error codes: 401 `unauthorized`, 429 `rate_limited` (too many
wrong tokens from one address; the right token still works), 404/405 for
unknown routes and methods, 411/413 for bodies, 502 `engine_error`, 504
`timeout`, 500 `internal` (details only in the runner's log).

## Operator guide: the sandbox runner

The runner is `compute/sandbox_runner.py`, started as
`python3 -m compute.sandbox_runner` from a checkout of the `compute/` folder.
It needs Python 3.12 or later and nothing but the standard library. It runs on
the cluster (never on the web server), listens on `127.0.0.1:11436` and is
exposed to the web server through HTTPS (Caddy) or an SSH tunnel, like the
[compute gateway](compute.md).

### Choosing an engine

The runner **refuses to start** with a rootful engine (rootful Docker, or
Podman run as root) unless `BC_SANDBOX_ALLOW_ROOTFUL=1` is set: with a rootful
engine a container escape is root on the cluster. In order of preference:

1. **Rootless Podman** (`BC_SANDBOX_ENGINE=podman`, the default). Containers
   run inside the runner account's user namespace: an escape lands in an
   unprivileged account. Add gVisor (`BC_SANDBOX_RUNTIME=runsc`) if your
   distribution supports it with rootless Podman.
2. **Docker with gVisor** (`BC_SANDBOX_ENGINE=docker`, `BC_SANDBOX_RUNTIME=runsc`,
   `BC_SANDBOX_ALLOW_ROOTFUL=1`). gVisor puts a user-space kernel between the
   sandbox and the host kernel, which stops most kernel exploits; the Docker
   daemon itself is still root. Membership of the `docker` group is equivalent
   to root, so the runner account is root-equivalent: give it nothing else.
3. **Rootless Docker** (`dockerd-rootless-setuptool.sh install`): similar to
   rootless Podman.

Plain rootful Docker with runc is for development machines only.

The engine must be able to enforce limits: on start the runner checks that the
engine reports memory, pids and CPU control (cgroup v2 with those controllers
delegated to the runner account for rootless engines) and refuses to start
otherwise. After creating each sandbox it inspects the container and removes it
if any restriction (read-only root, memory, pids, network, capabilities, user)
was not applied.

### Images

The runner **never pulls**: every image in `BC_SANDBOX_IMAGES` must already be
present (the runner refuses to start otherwise), so a request can never make
the cluster download anything. Pin images by digest if you can
(`registry/name@sha256:...`). The default is
`mirror.gcr.io/library/python:3.12-slim`:

```sh
podman pull mirror.gcr.io/library/python:3.12-slim
```

An image needs `sh`, GNU coreutils (`timeout`, `readlink -m`, `head -z`,
`stat -c`), GNU findutils (`find -printf`) and GNU tar with gzip, which every
Debian or Ubuntu based image has. With `python3` (3.7 or later, as in the
`python` images) file reads and writes walk the path with `O_NOFOLLOW` and
directory descriptors, so a symlink the agent swaps in mid-operation cannot
redirect them; without it they fall back to `readlink -m` followed by the open,
which leaves a small race (harmless for the host: the operation still runs as
uid 1000 inside the container). Agents run as uid 1000 with `HOME=/workspace`
and no network, so preinstall what they need.

#### The agent image (`compute/sandbox-image`)

The repository ships the recommended image: `mirror.gcr.io/library/python:3.12-slim`
plus Git, a C toolchain (`build-essential`), Node.js and npm, ripgrep, jq,
`patch`, `zip`/`unzip` and a few small utilities, with a system Git identity
("BananaChat agent") and user 1000. It is about **1.1 GB**. Build it on the
compute host, as the account that runs the engine (the runner account for
rootless Podman):

```sh
docker build -t bananachat-agent:1 compute/sandbox-image      # or: podman build -t localhost/bananachat-agent:1 compute/sandbox-image
# BC_SANDBOX_IMAGES=bananachat-agent:1,mirror.gcr.io/library/python:3.12-slim
```

Put it **first** in `BC_SANDBOX_IMAGES` (the first image is the one every
task uses) and restart the runner. **Git in the image** is what makes patch
export possible (see [Git repositories](#git-repositories)): without it,
repositories are still imported but people cannot download their changes as a
`.patch`; Admin → Agents says which case applies. Nothing in the image needs
root or network at run time; sandboxes still run read-only, as uid 1000, with no
network. To change the tools, edit the Dockerfile, build a new tag
(`bananachat-agent:2`), allow it and restart: tags let you roll back.

Why the runner never pulls: images are the only code the runner starts, so they
are chosen and updated by you, ahead of time. A request can never make the
compute host download anything, a registry outage or rate limit cannot break
running agents, and a moved tag cannot silently change what sandboxes run.

The first image in the list is the default.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `BC_SANDBOX_HOST`, `BC_SANDBOX_PORT` | `127.0.0.1`, `11436` | Listen address. Keep it on loopback; a warning is logged otherwise. |
| `BC_SANDBOX_TOKEN_FILE` | (required) | Bearer token file: regular file, mode 0600, at least 32 characters. Created with a random 256-bit token on first start if missing. |
| `BC_SANDBOX_ENGINE` | `podman` | `podman` or `docker`. |
| `BC_SANDBOX_ENGINE_BINARY` | found on `PATH` | Absolute path of the engine CLI. |
| `BC_SANDBOX_RUNTIME` | engine default | OCI runtime, e.g. `runsc` for gVisor (recommended). |
| `BC_SANDBOX_IMAGES` | `mirror.gcr.io/library/python:3.12-slim` | Comma-separated allowlist; the first is the default. Pulled by you, never by the runner. |
| `BC_SANDBOX_NETWORK` | `none` | `none`, or `bridge` to give sandboxes network access (see the warning below). |
| `BC_SANDBOX_MAX` | `4` | Sandboxes alive at once (1-64). |
| `BC_SANDBOX_MAX_EXECS` | `8` | Engine operations (commands, file and archive operations) at once. |
| `BC_SANDBOX_MEMORY_MB` | `1024` | Memory per sandbox, swap disabled. The workspace and `/tmp` live in memory and count towards it. |
| `BC_SANDBOX_CPUS` | `1.0` | CPUs per sandbox. |
| `BC_SANDBOX_PIDS` | `256` | Processes and threads per sandbox. |
| `BC_SANDBOX_WORKSPACE_MB` | `512` | Size of the `/workspace` tmpfs. |
| `BC_SANDBOX_EXEC_TIMEOUT` | `300` | Longest command, in seconds (5-3600). |
| `BC_SANDBOX_IDLE_TTL` | `1800` | Seconds without use before a sandbox is removed. |
| `BC_SANDBOX_MAX_AGE` | `21600` | Oldest a sandbox may get, in seconds, even while in use. |
| `BC_SANDBOX_OUTPUT_KB` | `64` | Output kept per stream and command. |
| `BC_SANDBOX_ALLOW_ROOTFUL` | `0` | `1` accepts a rootful engine (read [choosing an engine](#choosing-an-engine) first). |
| `BC_SANDBOX_INSTANCE` | `default` | Name stored in a container label. The runner only reaps containers carrying its own name, so two runners (or a test run) on one host leave each other alone. |
| `BC_SANDBOX_MAX_CONNECTIONS` | `32` | HTTP requests handled at once; more get a JSON 503. |
| `BC_SANDBOX_TRUSTED_PEERS` | `127.0.0.1,::1` | Addresses or networks the web server connects from (the local Caddy or SSH tunnel), or `none`. Other peers get at most `BC_SANDBOX_MAX_CONNECTIONS_PER_PEER` connections each and, together, never the last quarter of `BC_SANDBOX_MAX_CONNECTIONS`, which stays free for trusted peers. If the web server connects directly (for example over Tailscale), add its address. |
| `BC_SANDBOX_MAX_CONNECTIONS_PER_PEER` | `4` | Connections at once from one untrusted address. |

Callers can lower `memory_mb`, `cpus` and `workspace_mb` per sandbox, never
raise them. Size the host for `BC_SANDBOX_MAX` × `BC_SANDBOX_MEMORY_MB` of
memory and `BC_SANDBOX_MAX` × `BC_SANDBOX_CPUS` CPUs on top of Ollama.

> **Network warning.** `BC_SANDBOX_NETWORK=bridge` lets model-written code
> reach the internet and whatever the cluster can reach: your LAN, other
> services on the host, cloud metadata endpoints (`169.254.169.254`), Ollama if
> it listens beyond loopback. It enables data exfiltration and attacks on third
> parties from your address. The runner logs a `SECURITY` warning at every
> start. If you need package downloads, preinstall them in the image instead;
> if you must enable it, firewall the bridge (deny RFC 1918, link-local and the
> host) at the host level.

### Install with rootless Podman

As root, once:

```sh
useradd --system --create-home --home-dir /var/lib/bananachat-sandbox --shell /usr/sbin/nologin bcsandbox
usermod --add-subuids 200000-265535 --add-subgids 200000-265535 bcsandbox   # if not assigned automatically
loginctl enable-linger bcsandbox           # gives it /run/user/<uid> and a user systemd for cgroups
install -d -m 0755 /opt/bananachat-sandbox
cp -r compute /opt/bananachat-sandbox/     # from a BananaChat checkout
install -d -o bcsandbox -g bcsandbox -m 0700 /etc/bananachat/sandbox
```

Check that the account gets the memory, pids and cpu controllers (cgroup v2):

```sh
cat /sys/fs/cgroup/user.slice/user-$(id -u bcsandbox).slice/cgroup.controllers
# needs: cpu memory pids. If not, create /etc/systemd/system/user@.service.d/delegate.conf with
#   [Service]
#   Delegate=cpu cpuset io memory pids
# and run: systemctl daemon-reload
```

As `bcsandbox` (`sudo -u bcsandbox -i` or `machinectl shell bcsandbox@`), pull the
images:

```sh
podman pull mirror.gcr.io/library/python:3.12-slim
podman info --format '{{.Host.Security.Rootless}} {{.Host.CgroupControllers}}'   # true [cpu memory pids ...]
```

Cap all sandboxes together with the account's slice, so even many sandboxes
cannot starve Ollama:

```sh
systemctl set-property user-$(id -u bcsandbox).slice MemoryMax=6G TasksMax=2048 CPUQuota=400%
```

Create `/etc/bananachat/sandbox-runner.env` (mode 0640, root:bcsandbox):

```sh
BC_SANDBOX_TOKEN_FILE=/etc/bananachat/sandbox/runner.token
BC_SANDBOX_ENGINE=podman
BC_SANDBOX_IMAGES=mirror.gcr.io/library/python:3.12-slim
BC_SANDBOX_NETWORK=none
BC_SANDBOX_MAX=4
```

The token file is created on first start (mode 0600). To create it yourself:
`install -o bcsandbox -g bcsandbox -m 0600 /dev/null /etc/bananachat/sandbox/runner.token`
then `openssl rand -hex 32 > /etc/bananachat/sandbox/runner.token`.

### systemd unit

`/etc/systemd/system/bananachat-sandbox-runner.service` (replace `1001` with
`id -u bcsandbox`):

```ini
[Unit]
Description=BananaChat agent sandbox runner
Documentation=file:/opt/bananachat-sandbox/compute/README.md
After=network-online.target user@1001.service
Wants=network-online.target user@1001.service

[Service]
Type=simple
User=bcsandbox
Group=bcsandbox
WorkingDirectory=/opt/bananachat-sandbox
EnvironmentFile=/etc/bananachat/sandbox-runner.env
Environment=XDG_RUNTIME_DIR=/run/user/1001
ExecStart=/usr/bin/python3 -m compute.sandbox_runner
Restart=on-failure
RestartSec=5s
# Allow pending creation and container cleanup to finish before a forced stop.
# Increase this allowance if BC_SANDBOX_MAX exceeds the default of four.
TimeoutStopSec=420s
KillSignal=SIGTERM
UMask=0077

ProtectSystem=strict
ReadWritePaths=/var/lib/bananachat-sandbox /run/user/1001 /etc/bananachat/sandbox
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
LockPersonality=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
# If containers start from a shell as bcsandbox but not under this unit, remove the
# hardening lines above one at a time; distributions differ. Rootless Podman creates user namespaces and uses the setuid helpers newuidmap/newgidmap,
# so NoNewPrivileges, CapabilityBoundingSet=, RestrictNamespaces, PrivateUsers and
# @mount-restricting system call filters must stay off for this unit.

[Install]
WantedBy=multi-user.target
```

For **Docker** (with gVisor) use `SupplementaryGroups=docker`, drop the
`XDG_RUNTIME_DIR` line and the `user@` dependencies, set
`Environment=DOCKER_CONFIG=/var/lib/bananachat-sandbox/.docker`, and add the
stricter options that the Docker CLI tolerates:

```ini
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectHome=yes
PrivateDevices=yes
RestrictNamespaces=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources @mount @reboot @swap
ReadWritePaths=/var/lib/bananachat-sandbox /etc/bananachat/sandbox
```

gVisor for Docker: install `runsc` (see gvisor.dev), run `runsc install` and
restart Docker; `docker info` must list `runsc` under Runtimes.

Then `systemctl daemon-reload && systemctl enable --now bananachat-sandbox-runner`
and check `journalctl -u bananachat-sandbox-runner`: it states the engine, and
any `SECURITY:` warning (rootful engine, network enabled, running as root,
non-loopback listener) deserves attention.

### Exposing it to the web server

Keep the runner on loopback and put Caddy in front of it with a certificate,
allowing only the web server's address:

```caddy
sandbox.example.org {
    @web remote_ip 203.0.113.10          # the web server
    handle @web {
        request_body {
            max_size 60MB
        }
        reverse_proxy 127.0.0.1:11436 {
            transport http {
                read_timeout 10m
                write_timeout 10m
            }
        }
    }
    respond 403
}
```

Commands may run for `BC_SANDBOX_EXEC_TIMEOUT` seconds (plus a few for
clean-up) before the answer starts, so any proxy between the two machines must
allow that. Archive downloads, on the other hand, must be read at 1 MB/s on
average (after 15 s) and never stall for 10 s, or the runner drops them; the
web server fetches the whole archive before passing it to the browser.
Alternatively, an SSH tunnel from the web server:
`ssh -N -L 127.0.0.1:11436:127.0.0.1:11436 tunnel@cluster` (run it as a
systemd service with `Restart=always`) and `BC_AGENTS_RUNNER_URL=http://127.0.0.1:11436`.

### Pairing with the web server

Copy the token to the web server into a 0600 file readable by the BananaChat
account and set (see [configuration](configuration.md)):

```sh
BC_AGENTS_RUNNER_URL=https://sandbox.example.org
BC_AGENTS_RUNNER_TOKEN_FILE=/opt/bananachat/data/.agents-runner-token
```

Check from the web server:
`curl -H "Authorization: Bearer $(cat /opt/bananachat/data/.agents-runner-token)" https://sandbox.example.org/healthz`.
The admin page shows the same status and warns when the network is on or the
engine is rootful.

To rotate the token, write a new one on both machines and restart the runner
(this removes all sandboxes, so do it while no task runs).

### Operations

- **Restarts remove every sandbox.** On stop the runner deletes its containers;
  on start it removes any labelled container of its instance it does not know.
  Running tasks see their sandbox disappear (410/404): the agent gets a new, empty
  sandbox (at most twice per run) and is told its files are gone.
- **Reaper.** Every 15 seconds sandboxes idle longer than `BC_SANDBOX_IDLE_TTL`
  or older than `BC_SANDBOX_MAX_AGE` are removed, dead containers forgotten,
  unknown ones removed and failed removals retried (they keep counting
  against `BC_SANDBOX_MAX` until they succeed).
- **Emergency stop.** `systemctl stop bananachat-sandbox-runner` kills all
  agent code within seconds. As a last resort:
  `podman rm -f $(podman ps -aq --filter label=io.bananachat.sandbox=1)`.
- **Logs** (journald) record sandbox creation and removal and each command's
  exit code and duration, never command text, file contents or the token.
- **Updates.** Replace the `compute/` folder and restart. New image: pull it,
  add it to `BC_SANDBOX_IMAGES`, restart.

### Archive uploads

Uploads are never extracted on the host. The runner reads the archive in
memory, validates it completely, and only then streams a rewritten plain tar
into `tar -x --no-same-owner --no-same-permissions` inside the container:

- only regular files and directories are kept; symlinks, hard links, devices
  and FIFOs are skipped (and counted in `skipped`);
- names must be relative and stay inside the target: absolute names, `..`,
  backslashes, control characters and over-long names reject the whole
  archive;
- at most 10,000 entries, and the decompressed size may not exceed the
  sandbox's workspace size, checked both against the declared sizes and while
  actually decompressing (zip and gzip bombs stop early); oversized tar
  metadata headers are refused before they are read;
- encrypted zips and compression methods other than stored/deflate are
  refused; owners become uid 1000 and modes 0644/0755 (setuid bits dropped);
- at most two uploads or archive downloads are processed at once (file writes have a
  separate pool of four, so slow archive transfers cannot stall the agents' `write_file`).

A failure after validation (for example the workspace filled up) can leave
some files behind; the answer says so.

## Threat model

The runner exists to run code chosen by a language model, which may be
steered by anything it reads (prompt injection) or by a malicious user.
Assume **everything inside a sandbox is hostile**, and that the web server
may be compromised.

### Assets

The cluster host (and Ollama on it), the network the cluster can reach, other
users' sandboxes, the runner token, and the availability of the service.

### Controls

| Threat | Control | Proved by |
| --- | --- | --- |
| Escape to the host via container flags | Argument lists only (never a host shell); `--cap-drop ALL`, `no-new-privileges`, default seccomp, no `--privileged`, no host mounts, no devices, `--user 1000:1000`; inspection after start refuses containers where the engine ignored a flag | `test_every_sandbox_gets_every_hardening_flag`, `test_verification_rejects_an_engine_that_ignores_flags`, `test_non_root_without_capabilities_and_read_only_root` (docker) |
| Kernel exploit from inside | Rootless engine required unless explicitly allowed; gVisor recommended | `test_rootful_engine_is_refused_unless_explicitly_allowed` |
| Injection into engine commands | IDs generated by the runner (hex); session, image, runtime, instance matched with full-string patterns; images only from the allowlist; commands and paths passed as separate arguments to fixed scripts inside the container | `test_commands_reach_the_container_as_one_argument_never_through_a_host_shell`, `test_engine_passes_arguments_verbatim_without_a_shell`, `test_create_validates_session_image_and_limits`, `test_configuration_defaults_and_validation` |
| Network abuse (exfiltration, LAN, metadata) | `--network none` by default, loud warning otherwise | `test_there_is_no_network` (docker) |
| Tampering with the image | Read-only root; writable only `/workspace` (tmpfs, size-capped, uid 1000) and `/tmp` (tmpfs, 64 MB, noexec) | `test_non_root_without_capabilities_and_read_only_root`, `test_the_workspace_size_is_capped` (docker) |
| Path traversal / symlinks | Paths normalised under `/workspace` before anything runs; resolved again inside the container one component at a time with `O_NOFOLLOW` (python3 images; `readlink -m` otherwise) so symlinks cannot lead out, even swapped in mid-operation; FIFOs and devices refused | `test_paths_outside_the_workspace_or_malformed_are_refused`, `test_query_strings_are_strict`, `test_files_stay_inside_the_workspace` (docker), `test_the_python_file_helper_resolves_inside_the_workspace_only`, `test_swapped_symlinks_never_lead_out_of_the_workspace` (docker) |
| Memory, CPU, process exhaustion, fork bombs | `--memory` = `--memory-swap`, `--cpus`, `--pids-limit`, `nofile` and `core` ulimits, `--oom-score-adj 500`; engine must enforce limits or the runner refuses to start; kill-all uses only shell builtins, and a sandbox that cannot be cleaned is removed | `test_memory_limit_kills_the_hog_but_not_the_sandbox`, `test_pids_limit_and_fork_bombs` (docker), `test_engines_that_cannot_enforce_limits_are_refused`, `test_a_command_that_cannot_be_killed_takes_its_sandbox_with_it` |
| Runaway commands and detached processes | Exec timeout (capped) kills every process except the idle main one, including `setsid`/`nohup` children | `test_timeouts_kill_the_command_and_everything_it_started` (docker), `test_timeouts_are_capped_and_stop_the_command` |
| Huge output | Per-stream cap with a marker; floods stopped at 16 MB | `test_output_is_truncated_and_floods_are_stopped` (docker), `test_output_is_capped_with_a_marker`, `test_engine_timeouts_output_caps_and_stdin` |
| Zip/gzip bombs, malicious archives | See [archive uploads](#archive-uploads) | `test_gzip_bombs_stop_at_the_workspace_size`, `test_zip_bombs_are_refused_from_their_declared_sizes`, `test_oversized_metadata_headers_are_refused_before_being_read`, `test_archive_traversal_and_bad_names_are_refused`, `test_archives_are_rewritten_with_only_safe_entries`, `test_bad_archives_never_reach_the_container`, `test_archive_round_trip` (docker) |
| Too many sandboxes or operations | `BC_SANDBOX_MAX` (429), one command per sandbox (409), `BC_SANDBOX_MAX_EXECS` (503), two large transfers at once, idle and age reaper | `test_capacity_is_enforced`, `test_one_command_per_sandbox_and_a_global_cap`, `test_capacity_and_the_reaper` (docker), `test_reaper_removes_idle_expired_and_unknown_containers` |
| Orphaned containers | Label per instance; reconcile on start and in the reaper; removal on shutdown | `test_start_up_removes_leftover_sandboxes_of_this_instance_only`, `test_shutdown_removes_every_sandbox`, the docker `runner` fixture |
| Unauthenticated use, token guessing | Bearer token (≥ 32 chars, generated 256-bit, file must be 0600 and not a symlink), constant-time comparison, per-address limit on failures | `test_every_api_route_needs_the_token`, `test_repeated_bad_tokens_are_rate_limited_but_the_real_token_still_works`, `test_token_file_is_created_private_and_checked` |
| Slow or abusive HTTP clients | Socket timeouts, 10 s to send headers, 5 min per authenticated request (exec: its timeout + 90 s), body limits, no chunked bodies, bounded connections with JSON 503, a few connections per untrusted address and a reserve for trusted ones, archive downloads dropped when the reader stalls | `test_slow_clients_are_disconnected`, `test_bodies_are_limited_and_validated`, `test_excess_connections_get_a_json_503`, `test_one_untrusted_peer_cannot_take_every_connection`, `test_trusted_peers_are_not_capped_per_address_and_keep_a_reserve`, `test_stalled_archive_downloads_release_their_slot` |
| Engine not pulling surprise images | The runner never pulls; images must be present at start | `test_images_are_never_pulled_and_runtime_must_exist` |
| SSRF through repository imports (web server) | See [Git repositories: safety](#import-safety): archive URLs built from validated parts for allowlisted hosts, every resolved address must be public, connection to the checked address, TLS verified, redirects re-checked (3 at most), timeouts and a size cap while streaming | `tests/app/test_agents_git.py`: `test_repository_addresses_are_parsed_strictly`, `test_only_public_addresses_are_accepted`, `test_private_addresses_from_dns_are_refused_before_connecting`, `test_download_connects_to_the_checked_address_over_verified_tls`, `test_redirects_only_to_allowed_hosts_and_each_hop_is_checked`, `test_the_size_cap_is_enforced_while_streaming`, `test_timeouts_and_stop_end_the_download`, `test_the_proxy_is_only_used_when_the_operator_opts_in` |
| Repository content running outside a sandbox, or Git running what the agent configured | The web server never runs `git` or unpacks archives; import and export scripts run in the sandbox; export uses a fresh Git directory (objects borrowed read-only), a temporary index and no system/global configuration | `test_import_and_patch_export_end_to_end` (docker, agent image), `test_scripts_and_their_answers_are_strict` |

### What is not protected

- **A compromised web server can use every sandbox.** Anyone with the token
  can create sandboxes up to the caps, run anything in them, and read or
  delete any sandbox, whichever user it belongs to. Separating users is the
  web server's job; the runner only guarantees the limits and the isolation
  from the host. Protect the token like a password.
- **Shared kernel.** With runc (the default runtime) sandboxes share the host
  kernel; a kernel vulnerability reachable through the default seccomp profile
  could lead to an escape, landing in the runner account (rootless) or root
  (rootful). gVisor reduces this sharply. Keep the kernel patched.
- **Side channels and noisy neighbours.** CPU caches, timing, and I/O
  bandwidth are shared; one sandbox can slow others and Ollama within its
  caps. Use the slice-level caps above.
- **Everything inside a sandbox.** The agent can delete or corrupt its own
  workspace, produce misleading output, or plant malicious files (scripts,
  archives with symlinks, HTML) that a person later downloads. Treat
  downloaded workspaces as untrusted.
- **Network mode `bridge`** removes the network protection entirely (see the
  warning above).
- **Images.** A malicious or vulnerable image runs with the same restrictions,
  but you choose and update them; pin digests.
- **Plain HTTP.** The runner speaks HTTP; the token and all data are only
  protected in transit by the HTTPS proxy or SSH tunnel you put in front.
- **Durability.** Workspaces live in memory and disappear on removal, idle
  timeout, maximum age, restart or reboot.

### Tested environments

The unit tests use a recording fake engine; the integration tests
(`tests/test_sandbox_runner_docker.py`, run in CI by the `sandbox` job) use
real rootful Docker with runc. The Podman and gVisor code paths (flags,
`podman info` parsing) are covered by unit tests only; verify a new
installation with a quick agent task and `journalctl` before opening it to
users.

## Using agents

**Cloud sessions** (top bar) appears when an administrator has enabled the
feature and you belong to the selected audience. Administrators-only mode
restricts sessions to admins; everyone mode permits active signed-in accounts.
Access-list mode uses the *Agents* capability in **Admin → Access**, including
existing access requests and individual grants. Model access and usage limits
still apply in every mode.

### Starting a task

On `/agents`, describe the task (the goal, the files involved and how to check
the result), pick a model and press **Start session**. When an administrator
allows it, the task can start from a public Git repository
([Git repositories](#git-repositories)). You can add files or a
`.zip`/`.tar.gz` archive (up to `BC_AGENTS_MAX_UPLOAD_MB`, default 20 MB in
total): they are copied into `/workspace` of the task's sandbox, and archives
are extracted there, never on the web server. When swarms are enabled,
**Agent swarm** lets the agent split the work among sub-agents that run in
parallel.

Only models that can call tools are offered. New tasks are refused while the
site is in maintenance or no accessible model is available, when you already have
as many running tasks as allowed, when all sandboxes are busy, or when your
*agent* tokens (or the model's own limits) are used up (Account → Your limits).

### While it works

The task page shows a live timeline (it survives reloads and lost
connections): your messages, the agent's messages and reasoning, and every
tool call. Open a tool call to see its exact arguments and the output that was
given back to the model (long output is shortened to about 8 KB). For a swarm,
each sub-agent has a lane with its status, its steps and its report.

The agent's tools are `bash` (a shell command), `read_file`, `write_file`,
`edit_file` (replace one exact, unique piece of text), `list_files`,
`search` (grep) and `finish` (the final summary); the main agent of a swarm
also has `delegate`. Every call is checked before anything runs: unknown
tools or arguments, wrong types, arguments nested more than 32 levels deep and
paths outside `/workspace` are refused and the agent is told why.

- **Stop** ends the task at once; a command that is running is killed, and so is
  anything the agent left running in the background (`nohup`, `&`): whenever a
  run ends, for any reason, nothing it started keeps running. The
  workspace is kept for downloads (unless the command could not be killed, in
  which case the sandbox is removed).
- **Follow-up messages**: while the task runs, a message is read at the
  agent's next step. When it has ended, a message starts a new run of the same
  task, in the same workspace if it still exists, with the conversation so far.
- **Workspace**: browse the files, open text files, download a file or the
  whole workspace (`.tar.gz`, up to 50 MB, one download at a time), and upload more files.
  Tasks started from a Git repository also offer **Download changes (.patch)**
  (see [Git repositories](#git-repositories)). After a run
  ends the sandbox is kept for a while (30 minutes by default) and then
  removed; a follow-up creates a fresh one when needed.

### States

| State | Meaning |
| --- | --- |
| Queued | Starting, or waiting for a free sandbox. |
| Running | The agent is working. |
| Paused | Maintenance or an AI-server outage: the task waits and continues by itself (at most one hour; paused time does not count against the time limit). |
| Finished | The agent called `finish`; its summary is shown. |
| Limit reached | A hard limit was hit: steps, tokens or minutes per run, your agent tokens, or the model's own limits. Send a message to continue. |
| Stopped | You or an administrator stopped it. |
| Failed | The model or the sandbox kept failing (retries are limited), the sandbox service is unavailable, or your access was withdrawn. |
| Interrupted | The server restarted while the task ran, or it stayed paused too long. Send a message to continue. |

Everything you and the agent write, every command and its (shortened) output
is stored with the task and visible to administrators. Tasks are deleted
automatically after the retention period (30 days by default) or when you
delete them.

## Git repositories

A task can start from a **public** Git repository, like Claude Code on the web,
while sandboxes keep having no network: the web server downloads the source
archive and the runner unpacks it into the sandbox. When the image has Git,
the person can download everything the agent changed as a `.patch`.

### Starting from a repository

On `/agents`, open **Start from a Git repository**, paste the address
(`https://github.com/owner/repo`, `https://gitlab.com/group/sub/project`,
`https://codeberg.org/owner/repo`; `.git` at the end is fine) and optionally a
branch, tag or commit (empty means the default branch). Only allowed hosts work
(Admin → Agents → *Git repositories*), only public repositories (no
credentials are ever used), and only as large as the site's limit (50 MB of
compressed archive by default). Imports count against *Imports per user per
hour*; they cost no tokens (only model calls do).

What happens at the start of the task's first run, before the first model
call (each step appears in the timeline):

1. The web server downloads the archive (see [safety](#import-safety)) into a private
   temporary file under the instance folder.
2. The runner validates it and extracts it inside the sandbox into a scratch
   folder (the usual [archive rules](#archive-uploads): symlinks and special
   files are skipped, sizes are capped).
3. A fixed script *in the sandbox* moves the archive's single top-level folder
   to `/workspace/<repo>`, removes any `.git` the archive carried and, when the
   image has Git, runs `git init` and commits everything as
   "Imported owner/repo@ref".
4. The import is recorded as a task step (repository, folder, initial commit),
   and the agent is told where the repository is.

Errors end the run with a clear reason: the repository or ref was not found
(or is private: GitHub answers 404 for both), it needs a sign-in, it is larger
than the limit, the host is not allowed or resolves to a private address, the
server is unreachable, too slow, or sends something that is not an archive.
Follow-ups do not import again.

### Downloading the changes

**Download changes (.patch)** (workspace panel) appears for tasks imported with
Git while the workspace exists. The patch is `git diff --binary` between the
imported commit and the current working tree: the agent's commits, uncommitted
edits, deletions and new untracked files, but not files matched by the
repository's `.gitignore` (unless they were part of the import). Apply it to a
checkout of the same commit with `git apply --binary changes.patch` (it is a
plain diff, not a mail series). It is computed in the sandbox:

- in a scratch folder with a **fresh Git directory** that borrows the
  repository's objects read-only (`objects/info/alternates`) and a temporary
  index, so the agent's repository (refs, index, working tree) is never
  modified;
- with `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null` and
  `--no-ext-diff --no-textconv`, so nothing the agent wrote into `.git/config`,
  `.git/info/attributes`, `~/.gitconfig` or `~/.config/git` (fsmonitor, filters,
  diff drivers, hooks, pagers) can make Git run a program;
- split into 1 MB parts that the web server reads through the runner's file API
  and checks against the size and SHA-256 the script printed; at most 20 MB
  (larger changes: download the workspace).

It counts as the person's one download at a time, needs the *Agents* access,
and is served as an attachment (`text/x-diff`, `nosniff`, sandboxing CSP): it is
text the agent wrote, so review it before applying. The workspace archive
download is unchanged.

### Git settings

Admin → Agents → *Git repositories*:

| Setting | Default | Meaning |
| --- | --- | --- |
| Allow starting tasks from a Git repository | off | Off: the form field is hidden and imports are refused (also for tasks created before it was turned off). |
| Allowed hosts | `github.com`, `gitlab.com`, `codeberg.org` | One per line. Self-hosted servers need their kind: `gitlab:git.example.org` or `gitea:git.example.org` (Gitea and Forgejo). GitHub downloads come from `codeload.github.com`. At most 20. |
| Largest repository archive | 50 MB (1–50) | The compressed archive; the runner accepts at most 50 MB. The extracted size is bounded by the sandbox's workspace size. |
| Imports per user per hour | 10 (1–100) | Applies to everyone, administrators included. |

**Check the image for Git** starts a short-lived sandbox, runs `git --version`
and removes it; every import records the answer too. When Git is missing and
imports are on, the page shows a warning (imports still work; no patch export).

Changes are recorded in the audit log (`admin.agents.git`,
`admin.agents.image_check`).

### Import safety

The web server fetches from the internet on behalf of users, so the download
is built to be useless for server-side request forgery:

- **No user-controlled URL is fetched.** The address is parsed strictly (HTTPS
  only; no credentials, port, query, fragment, escapes or extra path parts;
  owner, repository and ref must match conservative patterns) and the archive
  URL is built from those parts: GitHub
  `https://codeload.github.com/<owner>/<repo>/tar.gz/<ref>`, GitLab
  `https://<host>/<namespace>/<repo>/-/archive/<ref>/<repo>-<ref>.tar.gz`,
  Gitea/Forgejo `https://<host>/<owner>/<repo>/archive/<ref>.tar.gz`.
- **Only public addresses.** The host name is resolved and *every* address must
  be globally routable: private, loopback, link-local (cloud metadata),
  shared (100.64/10), multicast, reserved, documentation and unspecified
  addresses are refused, as are IPv6 forms that embed IPv4 (mapped, 6to4,
  Teredo, NAT64). The connection goes to an address that was checked (no second
  resolution, so DNS rebinding cannot swap it), and TLS is verified for the
  host name with the system's CAs.
- **Redirects** are followed only to allowed hosts (and `codeload.github.com`),
  over HTTPS on port 443, at most three, each resolved and checked again. A
  redirect to a sign-in page means the repository is private.
- **Bounded:** 10 s to resolve and connect, 30 s without data, 120 s in total;
  the size cap is checked against `Content-Length` and again while streaming
  into a new mode-0600 file (the download stops as soon as it is exceeded). The
  body must be gzip; HTML answers are refused. Stop cancels a download at once.
- **Nothing runs on the web server:** no `git`, no unpacking. The runner
  validates and extracts the archive in the sandbox; Git runs only there.

**Network requirements.** The web server needs outbound HTTPS (443) to the
allowed hosts (and `codeload.github.com` for GitHub). If it must go through a
proxy, set `BC_AGENTS_GIT_PROXY=1` in the app's environment: then `HTTPS_PROXY`
(an `http://` proxy, optionally `http://user:password@host:port`) is used for
imports, and only then; by default the environment's proxy variables are
ignored. Even through the proxy, the host name is resolved and checked on the
web server and the proxy is asked to `CONNECT` to the checked address, so the
proxy cannot be used to reach internal addresses either (the web server must
therefore be able to resolve public names). IPv6-only networks that rely on
DNS64/NAT64 are not supported for imports.

## Administering agents

**Admin → Cloud sessions** (English only). Every change is recorded in the audit log
(`admin.agents.*`).

- **Sandbox runner**: the runner's `/healthz` (engine, rootless, runtime,
  network, sandboxes in use and the limit, images). The page warns in red
  when sandboxes have network access or the engine runs as root.
- **Running tasks** and **Stop all running tasks**: the kill switch. Every
  queued, running or paused task stops within a second or two, whichever
  process runs it; commands are killed and owners see that an administrator
  stopped the task. Turning the feature off also stops running tasks, at
  their next step, and blocks new ones.
- **Git repositories**: allowing tasks to start from a public repository, the
  allowed hosts and limits, and whether the image has Git (see
  [Git settings](#git-settings)).
- **Settings** (stored in the database; the feature is **off** until you enable it):

  | Setting | Default | Meaning |
  | --- | --- | --- |
  | Enable cloud sessions | off | Off: nobody can start sessions or send follow-ups; running work stops at its next safe point; history stays readable. |
  | Who can start sessions | Administrators only | Admins only, everyone signed in, or the existing Agents access lists. Previously configured sites retain access-list mode. |
  | Enable agent swarms | off | Allows the *Agent swarm* option. |
  | Steps per run | 40 (1–200) | Model calls per run, all agents of a swarm together (each call reserves its step first, so parallel sub-agents cannot exceed it). |
  | Minutes per run | 20 (1–240) | Wall time per run; checked before every tool call, and no command may run past it. |
  | Tokens per run | 300,000 | Prompt and answer tokens per run. |
  | Running tasks per user | 1 (1–10) | Administrators are limited by the site limit only. |
  | Running tasks on the site | 4 (1–64) | Also capped by the runner's `BC_SANDBOX_MAX`. |
  | Command timeout | 120 s | Longest single command (the runner's `BC_SANDBOX_EXEC_TIMEOUT` caps it too). |
  | Keep workspace | 30 min (0–1440) | How long a sandbox survives after a run, for downloads and follow-ups (the runner's idle TTL and max age still apply). |
  | Starts per user per hour | 10 | New tasks and follow-ups that start a run (including a message queued during a run that restarts the task when it ends). |
  | Keep tasks | 30 days | Ended tasks and their logs are deleted afterwards (their sandboxes too). |
  | Sub-agents per swarm run | 4 (1–8) | Sub-agents cannot start sub-agents. |
  | Sub-agents at the same time | 2 (1–4) | |

  Values outside the ranges are refused, and values read from the database
  are clamped again, so no stored value can exceed the hard maximums.
- **Models**: tool-calling support is read from Ollama's `/api/show`
  (`capabilities` contains `tools`) every 15 minutes while agents are enabled,
  or with **Check tool support**. External API models use their enrolled
  capabilities. Override per model (*Always*/*Never*). People
  also need access to the model in the chat (rollout, categories, policies).
- **All tasks**: every task of every user, filterable by state, with the full
  log (prompt, every model message and reasoning, tool call, arguments and
  result, tokens and timings). Administrators can stop or delete any task.

**Access-list mode**: Admin → Access → *Agents*. The capability starts as
*Only people on the allowlist* with access requests enabled; approve requests
there or add people to the allowlist (optionally until a date).

**Tokens and rates**: every model call is charged to the **agent** pool
(Admin → Limits; tokens × the model's weight) and to the model's own limits,
checked before a task starts and before every step; a task stops with *Limit
reached* when the owner runs out. Task starts also obey the agent pool's
request-rate rules, the model's own rate and *Starts per user per hour*.
Agents think at the reasoning effort the owner may use (medium, or less when
the owner has not unlocked medium for the model).
Agent model calls go through the normal inference queue with the lowest
priority (people waiting for a chat answer go first) and are never sent to
volunteer worker PCs.

Account status, the session audience, model access and token limits are checked
again after queue waits and on each provider attempt. Revoking access or
disabling sessions stops subsequent model and tool work. Fresh checks do not
charge the same model request rate twice. Tightened run budgets apply to active
sessions; raising a setting does not expand a run's original allowance.

### How the web server runs tasks

Each run is a background thread holding a lease on the task
(`agent_tasks.owner_token`/`heartbeat_at`, renewed every 5 seconds by the
process supervisor, like chat runs). Stop requests are a database flag, so any
process (or the kill switch) can stop any task. If a process dies, its tasks'
leases expire after 60 seconds and a background job marks them
*Interrupted*; nothing the dead process might still write is accepted. A
background job deletes sandboxes whose keep-time ended, and deletes runner
sandboxes belonging to our tasks that we no longer track. When a sandbox
disappears during a run (runner restart, TTL), the agent is told that the
workspace was reset and a new, empty sandbox is created (at most twice per
run). Model and runner errors are retried at most three times with backoff;
a command is only retried when the request never reached the runner, so
nothing runs twice.
