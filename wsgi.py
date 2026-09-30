"""WSGI entry point: ``gunicorn -c gunicorn.conf.py wsgi:app``.

The managed service units of existing installations run exactly that
command, so the module name and the ``app`` attribute must not change.
While the lifecycle tool's maintenance file exists (during updates and
backups) visitors see a short "updating" page; the health checks and
``/status`` keep answering.
"""

from bananachat import create_app
from bananachat.update_gate import UpdateGate

app = create_app()
app.wsgi_app = UpdateGate(app.wsgi_app)
