"""Third review of personalities: share links of unavailable owners and what the shared page reveals."""

from __future__ import annotations

import pytest

from tests.app.test_limits import _signed_in


def _shared(app, owner, **fields):
    from bananachat.db import personalities

    with app.app_context():
        personality_id = personalities.create(owner["id"], "Pirate", "Talk like a pirate.", created_by=owner["id"],
                                              **fields)
        return personality_id, personalities.share(personality_id)


@pytest.mark.parametrize("how", ["suspended", "no_access"])
def test_share_links_stop_working_while_the_owner_may_not_use_personalities(app, make_user, how):
    from datetime import datetime, timedelta, timezone

    from bananachat.db import access, users

    owner, viewer = make_user("olive"), make_user("vic")
    _personality_id, token = _shared(app, owner)
    browser = _signed_in(app, "vic")
    assert browser.get(f"/personalities/shared/{token}").status_code == 200
    with app.app_context():
        if how == "suspended":
            users.suspend(owner["id"], datetime.now(timezone.utc) + timedelta(days=1))
        else:
            access.add_membership("custom_personality", 0, owner["id"], "denylist", added_by=None)
    assert browser.get(f"/personalities/shared/{token}").status_code == 404
    assert browser.post(f"/personalities/shared/{token}/save").status_code == 404
    assert viewer is not None


def test_the_shared_page_hides_a_preferred_model_the_viewer_cannot_use(app, make_user):
    from bananachat.db import personalities

    owner = make_user("mona")
    make_user("val")
    personality_id, token = _shared(app, owner)
    with app.app_context():
        # A model the viewer cannot use (not in the catalog for them).
        personalities.update(personality_id, name="Pirate", instructions="Talk like a pirate.", enabled=True,
                             updated_by=owner["id"], preferred_model="secret-internal-model:70b")
    browser = _signed_in(app, "val")
    page = browser.get(f"/personalities/shared/{token}").get_data(as_text=True)
    assert "secret-internal-model" not in page
    response = browser.post(f"/personalities/shared/{token}/save", follow_redirects=True)
    assert "secret-internal-model" not in response.get_data(as_text=True)
