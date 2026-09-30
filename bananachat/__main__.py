"""Development server: ``python -m bananachat [--host H] [--port P] [--debug]``.

For production use Gunicorn (see ``gunicorn.conf.py``).
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run BananaChat with the development server.")
    parser.add_argument("--host", default=os.environ.get("BC_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BC_PORT", "8000")))
    parser.add_argument("--debug", action="store_true", help="Enable the interactive debugger (loopback only).")
    args = parser.parse_args(argv)
    if args.debug and args.host not in ("127.0.0.1", "localhost", "::1"):
        print("The interactive debugger is only allowed on a loopback address.", file=sys.stderr)
        return 2
    os.environ.setdefault("BC_ENV", "development")

    from bananachat import create_app
    from bananachat.app import start_background_services
    from bananachat.services import background

    app = create_app()
    start_background_services(app)
    print(f"BananaChat is running on http://{args.host}:{args.port}", flush=True)
    try:
        app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False, threaded=True)
    finally:
        background.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
