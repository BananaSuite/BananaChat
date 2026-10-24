# External API providers

Administrators can offer hosted models through BananaChat's ordinary chat,
playground, agents and `/v1` API alongside Ollama models. Users keep their
BananaChat account and API keys; provider credentials stay on the server.
Provider usage is billed to your provider account. Anthropic models are
available through the native Anthropic Messages API with an Anthropic API key;
consumer subscriptions are not supported.

BananaChat speaks two API formats:

- **Anthropic Messages**, natively, including adaptive thinking, effort,
  thinking budgets for older Claude models, tool use and image input.
- **OpenAI Chat Completions**, used by OpenAI and by every OpenAI-compatible
  service: OpenRouter, Google Gemini, Mistral, Groq, DeepSeek, xAI, Together,
  Fireworks, Azure OpenAI and local servers such as LM Studio, vLLM, llama.cpp
  and LiteLLM.

Endpoints must stream their answers (server-sent events). OpenAI's separate
Responses API and vendor-specific native APIs (for example Gemini's own API)
are not used.

## Add a provider

1. Open **Admin → Models → External APIs**.
2. Under **Add a provider**, choose the **Service**. The preset fills in the
   API base URL, the API format, the header that carries the key and any
   suggested extra headers. Every value stays editable. Choose **Other
   OpenAI-compatible service** for anything not listed and enter its base URL.
   If you leave the base URL empty, the service's own address is used.
3. Give the provider a name and paste its API key, then click **Add provider**.
   The key is stored encrypted on the server and is never shown again. Adding,
   changing or removing a provider asks for your password (or an authenticator
   code) unless you signed in or confirmed in the last ten minutes, with or
   without a key: the connection decides where users' prompts are sent.
4. Click **Test connection**. It checks the key and the model list. Enter a
   model ID (the field suggests listed ones) to also stream a short test answer:
   this costs a few tokens and shows whether the provider reports token usage.
   Errors explain what failed (rejected key, missing permission, no credit,
   wrong URL, rate limit, timeout) and quote the provider's own message with
   anything that looks like a key removed.
5. Click **Refresh model list**. **Available models** lists every model the
   provider returns, with its context window, maximum output, price per million
   tokens and features where the provider states them. Filter the list, select
   the models you want to offer and click **Add selected models**. Tick
   **Publish the selected models now** to make them available at once with
   standard limits; otherwise they wait for review in **Catalogue**, where you set
   the display name, categories, access rules and limits before publishing.
6. Send a short real conversation through the chat and through a BananaChat API
   key, and compare the tokens with the provider's own usage page.

Models are never hard-coded: the list always comes from the provider. For an
endpoint without a model list (or Azure deployment names), use **Add a model
ID manually**.

## Service setup

| Service | Base URL (preset) | Key header | Notes |
| --- | --- | --- | --- |
| Anthropic | `https://api.anthropic.com/v1` | `x-api-key` (plus `anthropic-version`) | Native Messages API. |
| OpenAI | `https://api.openai.com/v1` | `Authorization: Bearer` | Sends `max_completion_tokens`. |
| OpenRouter | `https://openrouter.ai/api/v1` | `Authorization: Bearer` | Suggests `HTTP-Referer` and `X-Title` headers. |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `Authorization: Bearer` | Gemini's OpenAI-compatible endpoint. |
| Mistral AI | `https://api.mistral.ai/v1` | `Authorization: Bearer` | |
| Groq | `https://api.groq.com/openai/v1` | `Authorization: Bearer` | |
| DeepSeek | `https://api.deepseek.com/v1` | `Authorization: Bearer` | |
| xAI (Grok) | `https://api.x.ai/v1` | `Authorization: Bearer` | |
| Together AI | `https://api.together.xyz/v1` | `Authorization: Bearer` | |
| Fireworks AI | `https://api.fireworks.ai/inference/v1` | `Authorization: Bearer` | |
| Azure OpenAI | `https://YOUR-RESOURCE.openai.azure.com/openai/v1` | `api-key` | The v1 API; no `api-version` needed. |
| LM Studio | `http://localhost:1234/v1` | none, or bearer | Local server. |
| vLLM | `http://localhost:8000/v1` | none, or bearer (`--api-key`) | Local server. |
| llama.cpp server | `http://localhost:8080/v1` | none, or bearer (`--api-key`) | Local server. |
| LiteLLM proxy | `http://localhost:4000/v1` | bearer (virtual key) | Local or private gateway. |

**Anthropic.** Create a key in the Anthropic Console. The model list includes
each model's context window, maximum output and capabilities (images, adaptive
thinking or thinking budgets, effort levels), which BananaChat uses to suggest
each model's settings.

**OpenAI.** Create a project key at platform.openai.com. Reasoning models
receive `reasoning_effort`; sampling parameters are sent only to models without
a reasoning level.

**OpenRouter.** One key reaches many vendors. Its model list is public, so the
connection test also calls `/key` to prove the key works and shows the credit
left on it. The list includes context length, input modalities (images),
supported parameters (tools, reasoning) and prices. `HTTP-Referer` and
`X-Title` attribute requests to your site; they are optional. Reasoning is sent
as OpenRouter's `reasoning` object and its streamed reasoning is shown.

**Google Gemini.** Use a Google AI Studio key with the OpenAI-compatible
endpoint. Google lists models as `models/<id>`; BananaChat stores the bare ID,
which is what the chat endpoint takes. Models added before 1.7 keep their
public ID (`external:<provider>:models/<id>`), publication, limits and access;
the update only changes the ID they are called with. A provider reached
through another address keeps the Custom preset at the update; choosing the
Gemini preset for it later changes its models' IDs in the same way.

**Mistral, Groq, DeepSeek, xAI, Together, Fireworks.** Create a key in the
service's console and choose its preset. Where the model list states context
windows, vision, tools or prices (Mistral, Groq, Together), they are shown and
used as suggestions. Reasoning text in `reasoning_content`, `reasoning` or
Mistral's typed thinking chunks is shown as reasoning. Groq's usage in
`x_groq` is used when the standard usage record is missing.

**Azure OpenAI.** Replace `YOUR-RESOURCE` with your resource name and use a key
of that resource; it is sent in the `api-key` header. Azure lists base models,
not your deployments: add each deployment name with **Add a model ID
manually**.

**Local servers (LM Studio, vLLM, llama.cpp, LiteLLM).** The local presets tick
**Allow a local or private-network endpoint**, which permits loopback and
private (RFC 1918 / ULA) addresses and plain HTTP to them. `localhost` means the
machine running BananaChat (inside a container, the container itself); use the
server's LAN address otherwise. Leave the key empty unless the server requires
one. If a server rejects `stream_options`, set **Streamed token usage** to
*Endpoint has no stream_options support*; usage is then still read when the
server sends it, and estimated otherwise.

**Model servers on your own hardware count as local.** LM Studio, vLLM and
llama.cpp also tick **Runs on our own hardware** (LiteLLM does not: it usually
forwards to cloud services). For such a provider:

- its models are local models, like Ollama's: the *local* share of the chat
  token settings applies (Administration → Limits), and quota fallbacks between
  local and cloud models treat them as local;
- its model list is checked every minute (cloud providers: every 15 minutes),
  so a model loaded in LM Studio or a restarted llama.cpp shows up quickly;
- with **Enrol new chat models automatically**, every chat model it lists is
  published (cloud providers: known chat families only), since the server
  lists only what its operator installed;
- when it stops answering (LM Studio closed, the PC asleep) for two checks in a
  row its models are hidden from the model choice, with the reason shown in
  Administration → Models; they return with the first listing that works. A
  server that answers with an error (a wrong key, a wrong address) changes
  nothing;
- LM Studio's own API (`/api/v1/models` from 0.4, `/api/v0/models` before)
  adds each model's context length (the loaded instance's, when it is loaded),
  vision, tool use and reasoning levels, and marks embedding models as not for
  chat; llama.cpp's `/props` adds the context the server was started with
  (`-c`) and whether it reads images; vLLM lists `max_model_len` itself. Any of
  these is optional: without it the plain model list is used.

Local servers run in the hosted lane of the request queue
(`BC_MAX_CONCURRENT_HOSTED`), with their own queueing, so a busy or unreachable
Ollama never holds them up.

**Other OpenAI-compatible services.** Choose **Other OpenAI-compatible
service**, enter the API root (without `/chat/completions` or `/models`) and
pick the key header. **Output limit field** lets a gateway receive
`max_completion_tokens` or `max_tokens`. **Extra headers** (one `Name: value`
per line) are sent with every request; they are visible settings, not secret
storage, and cannot replace the key or protocol headers.

## Models and their lifecycle

Model IDs are scoped to the provider: `external:<provider-id>:<upstream-model-id>`.
Use the ID returned by BananaChat's `/v1/models`, not the raw provider ID, when
calling BananaChat. This keeps providers with the same upstream model name and
self-hosted models apart.

Each model's settings (on the provider page, under the model) are:

- **Images in conversations**, **Function tools for agents** and the
  **Supported reasoning levels**. Enable only what the model supports; the
  provider's list pre-selects them where it says.
- **Sampling parameters**: send `temperature` and `top_p` when no reasoning
  level is used. On by default for OpenAI-compatible models and off for
  Anthropic models (current Claude models refuse them).
- **Thinking control** (Anthropic): adaptive thinking with effort for Claude
  4.6 and newer, or a thinking budget for older models.

Enabled providers refresh their model lists every 15 minutes, independently of
Ollama, and update the stated context windows, output limits and prices. A
model missing from two consecutive successful listings becomes unavailable;
one listing without it changes nothing, and a model that reappears is
available again with its settings and limits intact. A failed, malformed,
oversized or incomplete listing changes nothing. A generation answered with a
model-specific "not found" error also withdraws that model until it is listed
again. Manually added models are not withdrawn for being absent from a list.

Optional automatic enrolment (per provider) publishes chat models of known
families with conservative limits; other models wait for review.

## Reasoning, output limits and streaming

| BananaChat level | OpenAI-compatible | OpenRouter | Anthropic (adaptive) | Anthropic (budget) |
| --- | --- | --- | --- | --- |
| Off | `reasoning_effort: none` | `reasoning: {effort: none}` | no thinking | no thinking |
| Low / Medium / High | `low` / `medium` / `high` | same, in `reasoning` | `output_config.effort` | 2,048 / 6,144 / 12,288 budget |
| Extra | `xhigh` | `xhigh` | `xhigh` | 20,480 budget |
| Max | `xhigh` | `xhigh` | `max` | 32,000 budget |

Anthropic adaptive thinking requests `display: "summarized"`, so users see the
reasoning summary instead of a silent pause. A thinking budget is added to
`max_tokens`.

When an agent continues a tool round, the earlier turns of that round go back
exactly as Claude streamed them (signed `thinking` and `redacted_thinking`
blocks first, then the text and the tool calls with Claude's own ids), and
thinking is requested again. Claude binds each thinking block to the system
prompt, the tools and every message before it, so a block is sent back only
while all of that is unchanged. When it is not (older steps were shortened to
fit the context, the tools, the system prompt, the provider settings or the
model changed), when a turn contained a block BananaChat does not send back
(server tools, citations), or when the round's thinking passes 300,000
characters, the round continues without a thinking request, as Claude refuses
one whose last tool turn lost its blocks; the effort level still applies.
Thinking from before the last user message is never sent again. Signatures
are kept only in the memory of the running task, never in the database.

The output limit is the administrator's maximum (or a lower client value),
capped at the model's stated maximum output. Between streamed tokens,
BananaChat waits up to `BC_FIRST_TOKEN_TIMEOUT`, because reasoning models can
think silently for minutes; `BC_GENERATION_TIMEOUT` bounds the whole answer.

## Token usage, limits and credits

Limits and credits use the provider's own token counts:

- OpenAI-compatible services receive `stream_options: {"include_usage": true}`
  and report `prompt_tokens` and `completion_tokens` at the end. Reasoning
  tokens that a service reports only in `total_tokens` (xAI) are counted as
  output.
- Anthropic reports input tokens (including cache writes and reads) when the
  answer starts and the output tokens when it ends.
- An interrupted answer (stopped by the user, a client disconnect or a failure)
  is charged with the input count the provider already reported (else an
  estimate that counts each image as 765 tokens and Chinese, Japanese and
  Korean text as a token per character) and at least an estimate of what was
  produced. A reasoning model that does not stream its reasoning (OpenAI's
  reasoning models; Claude streams a summary) is also charged 20 tokens per
  second for the time it worked before its first output, within the output
  limit; providers generate faster, so this stays below their bill. A request
  stopped or abandoned after it was sent is charged at least its input, even
  with no output. An answer that failed before any output is charged only for
  an input count the provider reported.
- When a provider sends no usage, BananaChat records an estimate and marks it
  as estimated.

Charges for stopped answers are estimates, so for a strict spending cap set a
limit at the provider too.

All of an account's BananaChat API keys share its API token allowance and
request rates. Hosted models count towards the service limits like local ones;
change that per model in Admin → Limits. Token limits are not money: use the
prices on the provider page to choose each model's usage weight.

Initial model presets use one five-hour token window and no weekly token window:

| Preset | Five-hour tokens | Requests per minute | Burst | Usage weight |
| --- | ---: | ---: | ---: | ---: |
| Light | 300,000 | 60 | 10 | 0.5 |
| Standard | 100,000 | 30 | 10 | 1 |
| Heavy | 50,000 | 6 | 3 | 3 |

Opus/Fable/Mythos and pro reasoning families start with Heavy; Haiku starts
with Light; other models start with Standard. Demand adjustment follows the
site settings.

## Errors

| Provider answer | Administrators see | Chat users see | `/v1` API returns |
| --- | --- | --- | --- |
| 401 | the key was rejected | a settings problem; choose another model | 502 `provider_misconfigured` |
| 403, 402, no credit or quota | missing permission or credit | a settings problem | 502 `provider_misconfigured` |
| 404 for the model | the model is not offered | the model is no longer offered | 404 `model_not_found` |
| 404 for the endpoint | check the base URL | a settings problem | 502 `provider_misconfigured` |
| 429 | rate limited, with the wait it asked for | busy, try again | 503 `provider_rate_limited` with its `Retry-After` |
| 529 or overloaded | overloaded | busy, try again | 503 `provider_overloaded` |
| 5xx, unreachable | server error or unreachable | not responding | 503 `provider_unavailable` |
| timeout | did not answer in time | not responding | 504 `timeout` |
| 400, 413, 422 | the provider rejected the request | the request was refused (too long or unsupported content) | 400 `provider_rejected_request` |

Chat messages are shown in the reader's language. When a model fails before
answering, BananaChat tries the request's fallback models first. Rate limits
and overload never mark a model as failing. The provider's own error message
appears only to administrators (connection test, provider page and server
log), with the key and anything that looks like a key removed.

## Security

**Keys.** API keys are encrypted at rest (Fernet: AES-128-CBC with
HMAC-SHA256) with a key derived from the instance secret (`BC_SECRET_KEY`, or
`BC_INSTANCE_DIR/.secret_key`), and stored in private `0600` files under
`BC_INSTANCE_DIR/.provider-keys` (directory `0700`) owned by the service user.
The database holds opaque references only. Keys are never rendered into forms,
returned by the API, written to the audit log or logged. If the instance secret
changes, stored keys can no longer be decrypted: the connection test says so,
and you enter each key again. Back up the instance directory and the instance
secret together, securely.

**Transport.** Public endpoints require HTTPS with normal certificate and
hostname verification. Credentials in URLs, query strings and fragments are
refused. No redirects or environment proxies are followed. DNS results are
checked and pinned for the connection, so a second resolution cannot change
the destination. Loopback and private addresses need **Allow a local or
private-network endpoint**; link-local, multicast, unspecified, reserved,
cloud-metadata (including Alibaba's `100.100.100.200` and AWS's
`fd00:ec2::254`) and translated IPv6 (NAT64, 6to4, Teredo) addresses stay
blocked even then. Plain HTTP is only allowed to non-public addresses.

**Responses.** HTTP/SSE input, output, event and token values are bounded. DNS,
headers and body have deadlines; cancellation closes sockets. A valid finish
marker is required before an answer counts as complete. Tool calls keep
matching IDs and are validated by the agent engine before execution. Images are
sent as already validated inline content; this connector never fetches
user-supplied URLs.

Hosted models stay available when the local compute server is down. The banner
reports a local outage; automatic selection can choose a hosted model. Access
rules and maintenance still apply. Hosted requests do not need memory for a
local inference model and still use the site's concurrency and queue limits.

Test a new provider with your own key before offering it to users.
