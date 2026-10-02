# API Reference

BananaChat offers an OpenAI-compatible HTTP API under `/v1`. Tools and SDKs
that accept a custom base URL (the official `openai` packages, LangChain,
LiteLLM, curl…) work without changes.

| Endpoint | Purpose |
|---|---|
| `GET /v1/models` | Models your account may use |
| `GET /v1/models/{id}` | One model |
| `POST /v1/chat/completions` | Chat completions, streamed or not |
| `POST /v1/images/generations` | One image from a text prompt (only when image generation is enabled) |

The web interface has a matching developer area at **/developer** (the
**API** link in the navigation): API key management, the account's token limits, quick-start
snippets, the usage history (`/developer/usage`) and a playground
(`/developer/playground`). The addresses of the previous release (`/api`,
`/api/usage`, `/api/playground`) redirect there permanently.

---

## Authentication

Send a personal API key as a bearer token:

```
Authorization: Bearer bc-xxxxxxxxxxxxxxxx
```

Create API keys on the developer page. A key is shown **once**, when it is
created or rotated; the server stores only its SHA-256 hash, so a lost key
cannot be recovered. Rotate it instead. Each account can have up to 10 active
keys. A key acts on behalf of its owner: it sees the models the owner may
use and spends the owner's allowance. Rotating replaces the value and keeps the
name; revoking disables the key immediately. Creating, rotating and revoking
keys is recorded in the audit log.

API keys authenticate requests. Model tokens measure text consumption and
are counted against the account's quota; creating another key does not increase
that quota.

| Situation | Status | `type` | `code` |
|---|---|---|---|
| Missing, malformed, unknown or revoked key | 401 | `authentication_error` | `invalid_api_key` |
| Owner's account is suspended | 403 | `permission_error` | `account_suspended` |
| More than 30 failed authentications from one address in a minute | 429 | `rate_limit_error` | `rate_limit_exceeded` |

---

## Base URL

Use your site's address followed by `/v1`, for example
`https://chat.example.com/v1`. The developer page shows the exact value.

---

## Limits

* **Request rate:** every `/v1` request takes one request from the account's
  bucket for the API service. The bucket holds *burst* requests and refills at
  *requests per second* (default 1 per second, bursts of 10; set by the
  administrator per service, and per account for custom limits). All API keys of
  an account, and the playground, share it. Administrators are exempt. See
  [rate-limit headers](#rate-limit-headers).
* **Body size:** 4 MB per request; larger bodies get 413.
* **Messages:** at most 100 per request and at most `BC_CHAT_MAX_CONTEXT_CHARS`
  characters of text in total (default 300,000).
* **Images in messages:** at most `BC_CHAT_MAX_CONTEXT_IMAGES` per request
  (default 8), each at most `BC_CHAT_MAX_IMAGE_MB` (default 5 MB).
* **Answer length:** `max_tokens` is capped by `BC_MAX_OUTPUT_TOKENS`
  (default 8,192); answers are also cut at `BC_CHAT_MAX_RESPONSE_KB` and after
  `BC_GENERATION_TIMEOUT` seconds.
* **Queue:** requests share the server's inference queue with the chat. Each
  account can have one API request running and up to three waiting.
* **Images:** `BC_IMAGE_GENERATION_RPM` images per minute and account
  (default 6; administrators exempt).

### Rate-limit headers

The request rate is a set of rules such as “60 requests per minute” and
“1,000 per day”; every rule must pass. The account's buckets are shared by all
of its API keys and the playground. Chat and agents use separate account
buckets for their respective services. Every authenticated
response (successful or not) of an account with a request rate carries
OpenAI-style headers for the tightest rule (the one with the fewest requests
left):

| Header | Meaning |
|---|---|
| `x-ratelimit-limit-requests` | The rule's bucket size (requests allowed in a row). |
| `x-ratelimit-remaining-requests` | Whole requests left in that bucket after this one. |
| `x-ratelimit-reset-requests` | Time until that bucket is full again, e.g. `2s`, `0.5s`, `1m30s`, `4h59m30s`. |
| `x-ratelimit-limit-tokens` | The account's single 5-hour token allowance (only when the API has a 5-hour limit). |
| `x-ratelimit-remaining-tokens` | Tokens left in that allowance. |
| `x-ratelimit-reset-tokens` | Time until the window ends; `0s` while no window is open (the next request opens one). |

Administrators, and accounts whose rate is not limited (for example during an
unlimited grant), get no request headers. When a bucket is empty the request
is refused before anything else happens:

```json
{"error": {"message": "Rate limit reached: at most 60 requests per minute (bursts of up to 10). Retry in 1 s.",
           "type": "rate_limit_error", "param": null, "code": "rate_limit_exceeded"}}
```

with status 429 and `Retry-After` set to the whole seconds (rounded up) until
the next request is allowed. SDKs such as `openai-python` retry on their own.
A model can have request-rate rules of its own; they are checked once the
model is chosen and answer the same way, naming the model.

---

## Token limits

API calls, the playground and image generation draw from the account's **API
allowance**, counted in tokens (prompt plus completion).

* **5-hour window:** a window opens with the account's first counted request
  and lasts 5 hours; when it ends, the single allowance is full again.
  When it is spent, further counted requests are refused until the reset or
  an approved increase. There is no separate slow-token allowance or
  spillover. Normal accounts run at API priority (behind administrators and
  sped-up accounts, ahead of chat messages); an administrator can still set
  an account's queue priority to slow independently of token consumption.
* **Weekly tokens** (only when the site enables them) count everything used in
  the account's rolling week, which starts with the first request after the
  previous week ended and lasts 7 days. When they run out, requests are
  refused until the week ends, even if the five-hour allowance has tokens left.
* **Model weight:** a request counts its tokens × the model's weight (a heavy
  model ×3 spends the allowance three times as fast). Some models do not count
  toward the allowance at all and have limits of their own instead; model
  limits count the model's tokens in every service (API, chat and agents).
* The numbers can grow with the account's tier, a dynamic adjustment to demand,
  the music-program bonus and grants from an administrator; the developer page
  shows the current figures and why. Fixed music bonuses can be configured
  independently for five-hour and weekly allowances. Administrators are not
  limited.

Upgrades combine previous regular and slow allocations when slow tokens were
enabled; otherwise the regular allocation is kept. Historical usage from
both allocations counts toward the single allowance without resetting its
window. Header names and quota error codes remain the same.

A request is refused before it starts when no tokens remain, and again when it
leaves the queue if the tokens ran out (or the account was suspended, or the
token revoked) while it waited:

```json
{"error": {"message": "You have used the tokens of this 5-hour window. They come back at 2026-10-01 14:32 UTC.",
           "type": "rate_limit_error", "param": null, "code": "insufficient_quota"}}
```

with status 429 and `Retry-After` set to the seconds until the window ends.
When the weekly tokens are the ones used up, the message names the end of the
week and `Retry-After` counts the seconds until then. A model's own limits
answer the same way and name the model (`You have used your 5-hour allowance
for Big Model. It comes back at …; other models still work.`); a model an
administrator locked for the account gets 403 `model_locked`.

Usage is charged once per request, with the token counts reported by the
model server. If a streamed answer is interrupted (the client disconnects, the
deadline passes) the text produced so far is charged using an estimate of four
characters per token; the usage history marks such entries as estimates. A
request that fails before producing any text is not charged. Every request is
listed in the usage history with its key and the tokens it counted.

---

## Service status

Maintenance mode and outages of the AI server never take the API down:
`GET /v1/models` keeps working and so does token management. Only **starting
new generations** is refused, with status 503 and `Retry-After: 60`:

```json
{"error": {"message": "The site is under maintenance, so new messages are paused. …",
           "type": "server_error", "param": null, "code": "maintenance"}}
```

`code` is `maintenance` (an administrator switched on maintenance mode; the
administrator's message is appended) or `outage` (the AI server is
unreachable). Administrators' API keys keep working during maintenance so they
can test.

Monitors can poll the public `GET /status`, which always answers HTTP 200
while the application runs:

```json
{"status": "ok", "accepting_requests": true, "database": "ok", "checked_at": 1790000000}
```

`status` is `ok`, `degraded` (a fallback AI server is in use), `maintenance`,
`outage` or `updating`; `accepting_requests` tells whether new generations can
start.

---

## GET /v1/models

```bash
curl https://chat.example.com/v1/models -H "Authorization: Bearer $BANANACHAT_API_KEY"
```

```json
{
  "object": "list",
  "data": [
    {"id": "auto", "object": "model", "created": 0, "owned_by": "bananachat"},
    {"id": "llama3.2:3b", "object": "model", "created": 1720000000, "owned_by": "bananachat"},
    {"id": "comfyui:sdxl_base.safetensors", "object": "model", "created": 1720000500, "owned_by": "bananachat"}
  ]
}
```

The list always starts with `auto` and then contains every model the token's
owner may use on the API: rolled out by an administrator, available on its
backend, and allowed by every access policy that applies (uncensored models,
image generation, categories scoped to the API or to both surfaces, and the
model's own policy). Image models (ids starting with `comfyui:`) appear only
when image generation is enabled. `created` is when the model was added to the
catalog (Unix time).

`GET /v1/models/{id}` returns one of these objects, or 404 (`model_not_found`)
when the model does not exist or is not available to you.

---

## POST /v1/chat/completions

```bash
curl https://chat.example.com/v1/chat/completions \
  -H "Authorization: Bearer $BANANACHAT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Explain quantum entanglement in one sentence."}
    ]
  }'
```

The body must be a JSON object sent with `Content-Type: application/json`
(415 otherwise; 400 for invalid JSON, including `NaN`/`Infinity`).

| Field | Type | Notes |
|---|---|---|
| `model` | string | A model id from `/v1/models`, or `auto` (default). |
| `messages` | array | 1-100 messages with `role` `system`, `user` or `assistant` (`developer` is treated as `system`). |
| `stream` | boolean | `true` for server-sent events. Default `false`. |
| `stream_options.include_usage` | boolean | With `stream`, send a final usage chunk. |
| `max_completion_tokens`, `max_tokens` | integer ≥ 1 | Maximum generated tokens (the smaller one wins), capped by `BC_MAX_OUTPUT_TOKENS`. |
| `temperature` | number 0-2 | |
| `top_p` | number 0-1 | |
| `presence_penalty`, `frequency_penalty` | number -2-2 | |
| `seed` | integer | Reduced modulo 2³¹. |
| `stop` | string or array of ≤ 4 strings | Each 1-200 characters. |
| `n` | integer | Only `1`. |
| `reasoning_effort` | `none`, `minimal`, `low`, `medium`, `high`, `extra`, `xhigh`, `max` | `xhigh` aliases `extra`. For reasoning models only (ignored for others); see [Reasoning effort](#reasoning-effort). |
| `response_format` | object | Only `{"type": "text"}`. |

Parameters you leave out use the administrator's settings for the model.
Unknown fields (such as `user`) are ignored. Tool and function calling
(`tools`, `functions`, `tool_choice` other than `none`, `tool` messages,
`tool_calls`), `logprobs` and JSON response formats are **not supported** and
return 400 with `code: "unsupported_parameter"`.

**Content.** `content` is a string, or an array of parts:

* `{"type": "text", "text": "…"}` - text parts are joined with newlines;
* `{"type": "image_url", "image_url": {"url": "data:image/png;base64,…"}}` -
  only in `user` messages, only inline `data:` URLs with base64 PNG, JPEG or
  WebP data (remote URLs are never fetched). A request with images needs a
  vision model: `auto` picks one, a named model without vision support gives
  400 (`model_not_suitable`), and 503 is returned when no vision model is
  available. Images are passed to Ollama in its `images` field.

**Model choice.** `auto` picks an available model you may use, preferring
models that are already loaded, and falls back to another one if the first
fails before answering. A named model is used as is: unknown names give 404
(`model_not_found`), models you may not use 403 (`model_not_allowed`), and
models that are currently unavailable 503.

**System prompt.** When the request has no system message, the model's system
prompt set by the administrator (if any) is added in front.

### Response

```json
{
  "id": "chatcmpl-5c1d…",
  "object": "chat.completion",
  "created": 1720000000,
  "model": "llama3.2:3b",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "Two particles share one quantum state…"},
    "logprobs": null,
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 42, "completion_tokens": 18, "total_tokens": 60}
}
```

`model` is the model that actually answered. `finish_reason` is `stop`, or
`length` when the answer hit `max_tokens` or the size limit. Reasoning models
put their thinking in `message.reasoning_content`, separate from `content`.

### Streaming

With `"stream": true` the response is `text/event-stream`. The server waits
until the model starts answering before it sends the first byte, so queue and
model errors still arrive as ordinary HTTP errors. Then:

```
data: {"id":"chatcmpl-…","object":"chat.completion.chunk","created":1720000000,"model":"llama3.2:3b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"logprobs":null,"finish_reason":null}]}

data: {"id":"chatcmpl-…","object":"chat.completion.chunk",…,"choices":[{"index":0,"delta":{"content":"Two"},"logprobs":null,"finish_reason":null}]}

data: {"id":"chatcmpl-…","object":"chat.completion.chunk",…,"choices":[{"index":0,"delta":{},"logprobs":null,"finish_reason":"stop"}]}

data: {"id":"chatcmpl-…","object":"chat.completion.chunk",…,"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":18,"total_tokens":60}}

data: [DONE]
```

* the first chunk carries `delta.role`;
* content arrives in `delta.content`, reasoning in `delta.reasoning_content`;
* one chunk carries the `finish_reason`;
* the usage chunk (empty `choices`) is sent only with
  `"stream_options": {"include_usage": true}`;
* the stream always ends with `data: [DONE]`.

If the answer fails after it started, the stream sends
`data: {"error": {"message", "type", "param", "code"}}` followed by
`data: [DONE]`, without a successful `finish_reason`. Closing the connection
stops the generation; the text produced so far is charged (see [Token limits](#token-limits)).

### Reasoning effort

Reasoning models take effort levels, lowest first: `off`, `low`, `medium`,
`high`, `max` - as many as each model supports (a model that can only switch
thinking on or off counts “on” as `medium`). `reasoning_effort` maps to them:
`none` → off (the lowest level for models that always think), `minimal` →
low, and `low`, `medium`, `high`, `max` as named; a model without a level
uses the nearest lower one it has. Without `reasoning_effort` the request runs
at `medium`, or at the highest level the account may use if that is lower.

Levels above `medium` are **locked** until unlocked for the account (by an
administrator, an approved request on the account page, or automatically
after sustained use of the model); administrators may also set other
defaults. A request above the account's level is refused:

```json
{"error": {"message": "The reasoning effort 'high' is locked for Big Model; the highest level you can use is 'medium'. Ask for more on your account page.",
           "type": "permission_error", "param": "reasoning_effort", "code": "reasoning_effort_locked"}}
```

with status 403. The web chat offers the same levels in the composer, with
locked ones marked and a link to request them.

### Errors specific to completions

| Situation | Status | `code` |
|---|---|---|
| Your account already has several requests waiting | 429 (`Retry-After: 10`) | `too_many_requests` |
| The server queue is full | 503 (`Retry-After: 5`) | `queue_full` |
| No slot became free within `BC_QUEUE_TIMEOUT` | 503 (`Retry-After: 10`) | `queue_timeout` |
| `reasoning_effort` above the level unlocked for the account | 403 | `reasoning_effort_locked` |
| The model is locked for the account | 403 | `model_locked` |
| The model's own request rate or tokens are used up | 429 | `rate_limit_exceeded`, `insufficient_quota` |
| The model failed before answering | 502 | `upstream_error` |
| The inference server is unreachable | 503 | `backend_unavailable` |
| Claude subscription capacity is exhausted, busy or cannot be verified | 503 (`Retry-After: 30`) | `provider_capacity_unavailable` |
| The answer exceeded `BC_GENERATION_TIMEOUT` | 504 | `timeout` |

Backend messages are passed on with addresses and URLs removed.

---

## POST /v1/images/generations

Available when the operator enabled image generation
(`BC_IMAGE_BACKEND=comfyui`, see [comfyui.md](comfyui.md)); otherwise it
returns 404 with `code: "images_disabled"`.

```bash
curl https://chat.example.com/v1/images/generations \
  -H "Authorization: Bearer $BANANACHAT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "auto", "prompt": "A quiet observatory above a sea of clouds", "size": "1024x1024"}'
```

| Field | Type | Notes |
|---|---|---|
| `prompt` | string | Required, 1-10,000 characters. |
| `model` | string | An image model id (`comfyui:<checkpoint>`) or `auto` (default: the first image model you may use). |
| `size` | string | `WIDTHxHEIGHT`, each side 256-2048 and a multiple of 8, at most 4,194,304 pixels. Default `1024x1024`. |
| `n` | integer | Only `1`. |
| `response_format` | string | Only `b64_json` (default). |

```json
{
  "created": 1720000000,
  "model": "comfyui:sdxl_base.safetensors",
  "data": [{"b64_json": "iVBORw0KGgo…"}],
  "output_format": "png",
  "size": "1024x1024",
  "usage": {"input_tokens": 0, "output_tokens": 5000, "total_tokens": 5000}
}
```

Each image costs a fixed `BC_IMAGE_TOKENS_PER_GENERATION` tokens (default 5,000;
× the image model's weight against the allowance), reported in `usage`. The charge is
reserved before the image is queued and kept reserved while it is generated;
it becomes a charge only when the image is delivered and is released on any
failure. Generated images are not stored by BananaChat.

| Situation | Status | `code` |
|---|---|---|
| Invalid prompt, size, `n` or `response_format` | 400 | `invalid_value`, `invalid_size`, `prompt_too_long`, `unsupported_parameter` |
| Text model given as `model` | 400 | `model_not_suitable` |
| Unknown model | 404 | `model_not_found` |
| Model not allowed (e.g. the image-generation policy) | 403 | `model_not_allowed` |
| No image model available, or the model is offline | 503 | `model_unavailable` |
| More than `BC_IMAGE_GENERATION_RPM` images in a minute, or the account's request rate | 429 | `rate_limit_exceeded` |
| Not enough tokens (in the 5-hour window or this week) for one image, or the model's own limit is used up | 429 | `insufficient_quota` |
| The image model is locked for the account | 403 | `model_locked` |
| Queue full / waited longer than `BC_COMFYUI_QUEUE_TIMEOUT` | 429/503 | `too_many_requests`, `queue_full`, `queue_timeout` |
| ComfyUI failed or returned something that is not an image | 502 | `upstream_error` |
| The image was not ready within `BC_COMFYUI_GENERATION_TIMEOUT` | 504 | `timeout` |

The Images page (`/images`) uses the same service with the same limits and
charge.

---

## Errors

Every error under `/v1` - including unknown paths (404), wrong methods (405,
with an `Allow` header), bodies over 4 MB (413) and unexpected server errors -
uses the OpenAI envelope:

```json
{"error": {"message": "Human-readable description", "type": "invalid_request_error",
           "param": "temperature", "code": "invalid_value"}}
```

| Status | `type` |
|---|---|
| 400, 404, 405, 413, 415 | `invalid_request_error` |
| 401 | `authentication_error` |
| 403 | `permission_error` |
| 429 | `rate_limit_error` (with `Retry-After`) |
| 5xx | `server_error` |

`param` names the offending field when there is one (e.g. `messages[2].content`).

---

## Examples

### Python

```python
from openai import OpenAI

client = OpenAI(base_url="https://chat.example.com/v1", api_key="bc-...")

response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)

stream = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Write a haiku about bananas."}],
    stream=True,
)
for chunk in stream:
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="", flush=True)
```

### Node.js

```javascript
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "https://chat.example.com/v1", apiKey: process.env.BANANACHAT_API_KEY });
const response = await client.chat.completions.create({
  model: "auto",
  messages: [{ role: "user", content: "Hello!" }],
});
console.log(response.choices[0].message.content);
```

### An image, saved to a file

```python
import base64
from openai import OpenAI

client = OpenAI(base_url="https://chat.example.com/v1", api_key="bc-...")
result = client.images.generate(model="auto", prompt="A lighthouse at dusk", size="1024x1024")
with open("lighthouse.png", "wb") as handle:
    handle.write(base64.b64decode(result.data[0].b64_json))
```

---

## Playground

`/developer/playground` sends conversations from the browser with a model,
system prompt, parameters (temperature, top_p, max_tokens, seed, stop,
reasoning) and several turns, and shows the answer as it streams. It uses the
same validation, model choice, queue and token limits as the API (charged as
`playground` requests, sharing the account's request rate), offers only the
reasoning levels the account may use, and can show each
conversation as the equivalent `curl` request.

## Hosted API models

Administrators may publish hosted models alongside Ollama and Claude subscription
models. Obtain their provider-scoped IDs from `/v1/models`, for example
`external:1:gpt-model-id`, and use them in ordinary `/v1/chat/completions` requests.
Your BananaChat API key is unchanged; all of your keys still share your account's
API quotas. Hosted-provider credentials stay on the server. See
[provider configuration](external-providers.md) for supported formats and capabilities.
