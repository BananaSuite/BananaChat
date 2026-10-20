"""A stand-in for the official ``codex`` CLI: only the app-server and exec shapes the connector reads.

It never contacts OpenAI. Each run follows $CODEX_HOME/scenario.json and appends
what it was given (arguments, environment, input, messages) to
$CODEX_HOME/calls.jsonl, so tests can inspect a process that has already exited.
"""
import json
import os
import sys
import time
from pathlib import Path

HOME = Path(os.environ["CODEX_HOME"])
SCENARIO = json.loads((HOME / "scenario.json").read_text()) if (HOME / "scenario.json").exists() else {}
ACCOUNT = {"type": "chatgpt", "email": None, "planType": "plus"}
RATE_LIMITS = {"rateLimits": {"limitId": "codex", "planType": "plus",
                              "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": None},
                              "secondary": {"usedPercent": 5, "windowDurationMins": 10080, "resetsAt": None}},
               "rateLimitsByLimitId": None, "ordinaryUsageAllowed": None, "accountId": "workspace-1"}


def record(entry):
    with (HOME / "calls.jsonl").open("a", encoding="utf-8") as calls:
        calls.write(json.dumps(entry) + "\n")


def send(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def answer(method, params):
    """``(result, error)`` for one request, as the real app server shapes them."""
    if method == "initialize":
        return {"userAgent": "fake-codex/0.0.0", "codexHome": str(HOME), "platformFamily": "unix",
                "platformOs": "linux"}, None
    if method == "account/read":
        if SCENARIO.get("account_error"):
            return None, {"code": -32603, "message": "auth storage unreadable sk-must-not-leak"}
        account = SCENARIO.get("account", ACCOUNT)
        return {"account": account, "requiresOpenaiAuth": True}, None
    if method == "account/rateLimits/read":
        if SCENARIO.get("rate_limits_error"):
            return None, {"code": -32600, "message": "chatgpt authentication required to read rate limits"}
        return SCENARIO.get("rate_limits", RATE_LIMITS), None
    if method == "model/list":
        models = SCENARIO.get("models", [])
        start = int(params.get("cursor") or 0)
        end = min(len(models), start + min(params.get("limit") or len(models), SCENARIO.get("page_size", 100)))
        return {"data": models[start:end], "nextCursor": str(end) if end < len(models) else None}, None
    return None, {"code": -32601, "message": "method not found"}


def app_server():
    for line in sys.stdin:
        message = json.loads(line)
        record({"kind": "message", "message": message})
        if "id" not in message:
            continue
        if SCENARIO.get("notify"):
            send({"method": "account/rateLimits/updated", "params": {"rateLimits": {"primary": None}}})
        if SCENARIO.get("server_request") and message.get("method") != "initialize":
            send({"id": "server-1", "method": "account/chatgptAuthTokens/refresh", "params": {}})
        result, error = answer(message.get("method"), message.get("params") or {})
        identity = message["id"] + 100 if SCENARIO.get("wrong_id") and message.get("method") != "initialize" \
            else message["id"]
        send({"id": identity, "error": error} if error else {"id": identity, "result": result})


def run_exec():
    data = sys.stdin.read()
    arguments = sys.argv[1:]
    instructions = next((value.split("=", 1)[1] for value in arguments
                         if value.startswith("model_instructions_file=")), None)
    system = Path(json.loads(instructions)).read_text() if instructions else None
    record({"kind": "exec", "input": data, "system": system})
    if SCENARIO.get("hang"):
        (HOME / "pid").write_text(str(os.getpid()))
    if SCENARIO.get("child"):
        child = os.fork()
        if child == 0:
            if SCENARIO["child"] == "own_group":
                os.setpgid(0, 0)  # As Codex starts its code-mode host.
            time.sleep(60)
            sys.exit(0)
        (HOME / "child-pid").write_text(str(child))
    if SCENARIO.get("stderr"):
        sys.stderr.write("sk-credential-must-not-leak\n")
    for event in SCENARIO.get("events", []):
        send(event)
    if SCENARIO.get("hang"):
        time.sleep(60)
    sys.exit(SCENARIO.get("exit", 0))


record({"kind": "start", "args": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd()})
if sys.argv[1:2] == ["app-server"]:
    app_server()
elif sys.argv[1:2] == ["exec"]:
    run_exec()
else:
    sys.exit(2)
