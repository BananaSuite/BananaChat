"""The community board: requests that cost supporters nothing are not voted through by default."""

from __future__ import annotations

from tests.app.test_community import _row, _submit


def test_r5_2_effort_requests_are_not_offered_to_the_community_by_default(app, make_user):
    from bananachat import db
    from bananachat.services import community

    asker = make_user("asker")
    with app.test_request_context():
        assert db.one("SELECT community_kinds FROM site_settings WHERE id=1")["community_kinds"] == \
            "window,weekly,temporary"
        assert not community.offered("effort") and not community.offered("rate")
        assert community.offered("window")

    request_id = _submit(app, asker, kind="effort", level="max")["id"]

    assert _row(app, request_id)["community"] == 0


def test_r5_2_an_administrator_choice_of_kinds_is_kept(app):
    from bananachat import db
    from bananachat.db.migrations.v29_community_kinds import upgrade

    with app.test_request_context():
        db.execute("UPDATE site_settings SET community_kinds='window,effort' WHERE id=1")
        upgrade(db.conn())
        assert db.one("SELECT community_kinds FROM site_settings WHERE id=1")["community_kinds"] == "window,effort"
