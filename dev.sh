#!/usr/bin/env bash
# Local development server. For an Internet-facing installation see docs/deployment.md.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host|--port) args+=("$1" "${2:?$1 requires a value}"); shift 2 ;;
        --debug) args+=("--debug"); shift ;;
        -h|--help) echo 'Usage: ./dev.sh [--host 127.0.0.1] [--port 8000] [--debug]'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Python 3.12 or newer is required")'
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/python -m pip install -q --only-binary=:all: --no-deps --upgrade 'pip>=26.2.1'
.venv/bin/python -m pip install -q --only-binary=:all: -r requirements.txt
export BC_ENV="${BC_ENV:-development}" BC_PROXY_MODE="${BC_PROXY_MODE:-0}"
exec .venv/bin/python -m bananachat ${args[@]+"${args[@]}"}
