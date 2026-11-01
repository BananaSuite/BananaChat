"""Regression tests for MC3-1 of the full re-audit pass 7 (cloud-session views bounded by size)."""

from __future__ import annotations

import json
import re
import secrets

from tests.app.fixtures import Browser
from tests.app.test_agents import add_user, agents_app, runner, token_file  # noqa: F401

STEPS = 40
OUTPUT = "<" * 60_000   # tojson writes each '<' as 6 bytes, HTML escaping as 4


def _long_task(app, user):
    from bananachat import db
    from bananachat.db import agents as agents_db
    task_id = secrets.token_hex(8)
    with app.app_context():
        agents_db.create(task_id, user_id=user["id"], title="Long output", prompt="Run it", model_id=None,
                         model_name="m", swarm=False, owner_token="t", max_user=None, max_site=100)
        ids = [agents_db.add_step(task_id, None, kind="tool", tool_name="bash", tool_args={"command": "x"},
                                  tool_result=OUTPUT, tool_status="ok") for _ in range(STEPS)]
        db.execute("UPDATE agent_tasks SET status='finished', owner_token=NULL WHERE id=?", (task_id,))
        everything = [row["id"] for row in agents_db.steps(task_id, limit=10_000)]
    assert everything[-STEPS:] == ids
    return task_id, everything


def _page_data(html: str) -> dict:
    found = re.search(r'<script type="application/json" id="page-data">(.*?)</script>', html, re.S)
    return json.loads(found.group(1))


def test_mc3_1_agent_task_views_stop_at_a_size(agents_app):
    app = agents_app()
    alice = add_user(app, "alice", allowed=True)
    task_id, everything = _long_task(app, alice)
    browser = Browser(app)
    browser.login("alice")

    # The task page embeds about a megabyte of step text and leaves the rest to the events poll.
    page = browser.get(f"/agents/{task_id}")
    assert page.status_code == 200
    assert len(page.data) < 8 * 1024 * 1024, len(page.data)
    data = _page_data(page.get_data(as_text=True))
    assert data["more"] is True and 0 < len(data["steps"]) < len(everything)

    # The poll the page runs (?after=<last step>) then fetches the rest in bounded pieces, none lost.
    seen = [step["id"] for step in data["steps"]]
    while True:
        answer = browser.get(f"/agents/{task_id}/events?after={seen[-1]}")
        assert answer.status_code == 200 and len(answer.data) < 2 * 1024 * 1024, len(answer.data)
        body = answer.get_json()
        seen += [step["id"] for step in body["steps"]]
        if not body["more"]:
            break
    assert seen == everything
    first = browser.get(f"/agents/{task_id}/events?after=0").get_json()
    assert first["more"] is True and len(first["steps"]) < len(everything)

    # The administrator's view of the task pages by size too ("Next entries").
    admin = Browser(app)
    admin.login("admin", "admin-password")
    seen, after = [], 0
    while True:
        view = admin.get(f"/admin/agents/tasks/{task_id}?after={after}")
        assert view.status_code == 200 and len(view.data) < 8 * 1024 * 1024, len(view.data)
        html = view.get_data(as_text=True)
        seen += [int(number) for number in re.findall(r"#(\d+) · ", html)]
        found = re.search(rf"/admin/agents/tasks/{task_id}\?after=(\d+)", html)
        if not found:
            break
        after = int(found.group(1))
    assert seen == everything


def test_mc3_1_a_step_larger_than_the_budget_is_still_shown(agents_app):
    from bananachat.db import agents as agents_db
    app = agents_app()
    alice = add_user(app, "alice", allowed=True)
    task_id, everything = _long_task(app, alice)
    with app.app_context():
        rows, more = agents_db.step_page(task_id, after=everything[1], max_bytes=10)
        assert [row["id"] for row in rows] == [everything[2]] and more is True
        rows, more = agents_db.step_page(task_id, after=everything[-2], max_bytes=10)
        assert [row["id"] for row in rows] == [everything[-1]] and more is False
        assert agents_db.step_page(task_id, after=everything[-1], max_bytes=10) == ([], False)


def _nested(depth: int, width: int, levels: int):
    """A chain of *depth* single-item lists ending in a *width*-wide tree of zeros *levels* deep, as a model
    may send in tool arguments (within the client's depth and node limits)."""
    node = 0
    for _ in range(levels):
        node = [node] * width
    for _ in range(depth):
        node = [node]
    return node


def test_mc3_1_admin_task_view_stays_small_for_nested_tool_arguments(agents_app):
    from bananachat import db
    from bananachat.db import agents as agents_db
    app = agents_app()
    alice = add_user(app, "alice", allowed=True)
    task_id = secrets.token_hex(8)
    tree = _nested(28, 20, 3)
    with app.app_context():
        agents_db.create(task_id, user_id=alice["id"], title="Nested", prompt="Run it", model_id=None,
                         model_name="m", swarm=False, owner_token="t", max_user=None, max_site=100)
        agents_db.add_step(task_id, None, kind="tool", tool_name="bash", tool_args={"command": "ls"},
                           tool_result="", tool_status="ok")
        for _ in range(20):
            agents_db.add_step(task_id, None, kind="assistant",
                               tool_calls=[{"name": "bash", "arguments": {"command": tree}}])
            agents_db.add_step(task_id, None, kind="tool", tool_name="bash", tool_args={"command": tree},
                               tool_result="", tool_status="ok")
        db.execute("UPDATE agent_tasks SET status='finished', owner_token=NULL WHERE id=?", (task_id,))
        stored = db.query("SELECT SUM(length(tool_args)) + SUM(length(tool_calls)) AS n FROM agent_steps "
                          "WHERE task_id=?", (task_id,))[0]["n"]
        count = agents_db.step_count(task_id)
    assert 900_000 < stored < 1024 * 1024
    admin = Browser(app)
    admin.login("admin", "admin-password")
    view = admin.get(f"/admin/agents/tasks/{task_id}")
    assert view.status_code == 200
    html = view.get_data(as_text=True)
    # All steps fit the text budget; their arguments are shown, but not re-indented 20 times over.
    assert len(re.findall(r"#(\d+) · ", html)) == count > 40
    assert len(view.data) < 3 * stored, (len(view.data), stored)
    # Ordinary arguments are still indented for reading.
    assert "{\n  &#34;command&#34;: &#34;ls&#34;\n}" in html
