"""Regression tests for NC3-1 and NC4-1 of the full re-audit pass 8 (sub-agent lanes in the cloud-session
views bounded; the compute server's model restore applies the C3-9 registry rule)."""

from __future__ import annotations

import json
import re

from tests.app.fixtures import Browser
from tests.app.test_agents import (add_user, agents_app, call, fast_loop, runner, set_settings,  # noqa: F401
                                   start_ok, token_file, wait_status)

INSTRUCTIONS = "<" * 8000   # the most delegate accepts
REPORT = "<" * 20_000       # the most finish accepts; tojson writes each '<' as 6 bytes


def _page_data(html: str) -> dict:
    found = re.search(r'<script type="application/json" id="page-data">(.*?)</script>', html, re.S)
    return json.loads(found.group(1))


def test_nc3_1_sub_agent_lanes_in_the_task_views_are_bounded(agents_app, fake_ollama):
    from bananachat import db
    from bananachat.db import agents as agents_db
    from bananachat.web import agents as web_agents

    app = agents_app()
    set_settings(app, swarms_enabled=True, max_subagents=4, max_concurrent_subagents=2)
    add_user(app, "bob", allowed=True)
    browser = Browser(app)
    browser.login("bob")

    def responder(body):
        messages = body["messages"]
        if "You are a sub-agent" in messages[0]["content"]:
            return call("finish", summary=REPORT)
        if messages[-1]["role"] == "tool":
            return call("finish", summary="done")
        return call("delegate", tasks=[{"title": f"Part {n}", "instructions": INSTRUCTIONS} for n in range(4)])

    fake_ollama.tool_responder = responder
    task_id = start_ok(browser, "Go", swarm="1")
    wait_status(app, task_id, "finished", timeout=20)
    # Many follow-up runs later: the same rows the loop writes, 4 lanes a run.
    with app.app_context():
        for index in range(5, 101):
            agents_db.add_lane(task_id, index, f"Part {index}", INSTRUCTIONS)
            agents_db.update_lane(task_id, index, status="finished", steps_used=1, summary=REPORT)
        last = db.scalar("SELECT MAX(id) FROM agent_steps WHERE task_id=?", (task_id,), 0)
        assert len(agents_db.lanes(task_id)) == 100
        # The full report stays in the lane's finish step.
        finish = db.query("SELECT tool_args FROM agent_steps WHERE task_id=? AND agent=1 AND tool_name='finish'",
                          (task_id,))
        assert json.loads(finish[0]["tool_args"])["summary"] == REPORT

    # The poll with no new steps carries only the newest lanes, without instructions, reports clipped.
    answer = browser.get(f"/agents/{task_id}/events?after={last}")
    assert answer.status_code == 200 and len(answer.data) < 256 * 1024, len(answer.data)
    body = answer.get_json()
    assert body["steps"] == []
    assert [lane["agent"] for lane in body["lanes"]] == list(range(101 - web_agents.EVENTS_LANES, 101))
    assert all("instructions" not in lane for lane in body["lanes"])
    assert all(lane["summary"] == "<" * web_agents.LANE_SUMMARY_CHARS + "…" for lane in body["lanes"])

    # The page holds the newest lanes too (64 clipped reports, about 1.5 MB of escaped `<` next to the
    # steps), so its size no longer grows with every run.
    page = browser.get(f"/agents/{task_id}")
    assert page.status_code == 200 and len(page.data) < 4 * 1024 * 1024, len(page.data)
    lanes = _page_data(page.get_data(as_text=True))["lanes"]
    assert [lane["agent"] for lane in lanes] == list(range(101 - web_agents.PAGE_LANES, 101))

    # Short reports are sent as they are.
    with app.app_context():
        agents_db.update_lane(task_id, 100, summary="All good")
    lanes = browser.get(f"/agents/{task_id}/events?after={last}").get_json()["lanes"]
    assert lanes[-1]["summary"] == "All good"

    admin = Browser(app)
    admin.login("admin", "admin-password")
    view = admin.get(f"/admin/agents/tasks/{task_id}")
    assert view.status_code == 200
    shown = re.findall(r"<li><strong>(\d+)\. Part", view.get_data(as_text=True))
    assert [int(n) for n in shown] == list(range(101 - web_agents.PAGE_LANES, 101))


# ----- NC4-1 ---------------------------------------------------------------------------------------

DOTLESS = ["intranet-host/team/model:latest", "localhost:11434/ns/m", "2130706433/ns/m:latest",
           "metadata/computeMetadata/v1", "hf.co/owner/repo/extra:Q4"]
TRUSTED = ["llama3.2:3b", "team/model:latest", "hf.co/owner/repo:Q4_K_M", "registry.ollama.ai/library/qwen3:4b"]


def test_nc4_1_compute_restore_uses_the_c3_9_registry_rule(tmp_path, fake_ollama):
    from banana_ops import models
    from banana_ops.files import write_json
    from bananachat.services import pulls

    # A recovery list from before the fix (or edited by hand) with such names: they are not pulled.
    fake_ollama.models = []
    write_json(tmp_path / ".model-recovery.json", {"schema": 1, "state": "pending", "inventory": {
        "ollama": DOTLESS + ["llama3.2:3b"], "huggingface": [], "manual": []}})
    result = models.compute_recovery(tmp_path, "restore", assume_yes=True, upstream=fake_ollama.url)
    assert [body["name"] for path, body in fake_ollama.requests if path == "/api/pull"] == ["llama3.2:3b"]
    assert [item["model"] for item in result["failed"]] == DOTLESS
    assert result["completed"] == ["llama3.2:3b"] and result["state"] == "deferred"

    # The same decision as the web server's download check, for every name above.
    for name in DOTLESS + TRUSTED:
        try:
            pulls.validate_ollama_name(name)
            allowed = True
        except ValueError:
            allowed = False
        assert models.downloadable(name) is allowed is (name in TRUSTED), name


def test_nc4_1_an_existing_ollamas_custom_registry_models_need_review(tmp_path, fake_ollama):
    from banana_ops.models import inventory

    fake_ollama.models = ["intranet-host/team/model:latest", "llama3.2:3b", "metadata/computeMetadata/v1"]
    result = inventory(tmp_path, {}, upstream=fake_ollama.url)
    assert result["ollama"] == ["llama3.2:3b"]
    assert [(item["backend"], item["name"]) for item in result["manual"]] == [
        ("ollama", "intranet-host/team/model:latest"), ("ollama", "metadata/computeMetadata/v1")]
    assert "intranet-host/team/model:latest" not in result.get("sizes", {})
