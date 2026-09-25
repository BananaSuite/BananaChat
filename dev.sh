#!/usr/bin/env bash
# Local development server. For an Internet-facing installation see docs/deployment.md.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
app_host=127.0.0.1
app_port=8000
app_debug=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) app_host="${2:?--host requires an address}"; shift 2 ;;
        --port) app_port="${2:?--port requires a number}"; shift 2 ;;
        --debug) app_debug=1; shift ;;
        -h|--help) echo 'Usage: ./dev.sh [--host 127.0.0.1] [--port 8000] [--debug]'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done
if [[ "$app_debug" == 1 && "$app_host" != 127.0.0.1 && "$app_host" != localhost && "$app_host" != ::1 ]]; then
    echo 'The interactive debugger is only available on loopback.' >&2
    exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Python 3.12 or newer is required")'
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
export BC_HOST="$app_host" BC_PORT="$app_port" BC_ENV=development BC_PROXY_MODE=0 BC_DEV_DEBUG="$app_debug"
exec .venv/bin/python - <<'PY'
import os
import config
from app import app
from services.ollama import start_background_sync, stop_background_sync
host, port = os.environ['BC_HOST'], int(os.environ['BC_PORT'])
print(f'BananaChat: http://{host}:{port}', flush=True)
print(f'Initial setup token: {config.SETUP_TOKEN}', flush=True)
start_background_sync()
try:
    app.run(host=host, port=port, debug=os.environ['BC_DEV_DEBUG'] == '1', use_reloader=False)
finally:
    stop_background_sync()
PY
