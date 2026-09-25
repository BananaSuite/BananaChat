# API Reference

BananaChat exposes an OpenAI-compatible REST API at `/v1/`. Existing tools and SDKs that support a custom base URL work without modification.

---

## Authentication

All API endpoints require a Bearer token in the `Authorization` header:

```
Authorization: Bearer bc-xxxxxxxxxxxxxxxxxxxx
```

Tokens are created in the web UI under **API → Create Token**. Each token belongs to your account and draws from your credit balance.

---

## Base URL

| Deployment | Base URL |
|---|---|
| Local dev | `http://127.0.0.1:8000/v1` |
| Production (IP) | `http://YOUR_IP/v1` |
| Production (domain) | `https://ai.example.com/v1` |

---

## Endpoints

### GET /v1/models

List all models available to your account.

```bash
curl http://YOUR_IP/v1/models \
  -H "Authorization: Bearer <token>"
```

**Response:**

```json
{
  "object": "list",
  "data": [
    {
      "id": "llama3.2",
      "object": "model",
      "created": 1720000000,
      "owned_by": "ollama"
    },
    {
      "id": "auto",
      "object": "model",
      "created": 1720000000,
      "owned_by": "bananachat"
    }
  ]
}
```

The special `auto` model selects the best available model based on current queue load.
The list is filtered by rollout, capability, category, and individual-model access policies for the token owner.

---

### POST /v1/chat/completions

Send a chat completion request.

```bash
curl http://YOUR_IP/v1/chat/completions \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Explain quantum entanglement in one sentence."}
    ]
  }'
```

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `model` | string | Yes | Model name from `/v1/models`, or `"auto"` |
| `messages` | array | Yes | Array of `{role, content}` objects |
| `stream` | boolean | No | `true` for SSE streaming (default: `false`) |
| `temperature` | float | No | Sampling temperature 0–2 (default: model default) |
| `max_tokens` | integer | No | Maximum tokens to generate |
| `top_p` | float | No | Nucleus sampling probability |

**Non-streaming response:**

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1720000000,
  "model": "llama3.2",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Quantum entanglement is a phenomenon where two particles become correlated..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 42,
    "completion_tokens": 18,
    "total_tokens": 60
  }
}
```

**Streaming response** (`"stream": true`):

The response is a series of `text/event-stream` events, each containing a `data:` line with a JSON delta object. The stream ends with `data: [DONE]`.

```
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","choices":[{"delta":{"role":"assistant"},"index":0}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","choices":[{"delta":{"content":"Quantum"},"index":0}]}

data: [DONE]
```

---

### POST /v1/images/generations

Generate an image with a discovered ComfyUI checkpoint. This experimental
endpoint follows the OpenAI base64 response shape; BananaChat does not store
generated images.

```bash
curl http://YOUR_IP/v1/images/generations \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "prompt": "A quiet observatory above a sea of clouds",
    "n": 1,
    "response_format": "b64_json"
  }'
```

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `prompt` | string | Yes | Image description, up to 10,000 characters |
| `model` | string | No | Image-generation model name or `auto` (default) |
| `size` | string | No | `WIDTHxHEIGHT`, 256–2048 per side and at most 4,194,304 pixels |
| `n` | integer | No | Must currently be `1` |
| `response_format` | string | No | Must currently be `b64_json` |

The model must be a discovered ComfyUI checkpoint, rolled out by an admin, and
allowed by every applicable access policy. Ollama catalog rows are text-only;
vision models that understand image input are not image-generation models.

```json
{
  "created": 1720000000,
  "model": "image-model",
  "data": [{"b64_json": "iVBORw0KGgo..."}],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 0,
    "total_tokens": 12
  }
}
```

---

## Model Access Policies

API tokens inherit their owner's access. Admins can apply independent policies
to uncensored models, image generation, categories, and individual models:

- **Allow all**
- **No one except** users retained on the allowlist
- **Everyone except** users retained on the denylist

All applicable policies must allow a model. Changing policy modes does not
delete either list. A denied API request receives HTTP 403 even if the client
submits a hidden model name directly or uses `auto`.

---

## Credits and Rate Limiting

API calls consume credits from your account balance at 1 credit per 1,000 tokens (input + output combined).

Image generation uses a configurable charge of 5 credits per image and
a default limit of 6 generations per minute. Operators can change these with
`BC_IMAGE_CREDITS_PER_GENERATION` and `BC_IMAGE_GENERATION_RPM`.

- **Regular credits**: higher queue priority (priority 1, behind admin users)
- **Slow credits**: lowest queue priority (priority 3); automatically used when regular credits are exhausted for the day

Default daily limits: 30 regular credits + 15 slow credits. Admins can approve quota increases requested from the account page.

If your credit balance is exhausted, the API returns HTTP 429:

```json
{"error": {"message": "Daily credit limit reached", "type": "rate_limit_error"}}
```

---

## Using with OpenAI SDKs

### Python

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://YOUR_IP/v1",
    api_key="bc-xxxxxxxxxxxxxxxxxxxx",
)

response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)
```

### Node.js

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://YOUR_IP/v1",
  apiKey: "bc-xxxxxxxxxxxxxxxxxxxx",
});

const response = await client.chat.completions.create({
  model: "auto",
  messages: [{ role: "user", content: "Hello!" }],
});
console.log(response.choices[0].message.content);
```

### curl (streaming)

```bash
curl http://YOUR_IP/v1/chat/completions \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -N \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Count to 5"}],
    "stream": true
  }'
```

---

## Error Responses

All errors follow the OpenAI error format:

```json
{
  "error": {
    "message": "Human-readable error description",
    "type": "error_type",
    "code": "optional_code"
  }
}
```

| HTTP Status | Type | Meaning |
|---|---|---|
| 400 | `invalid_request_error` | Malformed request body |
| 401 | `authentication_error` | Missing or invalid token |
| 403 | `permission_error` | Account suspended or model not available |
| 404 | `not_found_error` | Unknown model |
| 429 | `rate_limit_error` | Credit limit reached |
| 503 | `service_unavailable` | Ollama unreachable or queue full |

---

## API Playground

The web UI includes a built-in API playground at **/api/playground**. It lets you send requests and inspect raw responses without leaving the browser.


### Admission and completion limits

Chat completions accept text `system`, `user` and `assistant` messages. This endpoint does not implement tool calls or multimodal messages; use the chat attachment interface for files and images. Requests validate the JSON types and context budget before reserving inference capacity. `max_tokens` and `max_completion_tokens` limit generated tokens, capped by `BC_MAX_OUTPUT_TOKENS`; they do not change the model's context window.

The API and playground use the same bounded queue for local and remote inference. A non-admin account can have three outstanding requests, with one running at a time. Full queues return HTTP 503 with `Retry-After: 5` before a streaming response begins. Streaming failures emit an error event and `[DONE]` without a successful finish reason. Clients should check for that error or a successful terminal choice. Disconnecting an API request stops it; browser chats instead save their outcome for reconnection.

Final usage is committed before success is announced. If a response is interrupted after producing text and the inference server supplies no final usage, the ledger records a labelled estimate (one token per four characters) for generated text and input context. Such estimates are not exact tokenizer counts. Remote workers must deliver an ordered terminal chunk and final usage; stale or cancelled jobs cannot be completed later.
