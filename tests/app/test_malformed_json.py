"""Malformed JSON cannot escape parsing or alter saved user preferences."""

import pytest

from bananachat.db import users
from tests.app.fixtures import Browser


@pytest.mark.parametrize("body", [
    '{"font_scale": NaN}',
    '{"font_scale": Infinity}',
    '{"font_scale": -Infinity}',
    '{"font_scale": 1e999}',
    '{"font_scale": -1e999}',
    '{"unknown":' + '[' * 1200 + '0' + ']' * 1200 + '}',
    '{"unknown":' + '{"nested":' * 1200 + '0' + '}' * 1200 + '}',
], ids=["nan", "infinity", "negative-infinity", "overflow", "negative-overflow",
        "deep-array", "deep-object"])
def test_invalid_json_preferences_are_rejected_without_mutation(app, make_user, body):
    user = make_user("json-reader")
    browser = Browser(app)
    browser.login(user["username"])
    with app.app_context():
        original = users.get_preferences(user["id"])

    response = browser.fetch("/api/preferences", method="POST", data=body,
                             content_type="application/json")
    assert response.status_code == 400
    assert response.json["error"]["message"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    with app.app_context():
        assert users.get_preferences(user["id"]) == original

    # A rejected document must not poison the session or database connection.
    valid = browser.post_json("/api/preferences", {"font_scale": 1.2})
    assert valid.status_code == 200
    assert valid.json["preferences"]["font_scale"] == 1.2
