# External API providers

Administrators can offer hosted models through BananaChat's ordinary chat,
playground, agents and `/v1` API alongside Ollama models. Users keep their
BananaChat account and API keys; provider credentials stay on the server.
Provider usage is billed to your provider account. Claude Code subscription
profiles remain a separate, opt-in integration; see [Claude Code](claude-code.md).

## Add and activate a provider

1. Open **Administration → Models → External APIs**.
2. Enter a provider name, API format, base URL and provider API key.
3. Click **Check connection & refresh models**. Select a listed model, optionally
   give it a display name, and enable only its verified vision, function-tool
   and reasoning capabilities. Unselected reasoning stays disabled.
4. Click **Enroll model**, then review and publish it in **Catalog**. Configure
   its access rules and token/rate limits there. Model limits are installed
   before publication and remain active when the model is published.
5. Send a short real conversation through the chat and through a BananaChat API
   key. Verify the provider's model permissions, response, tokens and billing.

| Provider | API format | Base URL |
| --- | --- | --- |
| OpenAI | OpenAI-compatible | `https://api.openai.com/v1` |
| xAI / Grok | OpenAI-compatible | `https://api.x.ai/v1` |
| Anthropic API | Anthropic Messages | `https://api.anthropic.com/v1` |
| Other compatible services or your gateway | OpenAI-compatible | Its documented API root |

Use the API root, without `/chat/completions`, `/messages` or `/models` at the
end. This connector implements OpenAI Chat Completions and native Anthropic
Messages, not every vendor's proprietary API or OpenAI's separate Responses
API. Endpoints must support streamed responses in their selected format.

**Connection settings** let you rotate a key, disable a provider, enable
per-provider automatic enrollment, allow an explicitly trusted private gateway,
and choose compatibility options. A blank key preserves the current key; the
separate remove-key checkbox clears it. Connection changes hide the provider's
models until a fresh check (or explicit manual enrollment). Disabling a
provider refuses new generations and stops its active stream at the next
record. Removing a provider removes its stored credential and retires its
models; conversation and token-usage history stay intact.

For OpenAI-compatible endpoints, **Output limit field** defaults to
`max_completion_tokens` for `api.openai.com`, and `max_tokens` elsewhere. A
proxy may need an explicit override. **Streamed token usage** can omit the
`stream_options` field for gateways that reject it. Usage is still read when
reported; missing counts are recorded as estimates. Native Anthropic uses
`max_tokens` and its own streamed usage.

## Model selection and lifecycle

Model IDs are scoped to the provider: `external:<provider-id>:<upstream-model-id>`.
Use the ID returned by BananaChat's `/v1/models`, not the raw provider ID, when
calling BananaChat. The chat shows your model display name. This prevents
providers with the same upstream model name from colliding, and keeps
self-hosted and subscription models distinct.

Manual enrollment is the default. Optional automatic enrollment publishes known
chat model families with conservative model limits; unknown/non-chat families
wait for review. Model-list APIs usually do not advertise reasoning, vision or
tool capabilities, so automatic enrollment does not invent them. Explicitly
configure supported capabilities and effort levels after reviewing the model.

Enabled providers refresh their model lists every 15 minutes independently of
Ollama. A verified missing model becomes unavailable immediately; a failed,
malformed, oversized or incomplete listing preserves the previous catalog.
Models return when rediscovered, without discarding their custom policies.
Existing lifecycle retirement/removal settings still apply. Explicit upstream
`model_not_found`/`model_not_supported` errors also withdraw the affected model;
generic HTTP errors do not withdraw the entire catalog.

For providers without a model-list endpoint, **Add a verified model ID manually**
is available. Such models are operator-verified and are not withdrawn simply
because they are absent from a list. Model access and actual generation still
need a real test. Ignore lists, access policies, retirement and publication
controls remain binding.

## Token and request limits

Every user's BananaChat API keys share that account's API token budget and
request-rate buckets. Hosted API models count toward standard service budgets
by default, including the configured cloud-chat consumption policy. Unlike the
subscription connector, they do not default to an independent service exemption.
Administrators can change counting, limits, exemptions and reasoning access
through the existing account/model controls.

Initial model presets use one five-hour token window and no weekly token window:

| Preset | Five-hour tokens | Requests per minute | Burst | Usage weight |
| --- | ---: | ---: | ---: | ---: |
| Light | 300,000 | 60 | 10 | 0.5 |
| Standard | 100,000 | 30 | 10 | 1 |
| Heavy | 50,000 | 6 | 3 | 3 |

Opus/Fable/Mythos and pro reasoning families start with Heavy; Haiku starts
with Light; other models start with Standard. Demand adjustment is enabled for
these presets and follows the existing site settings. Administrators can
replace them, disable a quota, add an optional weekly budget, and set request
rules per second/minute/hour/day globally or per account/model. Input and output
usage is recorded as actual tokens; the existing model weight applies when
computing allowance consumption. Token budgets are internal controls, not
provider dollar balances or claims about subscription percentages.

Reasoning uses only the levels explicitly configured for that model. OpenAI
compatible endpoints receive `reasoning_effort` (`off` → `none`, Extra → `xhigh`).
Anthropic receives adaptive thinking and `output_config.effort` for supported
levels. Enable those only for models whose API supports them. Fresh account
permissions and effort ceilings are checked at routing, including fallback
models. The existing unlock-request and lower-tier cascade rules apply.

## Transport and private credentials

Public endpoints require HTTPS and normal certificate/hostname verification.
Credentials in URLs, query strings and fragments are refused. No redirects or
environment proxies are followed. DNS results are checked and pinned for the
connection, preventing a second DNS resolution from changing the destination.
Private addresses need explicit administrator permission; link-local,
multicast, unspecified and reserved/metadata addresses remain blocked.

API keys live in private `0600` files under
`BC_INSTANCE_DIR/.provider-keys` (directory `0700`), owned by the service user.
Database rows contain opaque references, not plaintext keys. Keys are never
rendered back into forms, returned by the public API or placed in audit details.
Upstream errors are replaced with credential-free messages. Provider-key files
are protected by filesystem permissions; this is not a claim of at-rest
application encryption. Back up the entire private instance directory securely,
including these files, and preserve it when applying a source update.

HTTP/SSE input, output, event and token values are bounded. DNS, headers and
body have deadlines; cancellation closes sockets. A valid terminal protocol
marker is required before success. Partial/disconnected responses use existing
interrupted-answer accounting. Function calls retain matching IDs and are
validated by the agent engine before execution. Images are sent as already
validated inline content, not fetched from user-supplied URLs by this connector.

Hosted models stay available when the local compute server is down. The banner
reports a local outage; automatic selection can choose a hosted model. Access
rules and maintenance still apply. Hosted HTTP requests do not require memory
for a local inference model and still use the site's concurrency/queue limits.

## Verification limits

Regression checks exercise real local HTTP servers, native/open-compatible
streams, private credential files, model discovery/retirement, schema upgrades,
admin forms, public API routing, quota sharing and cancellation. Browser checks
cover desktop and phone flows. They use synthetic keys and model responses.
Actual provider authentication, model-specific parameters, billing and live
traffic must be verified after your credentials are configured. No production
provider key or live subscription authentication is shipped in the ZIP.
