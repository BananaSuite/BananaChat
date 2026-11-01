"""BananaChat: a self-hosted chat application for Ollama.

The package is importable without its web dependencies so that stdlib-only
tools (the compute proxy, the lifecycle manager) can share constants with it.
"""

PRODUCT_NAME = "BananaChat"
__version__ = "1.7.6"


def create_app(*args, **kwargs):
    """Build the Flask application (imported lazily to keep this module light)."""
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
