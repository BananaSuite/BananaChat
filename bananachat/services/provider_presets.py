"""Connection presets for external API providers (administrator interface, English only).

A preset fills in the API format, base URL, how the key is sent and suggested
extra headers, and selects small dialect differences (how reasoning effort and
the output limit are sent). Every value stays editable, and ``custom`` covers
any other OpenAI-compatible service. Model lists always come from the provider.
"""
from __future__ import annotations

from dataclasses import dataclass

AUTH_STYLES = {
    "bearer": "Authorization: Bearer <key>",
    "x-api-key": "x-api-key: <key>",
    "api-key": "api-key: <key> (Azure)",
}


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    protocol: str  # "openai" (Chat Completions) or "anthropic" (Messages)
    base_url: str
    auth_style: str = "bearer"
    # Suggested non-secret headers; "{site_url}" and "{site_name}" are filled in by the page.
    headers: tuple = ()
    local: bool = False  # a server on this machine or the local network (needs trusted private endpoints)
    # Runs the models on the operator's own hardware: they count as local models (token limits, the local share of
    # the chat settings), the model list is checked every minute and they hide while the server is off.
    own_hardware: bool = False
    key_required: bool = True
    reasoning: str = "reasoning_effort"  # how effort is sent: "reasoning_effort" or "openrouter"
    token_parameter: str = "max_tokens"  # noqa: S105 - what "Automatic" resolves to: an API field name
    key_check: str = ""  # a path that validates the key when the model list is public
    hint: str = ""


PRESETS = {preset.key: preset for preset in (
    Preset("openai", "OpenAI", "openai", "https://api.openai.com/v1",
           token_parameter="max_completion_tokens",  # noqa: S106 - an API field name, not a secret
           hint="Create a key at platform.openai.com. Chat Completions API."),
    Preset("anthropic", "Anthropic", "anthropic", "https://api.anthropic.com/v1", auth_style="x-api-key",
           hint="Create a key at console.anthropic.com. Native Messages API with thinking and effort."),
    Preset("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1",
           headers=(("HTTP-Referer", "{site_url}"), ("X-Title", "{site_name}")), reasoning="openrouter",
           key_check="/key", hint="One key for many vendors. The optional headers attribute requests to your site."),
    Preset("gemini", "Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai",
           hint="Gemini's OpenAI-compatible endpoint with a Google AI Studio key."),
    Preset("mistral", "Mistral AI", "openai", "https://api.mistral.ai/v1"),
    Preset("groq", "Groq", "openai", "https://api.groq.com/openai/v1"),
    Preset("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1"),
    Preset("xai", "xAI (Grok)", "openai", "https://api.x.ai/v1"),
    Preset("together", "Together AI", "openai", "https://api.together.xyz/v1"),
    Preset("fireworks", "Fireworks AI", "openai", "https://api.fireworks.ai/inference/v1"),
    Preset("azure", "Azure OpenAI", "openai", "https://YOUR-RESOURCE.openai.azure.com/openai/v1",
           auth_style="api-key", token_parameter="max_completion_tokens",  # noqa: S106 - an API field name
           hint="Replace YOUR-RESOURCE. Enrol your deployment names with “Add a model ID manually”."),
    Preset("lmstudio", "LM Studio (local)", "openai", "http://localhost:1234/v1", local=True, key_required=False,
           own_hardware=True, hint="Start the server in LM Studio (Developer → Start server). Models load on first use; "
                                   "their context length and vision support come from LM Studio's own API."),
    Preset("vllm", "vLLM (local)", "openai", "http://localhost:8000/v1", local=True, key_required=False,
           own_hardware=True, hint="vllm serve <model>. Add --api-key and enter it here if the server is shared."),
    Preset("llamacpp", "llama.cpp server (local)", "openai", "http://localhost:8080/v1", local=True,
           key_required=False, own_hardware=True,
           hint="llama-server -m model.gguf --jinja. The context size comes from the server's /props."),
    Preset("litellm", "LiteLLM proxy", "openai", "http://localhost:4000/v1", local=True, key_required=False,
           hint="Use a LiteLLM virtual key if the proxy requires one."),
    Preset("custom", "Other OpenAI-compatible service", "openai", "",
           hint="Any server that implements Chat Completions with streaming."),
)}

def get(key) -> Preset:
    return PRESETS.get(key) or PRESETS["custom"]


def headers_for(preset: Preset, *, site_url: str = "", site_name: str = "") -> dict:
    values = {"site_url": site_url, "site_name": site_name}
    result = {}
    for name, value in preset.headers:
        value = value.format(**values).strip()
        if value:
            result[name] = value
    return result
