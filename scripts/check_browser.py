#!/usr/bin/env python3
"""Exercise the chat UI in Chromium using a disposable local application."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://source.example.invalid/BananaChat"
PAYLOAD = '<img src=x onerror="window.untrusted=true"><script>window.untrusted=true</script>'


def serve_fixture(directory):
    """Create fixture accounts and chats, then serve only on an ephemeral loopback port."""
    for name in list(os.environ):
        if name.startswith("BC_"):
            del os.environ[name]
    os.environ.update(
        BC_ENV="testing",
        BC_INSTANCE_DIR=str(directory),
        BC_DATABASE_PATH=str(directory / "app.db"),
        BC_LOGGING_LEVEL="off",
        BC_SECURE_COOKIES="0",
        BC_SOURCE_URL=SOURCE_URL,
        BC_OLLAMA_URL="http://127.0.0.1:9",
        BC_INFERENCE_FALLBACK_URL="http://127.0.0.1:9",
    )
    sys.path.insert(0, str(ROOT))
    from app import app
    import db
    from helpers._passwords import generate_password_hash
    from werkzeug.serving import make_server
    from services import dispatcher, model_recovery, ollama

    # The timing challenge is intentionally disabled for an automated form
    # submission. The real CSRF token and session-cookie flow remain enabled.
    app.config.update(TESTING=True)
    username, password = "browser-fixture", "Disposable-browser-password-123"
    uid = db.complete_initial_setup(username, generate_password_hash(password))
    for model in ("browser-broken:latest", "browser-tiny:latest"):
        db.upsert_model(model, model)
    ollama.list_available_models = lambda: [{"name": "browser-broken:latest"}, {"name": "browser-tiny:latest"}]
    ollama.list_running_models = lambda: []
    dispatcher.should_use_worker = lambda *_: False
    def generate(model, messages, options=None):
        if model == "browser-broken:latest":
            raise RuntimeError("Fixture model cannot load")
        prompt = messages[-1]["content"]
        if prompt.startswith("[partial failure]"):
            yield "The saved partial response.", False, {}
            raise RuntimeError("Fixture private backend diagnostic")
        if prompt.startswith("[slow]"):
            for _ in range(35):
                yield "Working. ", False, {}
                time.sleep(0.1)
            yield "Completed after reconnect.", True, {"prompt_tokens": 1, "completion_tokens": 36}
            return
        yield "Recovered with the available model.", True, {"prompt_tokens": 1, "completion_tokens": 1}
    ollama.generate_chat_stream = generate
    model_recovery.write({"schema": 1, "state": "pending", "inventory": {
        "schema": 1, "ollama": ["browser-tiny:latest"], "huggingface": [], "manual": [], "excluded_paths": ["models"]}})
    sid = db.create_session(uid, title="Browser regression fixture")
    db.add_message(sid, "user", "Explain the purpose of this project.")
    db.add_message(sid, "assistant", "A **private**, self-hosted chat application.")
    db.add_message(sid, "assistant", PAYLOAD)
    token = db.create_share_token(sid, uid)
    history_sid = db.create_session(uid, title="Long conversation")
    for number in range(65):
        db.add_message(history_sid, "assistant", f"Earlier reply {number:03d}")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    metadata = {
        "url": f"http://127.0.0.1:{server.server_port}",
        "username": username,
        "password": password,
        "private_chat": "/chat/" + sid,
        "shared_chat": "/share/" + token,
        "history_chat": "/chat/" + history_sid,
    }
    state = directory / ".ready.json"
    state.write_text(json.dumps(metadata))
    state.chmod(0o600)
    state.replace(directory / "ready.json")
    server.serve_forever()


def check_chat(state, output):
    """Check access, source links, HTML escaping, and voice callbacks at two widths."""
    from playwright.sync_api import expect, sync_playwright

    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for label, width, height in (("desktop", 1280, 900), ("mobile", 390, 844)):
                context = browser.new_context(viewport={"width": width, "height": height})
                context.add_init_script("""
                    window.SpeechRecognition = class {
                        start() { this.onstart(); this.onerror({error:'not-allowed'}); this.onend(); }
                        stop() { this.onend(); }
                    };
                    Object.defineProperty(window, 'speechSynthesis', {value: {
                        current: null,
                        speak(utterance) { this.current=utterance; if(utterance.onstart) utterance.onstart(); },
                        cancel() { if(this.current && this.current.onend) this.current.onend(); this.current=null; }
                    }});
                """)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                try:
                    private = context.request.get(state["url"] + state["private_chat"], max_redirects=0)
                    assert private.status == 302 and "/login" in private.headers["location"]
                    page.goto(state["url"] + state["shared_chat"], wait_until="networkidle")
                    footer = page.locator(".source-notice a").last
                    expect(footer).to_have_attribute("href", SOURCE_URL)
                    footer.scroll_into_view_if_needed()
                    footer.click(trial=True)
                    expect(page.locator(".message-bubble").last).to_contain_text(PAYLOAD)
                    assert not page.locator(".message-bubble img, .message-bubble script").count()
                    assert page.evaluate("window.untrusted === undefined")
                    page.screenshot(path=str(output / f"{label}-shared-chat.png"), full_page=True)

                    page.goto(state["url"] + "/login", wait_until="networkidle")
                    page.locator("input[name=username]").fill(state["username"])
                    page.locator("input[name=password]").fill(state["password"])
                    page.locator("button[type=submit]").click()
                    page.wait_for_load_state("networkidle")
                    page.goto(state["url"] + state["private_chat"], wait_until="networkidle")
                    assert page.url == state["url"] + state["private_chat"], "Fixture login did not establish a session"
                    bubbles = page.locator(".message.assistant .message-bubble")
                    expect(bubbles.first.locator("strong")).to_have_text("private")
                    expect(bubbles.last).to_contain_text(PAYLOAD)
                    assert not bubbles.locator("img, script").count()
                    assert page.evaluate("window.untrusted === undefined")

                    speaker = page.locator(".speech-play-btn").first
                    expect(speaker).to_have_text(page.evaluate("window.BC_I18N.read_aloud"))
                    speaker.click()
                    expect(speaker).to_have_attribute("aria-pressed", "true")
                    speaker.click()
                    expect(speaker).to_have_attribute("aria-pressed", "false")
                    page.locator("#voice-input-btn").click()
                    expect(page.locator("#voice-status")).to_have_text(page.evaluate("window.BC_I18N.voice_permission_denied"))
                    for selector in ("#input-model-picker-mount", "#incognito-toggle", "#params-toggle-btn"):
                        bounds = page.locator(selector).bounding_box()
                        assert bounds and bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width, (selector, bounds)
                    page.locator("#params-toggle-btn").click()
                    expect(page.locator("#chat-params-panel")).to_be_visible()
                    expect(page.locator("#personality-select-chat")).to_be_visible()
                    page.locator("#params-toggle-btn").click()
                    expect(page.locator("#chat-params-panel")).not_to_be_visible()
                    assert not errors, errors
                    page.screenshot(path=str(output / f"{label}-private-chat.png"), full_page=True)
                    page.goto(state["url"] + "/admin/models", wait_until="networkidle")
                    expect(page.locator("#model-recovery-heading")).to_have_text("Models after restore")
                    page.locator('button[name=action][value=defer]').click()
                    page.wait_for_load_state("networkidle")
                    expect(page.locator("#model-recovery-heading").locator("..")).to_contain_text("Downloads are deferred")
                    page.screenshot(path=str(output / f"{label}-model-recovery.png"), full_page=True)
                    page.goto(state["url"] + "/chat", wait_until="networkidle")
                    page.evaluate("setActiveModel('browser-broken:latest', 'Broken fixture')")
                    page.locator("#chat-input").fill("Continue with an available model.")
                    page.locator("#chat-send-btn").click()
                    expect(page.locator(".message.assistant .message-bubble").last).to_contain_text("Recovered with the available model.")
                    expect(page.locator(".chat-model-notice").last).to_contain_text("browser-tiny:latest")
                    expect(page.locator("#model-select-chat")).to_have_value("auto")
                    page.locator("#chat-input").fill("[partial failure] Preserve the reply.")
                    page.locator("#chat-send-btn").click()
                    expect(page.locator(".chat-outcome").last).to_contain_text(page.evaluate("window.BC_I18N.generation_failed"))
                    page.reload(wait_until="networkidle")
                    expect(page.locator(".message.assistant .message-bubble").last).to_contain_text("The saved partial response.")
                    expect(page.locator(".chat-outcome").last).to_have_text(page.evaluate("window.BC_I18N.generation_failed"))
                    assert "Fixture private backend diagnostic" not in page.locator("body").inner_text()
                    page.locator("#chat-input").fill("[slow] Stop this reply.")
                    page.locator("#chat-send-btn").click()
                    expect(page.locator(".message.assistant .message-bubble").last).to_contain_text("Working.")
                    page.locator("#chat-stop-btn").click()
                    expect(page.locator(".chat-outcome").last).to_contain_text(page.evaluate("window.BC_I18N.generation_stopped"))
                    page.locator("#chat-input").fill("[slow] Reconnect to this reply.")
                    page.locator("#chat-send-btn").click()
                    expect(page.locator(".message.assistant .message-bubble").last).to_contain_text("Working.")
                    page.reload(wait_until="domcontentloaded")
                    expect(page.locator(".message.assistant .message-bubble").last).to_contain_text("Completed after reconnect.", timeout=15000)
                    expect(page.locator("#chat-send-btn")).to_be_visible()
                    page.goto(state["url"] + state["history_chat"], wait_until="networkidle")
                    expect(page.locator(".message.assistant")).to_have_count(20)
                    expect(page.locator(".message-bubble").last).to_have_text("Earlier reply 064")
                    page.locator(".history-older").click()
                    expect(page.locator(".message-bubble").last).to_have_text("Earlier reply 044")
                    expect(page.locator("#chat-input")).to_have_count(0)
                    page.locator(".history-latest").click()
                    expect(page.locator("#chat-input")).to_be_visible()
                    assert not errors, errors
                    page.screenshot(path=str(output / f"{label}-chat-recovery.png"), full_page=True)
                    results.append({"viewport": label, "access_and_login": True,
                                    "source_link": True, "html_escaping": True,
                                    "speech_and_microphone_errors": True, "composer_controls_fit": True,
                                    "personality_settings_accessible": True, "model_restore_consent": True,
                                    "failed_model_fallback": True, "partial_failure_saved": True,
                                    "stop_and_reconnect": True, "paged_history": True, "script_errors": errors})
                except Exception:
                    page.screenshot(path=str(output / f"{label}-failure.png"), full_page=True)
                    raise
                finally:
                    context.close()
        finally:
            browser.close()
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")


def main():
    """Start and stop a disposable fixture, preserving only reviewable test artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".browser-artifacts")
    parser.add_argument("--serve", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve is not None:
        serve_fixture(args.serve)
        return 0
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bananachat-browser-") as temporary:
        directory = Path(temporary)
        with (output / "fixture.log").open("w") as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", str(directory)],
                                     cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            try:
                ready = directory / "ready.json"
                deadline = time.monotonic() + 30
                while not ready.exists():
                    if child.poll() is not None:
                        raise RuntimeError(f"Browser fixture failed to start; see {output / 'fixture.log'}")
                    if time.monotonic() > deadline:
                        raise TimeoutError("Browser fixture startup timed out")
                    time.sleep(0.1)
                check_chat(json.loads(ready.read_text()), output)
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
    print("Desktop and mobile browser checks passed; temporary server and data removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
