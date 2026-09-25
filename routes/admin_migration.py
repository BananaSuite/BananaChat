"""Database export and guidance for quiesced application restoration."""

from contextlib import closing
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile

from flask import flash, redirect, render_template, request, send_file, url_for

import db
from helpers import admin_required, get_current_user
from logger import log_action


def register_admin_migration_routes(app):
    @app.route("/admin/migration")
    @admin_required
    def admin_migration():
        return render_template("admin/migration.html", settings=db.get_site_settings())

    @app.route("/admin/migration/export")
    @admin_required
    def admin_migration_export():
        """Export a verified database with bounded memory and private temporary files."""
        archive = tempfile.TemporaryFile("w+b")
        try:
            with tempfile.TemporaryDirectory(prefix="bananachat-export-") as directory:
                snapshot = Path(directory) / "bananachat.db"
                db.create_consistent_backup(snapshot)
                with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)) as conn:
                    counts = {"users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                              "models": conn.execute("SELECT COUNT(*) FROM ai_models").fetchone()[0]}
                metadata = json.dumps({"format": "bananachat-migration-v1",
                    "exported_at": datetime.now(timezone.utc).isoformat(), "counts": counts,
                    "note": "Database only. Session keys, uploaded audio and model weights are separate. Use the managed migrate command for a complete installation package."}).encode()
                with tarfile.open(fileobj=archive, mode="w:gz") as output:
                    output.add(snapshot, arcname="bananachat.db")
                    info = tarfile.TarInfo("export_meta.json")
                    info.size = len(metadata)
                    output.addfile(info, io.BytesIO(metadata))
            archive.seek(0)
            log_action("admin_migration_export", request, user=get_current_user())
            response = send_file(archive, mimetype="application/gzip", as_attachment=True,
                download_name="bananachat_export_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + ".tar.gz", max_age=0)
            response.headers["Cache-Control"] = "private, no-store"
            response.call_on_close(archive.close)
            return response
        except BaseException:
            archive.close()
            raise

    @app.route("/admin/migration/import", methods=["POST"])
    @admin_required
    def admin_migration_import():
        """Retain the old route with guidance; a web request cannot stop its own peers."""
        flash("Restore from the server with bananachat restore --legacy-database FILE. The command stops all processes and saves a rollback package before replacing data.", "info")
        return redirect(url_for("admin_migration"))
