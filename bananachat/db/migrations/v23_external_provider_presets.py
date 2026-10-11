"""Version 23: external provider presets, the header that carries the key, and extra request headers.

Existing providers keep their connection: OpenAI-compatible ones send the key
as a bearer token and Anthropic ones as ``x-api-key``, as before. The preset is
inferred from the base URL; it only selects small dialect differences.

Gemini lists resource names ("models/gemini-2.5-flash"). 1.6 enrolled them as
listed; from this version the listing gives the bare model ID, which is what
its chat endpoint takes (services.external_api.discover). Models enrolled
before are called by the bare ID too, so the next listings still find them:
their public ID (``external:<provider>:models/…``), publication, limits and
access stay as they were, and automatic enrollment adds no second copy. A model
also enrolled by its bare ID already (typed in by hand) keeps both rows
unchanged; listings then find the bare one.
"""

import json
from urllib.parse import urlsplit

from sqlite_migrations import add_columns

# Frozen copy of the hosts known when this version was written (services.provider_presets may change later).
_HOSTS = {"api.openai.com": "openai", "api.anthropic.com": "anthropic", "openrouter.ai": "openrouter",
          "generativelanguage.googleapis.com": "gemini", "api.mistral.ai": "mistral", "api.groq.com": "groq",
          "api.deepseek.com": "deepseek", "api.x.ai": "xai", "api.together.xyz": "together",
          "api.fireworks.ai": "fireworks"}
_RESOURCE = "models/"


def _preset(base_url, protocol):
    try:
        host = (urlsplit(base_url or "").hostname or "").lower()
    except ValueError:
        host = ""
    if protocol == "anthropic":
        return "anthropic"
    preset = _HOSTS.get(host) or ("azure" if host.endswith(".openai.azure.com") else "custom")
    return "custom" if preset == "anthropic" else preset


def _bare(name):
    """The bare model ID of a Gemini resource name, as discover() lists it from this version on."""
    return name[len(_RESOURCE):] if isinstance(name, str) and name.startswith(_RESOURCE) and len(name) > 7 else name


def _bare_gemini_ids(conn, provider_id):
    taken = {name for (name,) in conn.execute(
        "SELECT backend_model_name FROM ai_models WHERE backend='external' AND external_provider_id=?",
        (provider_id,))}
    rows = conn.execute("SELECT id, backend_model_name, display_name FROM ai_models WHERE backend='external' "
                        "AND external_provider_id=? AND substr(backend_model_name, 1, 7)=? ORDER BY id",
                        (provider_id, _RESOURCE)).fetchall()
    for model_id, name, display in rows:
        bare = _bare(name)
        if bare == name or bare in taken:
            continue
        conn.execute("UPDATE ai_models SET backend_model_name=?, display_name=? WHERE id=?",
                     (bare, bare if display == name else display, model_id))
        taken.add(bare)
    raw = conn.execute("SELECT discovered_models FROM external_providers WHERE id=?", (provider_id,)).fetchone()[0]
    try:
        listing = json.loads(raw or "[]")
    except ValueError:
        return
    if not isinstance(listing, list):
        return
    # The saved listing as discover() now returns it, so the admin page matches it with the models above.
    entries, seen = [], set()
    for item in listing:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            bare = _bare(item["id"])
            if bare != item["id"]:
                display = item.get("display")
                item = {**item, "id": bare,
                        "display": bare if not isinstance(display, str) or display.startswith(_RESOURCE) else display}
            if bare in seen:
                continue
            seen.add(bare)
        entries.append(item)
    conn.execute("UPDATE external_providers SET discovered_models=? WHERE id=?", (json.dumps(entries), provider_id))


def upgrade(conn):
    add_columns(conn, "external_providers", {
        "preset": "TEXT NOT NULL DEFAULT 'custom'",
        "auth_style": "TEXT NOT NULL DEFAULT 'bearer' CHECK(auth_style IN ('bearer','x-api-key','api-key'))",
        "extra_headers": "TEXT NOT NULL DEFAULT '{}'",
    })
    rows = conn.execute("SELECT id, base_url, protocol FROM external_providers WHERE preset='custom'").fetchall()
    for provider_id, base_url, protocol in rows:
        preset = _preset(base_url, protocol)
        conn.execute("UPDATE external_providers SET preset=?, auth_style=? WHERE id=?",
                     (preset, "x-api-key" if protocol == "anthropic" else "bearer", provider_id))
        if preset == "gemini":
            _bare_gemini_ids(conn, provider_id)
