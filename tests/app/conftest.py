"""The application fixtures live in :mod:`tests.app.fixtures`, loaded as a plugin by the root conftest.

Loading them as a plugin (rather than defining them here) keeps them available
however test paths are ordered on the command line: pytest forgets a nested
conftest when ``tests/app`` and top-level ``tests/`` files are interleaved.
"""

from tests.app.fixtures import *  # noqa: F401,F403  (helpers imported by test modules)
