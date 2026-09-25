"""Keep the database that importing the application creates out of the checkout."""
import atexit
import os
import shutil
import tempfile

# Several test modules import the application at module level, which
# initialises whatever database DATABASE_PATH names. Without this the suite
# would create and reuse instance/bananachat.db inside the source tree, and a
# parallel run would have two workers initialising the same file at once.
# Individual tests still point config.DATABASE_PATH at their own tmp_path.
#
# The worker processes inherit the controller's environment, so a value chosen
# once at import time would be shared rather than private. Record which process
# chose it and choose again when the value was inherited from another one. A
# path the operator set themselves carries no owner and is always respected.
_OWNER = "BC_COLLECTION_DATABASE_PID"
if "BC_DATABASE_PATH" not in os.environ or os.environ.get(_OWNER) not in (None, str(os.getpid())):
    _collection_scratch = tempfile.mkdtemp(prefix="bananachat-collect-")
    os.environ["BC_DATABASE_PATH"] = os.path.join(_collection_scratch, "collect.db")
    os.environ[_OWNER] = str(os.getpid())
    atexit.register(shutil.rmtree, _collection_scratch, ignore_errors=True)
