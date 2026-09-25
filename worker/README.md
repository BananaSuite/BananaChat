# Optional personal worker

The personal worker contributes inference from a separate PC and adjusts activity around local use. Dedicated server deployments use the main `banana` command with `single`, `web`, or `compute` mode; see [server deployment](../docs/deployment.md).

The worker has one entrypoint:

```sh
python worker/bananachat_worker.py --help
```

## Supported platforms

| | Linux | macOS (Intel and Apple Silicon) | Windows |
| --- | --- | --- | --- |
| Per-user service | systemd user unit | launchd LaunchAgent | Task Scheduler job at login |
| Starts at boot | `install --system` | `install --system` | not offered |
| Settings file | `~/.config/bananachat-worker.env` | `~/.config/bananachat-worker.env` | `%APPDATA%\BananaChat\bananachat-worker.env` |
| Logs | journal | `~/Library/Logs/bananachat-worker.log` | file beside the settings file |
| Idle detection | xprintidle, GNOME Mutter, D-Bus screensaver | IOKit `HIDIdleTime` | `GetLastInputInfo` |
| GPU utilisation | pynvml or nvidia-smi | not available | pynvml or nvidia-smi |
| Returns to full speed on its own | needs `CAP_SYS_NICE` | needs privilege | yes |

The settings file is created by `install` with mode 0600 on every platform, and
anything already exported overrides it. `python worker/bananachat_worker.py config`
prints which of these apply on the machine in front of you, along with the
current idle and GPU readings.

Install its `worker/requirements.txt` dependencies in a Python environment, configure `BC_SERVER_URL` and a private `BC_WORKER_TOKEN` issued by the administrator's Workers page, and use the entrypoint's `run`, `install`, `start`, `stop`, `status`, and `uninstall` commands. Its help describes platform and service options. Keep worker credentials out of shell history and source control.

The old separate binary-builder/install/uninstall shell scripts have been retired. The Python source remains available under the project's AGPL-3.0-only license, with the same personal-worker behavior.

On Windows, installation creates a task for the current user at login with least privilege, running under `pythonw.exe` when it is present so no console window appears. It writes the settings file under `%APPDATA%\BananaChat` and points the worker at a log file beside it, because a Task Scheduler job has nowhere to send its output. Start, stop, status and uninstall use that same job. An older Windows system service must be removed before installing the user task; see [deployment](../docs/deployment.md#optional-desktop-worker). Native Windows and GPU operation still needs verification on the intended machine.

On Linux, `install --system`, `start --system`, `stop --system` and `status --system` operate on the boot-time service. Run installation from the worker's non-root account; it uses sudo for service management, and inference runs as that account. User services omit `--system`. Worker HTTP requests never follow redirects with credentials. Use HTTPS; on an independently secured private network, `BC_WORKER_ALLOW_HTTP=1` explicitly permits non-loopback HTTP.

On macOS, installation writes a LaunchAgent to `~/Library/LaunchAgents` that starts at login and logs to `~/Library/Logs/bananachat-worker.log`. `install --system` writes a LaunchDaemon to `/Library/LaunchDaemons` instead, which starts at boot; run that from the worker's own account, because the daemon is pinned to that account rather than left running as root. Start, stop and status use `launchctl kickstart`, `bootout` and `print`; older Intel Macs without `bootstrap` fall back to `load -w`. Both Intel and Apple Silicon are supported, and the agent is marked `ProcessType: Background` so the macOS scheduler already keeps it behind whatever you are doing.

Because launchd has no equivalent of systemd's `EnvironmentFile`, the worker reads `~/.config/bananachat-worker.env` itself, and the installer creates that file with mode 0600. The plist holds only its path, never the token. Set `BC_WORKER_ENV_FILE` to move it. Anything already exported wins over the file, so a systemd unit or a shell export still takes precedence.

## How it shares the machine

The worker watches what you are doing and gets out of the way. It reads GPU
utilisation through pynvml or `nvidia-smi`, and your idle time through
`GetLastInputInfo` on Windows, `HIDIdleTime` from IOKit on macOS, or
xprintidle, the D-Bus screensaver interface or GNOME Mutter on Linux. Wayland
and headless machines that expose none of those fall back to the GPU reading
alone, and a machine with no GPU telemetry at all is treated as idle.

A Mac has no GPU reading to fall back on. Apple Silicon exposes no supported
utilisation counter outside root-only `powermetrics`, and an Intel Mac runs
AMD or Intel parts that `nvidia-smi` knows nothing about. So on macOS the
gaming gate never fires and the idle reading carries the decision on its own:
the worker steps down to below-normal priority while you are at the keyboard
and returns to normal once you have been away for the idle threshold. The
Workers page still names the chip, read from `machdep.cpu.brand_string` on
Apple Silicon or `system_profiler` on Intel.

That gives four states. Above 70 percent GPU utilisation it assumes you are
gaming or rendering and does not start new work at all: it stops polling, and a
job it was about to claim goes back to the server for another worker or a later
retry. Below that, five minutes of no input counts as idle and inference runs
at normal priority; anything more recent drops both the worker and the Ollama
process it manages to below-normal priority. The thresholds are environment
variables (`BC_IDLE_THRESHOLD`, `BC_LIGHT_THRESHOLD`, `BC_GPU_GAMING_THRESHOLD`,
`BC_GPU_ACTIVE_THRESHOLD`) if your machine wants different ones.

A job already in flight is not interrupted when you come back to the keyboard.
It finishes at the lower priority, and the worker does not claim the next one.

On Linux, lowering a process's priority needs no privileges, but raising it
again needs `CAP_SYS_NICE`, which the generated service deliberately does not
have. So once the worker has stepped down for local activity it stays down
until it restarts, even after the machine goes idle again. Inference is slower
than it could be, never more intrusive, and the log says so once when it
happens. Give the unit `AmbientCapabilities=CAP_SYS_NICE` if you would rather
have the full behaviour. Windows has no such restriction and returns to normal
priority on its own.

Remote jobs are bounded by the server's queue, time and response limits. Token batches are sent at most every 100 ms or 4,096 characters, and cancellation is final. Failed or disconnected workers leave an explicit interrupted/failed response; they cannot complete an old job after its lease expires.
