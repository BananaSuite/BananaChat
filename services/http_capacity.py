"""Reserve HTTP threads for cancellation, status and ordinary navigation."""

from functools import wraps
import threading

from flask import jsonify, make_response

import config

_guard = threading.Lock()
_active = 0
_limit = config.HTTP_THREADS - 4


def configure(threads):
    """Apply the actual Gunicorn worker size after fork, before accepting work."""
    global _limit
    _limit = max(1, threads - 4)


class _Permit:
    def __init__(self):
        self.closed = False

    def close(self):
        global _active
        with _guard:
            if not self.closed:
                self.closed = True
                _active -= 1


def limit_inference(function):
    """Reject excess long responses before starting work; release on every exit."""
    @wraps(function)
    def limited(*args, **kwargs):
        global _active
        with _guard:
            if _active >= _limit:
                return jsonify({"error": {"message": "The server is busy. Please retry shortly.", "type": "server_error"}}), 503, {"Retry-After": "5"}
            _active += 1
        permit = _Permit()
        try:
            response = make_response(function(*args, **kwargs))
            if response.status_code < 400:
                original = response.response
                def stream():
                    try:
                        yield from original
                    finally:
                        try:
                            close = getattr(original, "close", None)
                            if close:
                                close()
                        finally:
                            permit.close()
                response.response = stream()
                response.call_on_close(permit.close)
            else:
                permit.close()
            return response
        except BaseException:
            permit.close()
            raise
    return limited
