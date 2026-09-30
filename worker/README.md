# BananaChat worker

The worker lets a BananaChat server use the Ollama on your computer — a gaming
PC, a workstation, a Mac — to answer chat and API requests. It runs quietly in
the background, steps aside while you use the machine and stops taking work
while you play games.

**Before you start, know this:** the requests your computer answers include the
users' messages (and any images they attached). Whoever controls this computer
could read them. Only run a worker for a server whose operator and users are
fine with that, and keep the computer's accounts secure.

## What you need

- Python 3.9 or newer (nothing else to install). Optionally
  `pip install nvidia-ml-py psutil` for faster GPU readings and for lowering the
  priority of the Ollama process.
- [Ollama](https://ollama.com/download) with the models the server wants,
  under the same names (for example `ollama pull qwen3:4b`).
- A worker token from the server's administrator (Admin → Workers → Register).
- This `worker` folder, copied anywhere on the computer.

## Install

```sh
python bananachat_worker.py install
```

This creates the settings file and a background service for your account:

| | Settings file | Service | Logs |
| --- | --- | --- | --- |
| Linux | `~/.config/bananachat-worker.env` | systemd user service `bananachat-worker` | `journalctl --user -u bananachat-worker` |
| macOS (Intel and Apple Silicon) | `~/.config/bananachat-worker.env` | LaunchAgent `com.bananasuite.bananachat.worker` | `~/Library/Logs/bananachat-worker.log` |
| Windows | `%APPDATA%\BananaChat\bananachat-worker.env` | Task Scheduler task `BananaChatWorker` at logon | `bananachat-worker.log` beside the settings file |

Open the settings file and fill in the first two lines:

```ini
BC_SERVER_URL=https://chat.example.org
BC_WORKER_TOKEN=bcw_...the token you were given...
BC_WORKER_NAME=Anna's gaming PC
```

Then start it:

```sh
python bananachat_worker.py start
python bananachat_worker.py status
```

The administrator's Workers page shows the computer as *online* within a few
seconds. `python bananachat_worker.py config` prints the settings in use, the
idle and GPU readings and the models Ollama offers — the first thing to run
when something does not work. `python bananachat_worker.py run` runs the
worker in the foreground instead (Ctrl+C stops it).

The settings file is created with mode 0600 (only you can read it). Values you
export in the environment win over the file; `BC_WORKER_ENV_FILE` points to a
different file.

On Linux and macOS, `install --system` (and `start|stop|status|uninstall
--system`) sets up a service that starts at boot instead of at login. Run it
from your normal account: it uses `sudo` for the service files, and the worker
still runs as you, never as root. On Windows, an old `BananaChatWorker` system
service from an early release must first be removed from an administrator
terminal (`sc.exe stop BananaChatWorker`, `sc.exe delete BananaChatWorker`).

To remove the worker: `python bananachat_worker.py uninstall` (the settings
file is kept; delete it yourself).

## How it shares your computer

Every few seconds the worker checks how long it has been since you touched the
keyboard or mouse and how busy an NVIDIA GPU is:

| State | When | What the worker does |
| --- | --- | --- |
| idle | no input for 5 minutes and the GPU is calm | runs requests at normal priority |
| light | no input for 30 seconds | runs requests at lower priority |
| active | you are using the computer | runs requests at lower priority |
| gaming | GPU at 70 % or more | takes no requests; a request it has not started answering goes back to the server for another worker |

A request that is already being answered finishes (at lower priority). While it
runs its own request the worker ignores the GPU load it causes itself, unless
you are at the keyboard. On a Mac there is no GPU reading, so the idle time
decides alone. Where no idle reading exists (a headless Linux box) the GPU
decides; with neither, the computer counts as idle.

On Linux and macOS, a process may lower its priority but not raise it again
without privileges, so after stepping aside the worker stays at the lower
priority until it restarts (it logs this once). Windows returns to normal on
its own.

If Ollama is not running, the worker starts `ollama serve` on the address of
`BC_OLLAMA_HOST` and stops it again after 10 minutes without a request, to give
the memory back. An Ollama you started yourself is never stopped. The first
request after a pause may need to load the model; the server allows for that
(3 minutes by default).

When the worker stops (shutdown, logout, `stop`), a request it has not started
answering goes back to the server; one in the middle of its answer is reported
as interrupted.

## Security

- The token is sent only to `BC_SERVER_URL`. That address must be `https://`;
  plain `http://` is accepted for `localhost` only, or on a network you
  secure yourself with `BC_WORKER_ALLOW_HTTP=1`.
- Redirects are never followed and proxy environment variables are ignored, so
  the token cannot be sent anywhere else. Answers from the server are size-limited.
- Keep the token out of shell history and chat messages. If it leaks, the
  administrator removes the worker and registers it again.

## All settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `BC_SERVER_URL` | (required) | The BananaChat server's address |
| `BC_WORKER_TOKEN` | (required) | The token from Admin → Workers |
| `BC_WORKER_NAME` | computer name | Name shown to administrators |
| `BC_WORKER_ALLOW_HTTP` | `0` | `1` allows `http://` to a non-local server |
| `BC_OLLAMA_HOST` | `http://127.0.0.1:11434` | Your Ollama |
| `BC_OLLAMA_BINARY` | `ollama` | The program started as `ollama serve` |
| `BC_OLLAMA_IDLE_TIMEOUT` | `600` | Seconds without a request before a started Ollama is stopped (0 = never) |
| `BC_OLLAMA_KEEP_ALIVE` | `0` | Seconds Ollama keeps a model loaded after a request |
| `BC_FIRST_TOKEN_TIMEOUT` | `180` | Seconds allowed for the first words (model loading); the server's value wins |
| `BC_INFERENCE_READ_TIMEOUT` | `30` | Longest pause allowed between words |
| `BC_GENERATION_TIMEOUT` | `300` | Longest answer; the server's value wins |
| `BC_IDLE_THRESHOLD` | `300` | Seconds without input that count as idle |
| `BC_LIGHT_THRESHOLD` | `30` | Seconds without input that count as light use |
| `BC_GPU_GAMING_THRESHOLD` | `70` | GPU % that counts as gaming |
| `BC_GPU_ACTIVE_THRESHOLD` | `50` | GPU % that counts as active when there is no idle reading |
| `BC_HEARTBEAT_INTERVAL` | `10` | Seconds between status reports (2–30) |
| `BC_ACTIVITY_CHECK_INTERVAL` | `5` | Seconds between activity readings |
| `BC_POLL_GAP` | `0.5` | Pause between requests for work |
| `BC_WORKER_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `BC_WORKER_LOG_FILE` | | Write a rotating log file here (set by the Windows installer) |
| `BC_WORKER_LOG_MAX_BYTES`, `BC_WORKER_LOG_BACKUPS` | `5242880`, `3` | Log rotation |
| `BC_WORKER_ENV_FILE` | see above | Settings file to read |

An invalid number falls back to its default with a warning in the log.

The Windows task and the macOS agent are tested against the files and commands
they produce, not on real Windows or Mac hardware; try a request on your
machine before relying on it unattended.
