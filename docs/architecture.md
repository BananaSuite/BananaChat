# Architecture

BananaChat is a Flask application (package `bananachat/`) served by Gunicorn,
with SQLite storage and Ollama for inference. This page explains how the
code is organised and the conventions every part follows.

## Layout

```
bananachat/
  __init__.py        create_app(), version
  config.py          Config dataclass from BC_* environment variables
  app.py             application factory: hooks, security headers, error pages
  security.py        sessions, login_required/admin_required, CSRF, throttling, passwords
  logs.py            logging and the private error log
  formatting.py      template filters (timeago, datetime, number, tokens, filesize)
  i18n/              translation catalogs, one JSON file per area
  db/                storage: connection, migrations, one module per domain
  services/          domain logic: inference, queue, Ollama/ComfyUI clients, access, background jobs
  web/               blueprints (one module per area; web/admin/ is a package)
  templates/         Jinja templates, one folder per area
  static/            css/app.css (design system), js/core.js (shared helpers), per-area css/js
compute/             stdlib-only programs for the compute server (inference proxy, checkpoint agent)
worker/              stdlib-only daemon for volunteer worker PCs
banana, banana_ops/, banana_backup/   the managed-installation lifecycle tool (shared with sibling products)
wsgi.py, gunicorn.conf.py            entry points used by the managed service units
```

Entry points and names relied on by existing installations must not change:
`wsgi:app`, `gunicorn.conf.py`, `python -m compute.inference_proxy`,
`GET /health` and `/healthz`, the `BC_*` variable names and the database
files in `BC_INSTANCE_DIR`.

## Storage

`bananachat.db` gives each thread one connection in autocommit mode:

```python
from bananachat import db

row = db.one("SELECT * FROM users WHERE id=?", (user_id,))     # Row (dict-like, .get())
rows = db.query("SELECT ...", params)                         # list[Row]
count = db.scalar("SELECT COUNT(*) ...", params, 0)
with db.transaction():                                        # BEGIN IMMEDIATE (savepoint when nested)
    db.execute("UPDATE ...", params)
```

* Always name columns; upgraded databases differ in column order from fresh ones.
* Store timestamps with `db.now()` (`YYYY-MM-DD HH:MM:SS`, UTC); compare them
  as strings; parse with `db.parse_timestamp()`; day windows with `db.day_bounds()`.
* Check-then-act sequences go inside `db.transaction()`.
* Schema changes are new, additive migrations appended to
  `bananachat/db/migrations/` (never edit released versions, never drop or
  rename what an older release reads).
* Domain modules live in `bananachat/db/<domain>.py` and contain SQL only;
  decisions belong in `services/`.

## Requests

* Views use `security.login_required` or `security.admin_required`.
  JSON clients get `{"error": {"code", "message"}}` with the right status
  (`security.json_error(message, status, code)`); browsers get a page or a
  redirect. `security.wants_json()` tells them apart (our fetch helper sends
  `X-Requested-With`).
* CSRF is checked for every unsafe request except the bearer-token
  blueprints `api_v1` and `worker_api`. Forms include
  `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">`; scripts send
  the `X-CSRF-Token` header (done by `api()` in `core.js`).
* The default request body limit is 2 MB; raise it per view with
  `@security.body_limit(bytes)` (applied before the body is parsed).
* Throttle with `@security.rate_limit(bucket, limit, window)` or
  `security.allow(bucket, limit, window, key=...)`.
* Record administrative and security-relevant actions with
  `users.audit(actor, "area.action", target, details, security.client_ip())`.
* `g.user`, `g.settings` (site settings dict) and `g.lang` are set for every request.

## Pages

* Templates extend `base.html` and fill `title`, `content`, and optionally
  `styles`, `scripts`, `body_class`, `topbar_extra`.
* **No inline `style="..."` attributes and no inline scripts**: the
  Content-Security-Policy blocks them. Style with classes from
  `static/css/app.css` (buttons, cards, forms, tables, badges, tabs, dialogs,
  `progress.meter` usage bars) plus an optional per-area stylesheet. Pass data
  to scripts with `<script type="application/json" id="page-data">{{ data | tojson }}</script>`.
* Scripts are ES modules: `<script type="module" src="{{ url_for('static', filename='js/<area>.js') }}"></script>`
  importing helpers from `./core.js` (`api`, `readEventStream`, `t`, `toast`,
  `confirmDialog`, `promptDialog`, `secretDialog`, `el`, `pageData`, `setBusy`, `copyText`).
  Build DOM with `el()` or `textContent`; never assign untrusted text to `innerHTML`.
* `<form data-confirm="Question">` asks for confirmation before submitting;
  `data-confirm-danger` styles it as destructive.
* Icons: `{% from "partials/icons.html" import icon %}{{ icon("trash") }}`.
* The interface is translated: `{{ t("area.key", name=value) }}` in templates,
  `t("key")` in scripts for catalog keys starting with `js.` (the prefix is
  dropped in the browser). Each area owns `bananachat/i18n/<area>.json` with
  identical key sets for `en` and `it`; keys are namespaced by area. The
  administrator interface is English-only by design and does not use the catalogs.

## Service status

Problems never take the site down. `services.status` turns maintenance mode,
an unreachable AI server (`BC_INFERENCE_OUTAGE_MODE=shutdown`), the backup
server being in use and the administrator's announcement into notices. Pages
render them with `partials/status_banner.html`; `core.js` polls `/status` and
updates the banner and every composer live (`onStatusChange()` in scripts).
Every view that starts a generation calls `status.guard()` (or
`status.guard(user, openai=True)` in the API) and returns its 503 answer when
new answers are paused; administrators are exempt from maintenance mode.
`GET /status` is public and always answers 200 for monitors; `GET /health` is
the readiness probe used by the updater. While the lifecycle tool updates the
server, `update_gate.UpdateGate` shows a short "updating" page.

## Inference

`services.inference.generate(TextRequest, CancelToken)` is the single path to
a model for chat, playground and API. It queues the request
(`services.queue`), routes it to a remote worker (`services.remote`) or the
local Ollama server (`services.ollama`), falls back to another model when one
fails before answering, enforces the response size and the generation
deadline, and yields `Queued`, `Started`, `Delta` and `Finished` events.
Callers persist the result and charge the usage (`db.credits.charge`).

Whether Ollama runs on this machine is one property, `config.ollama_is_local`
(`BC_INFERENCE_LOCAL`, else: an API key or a non-loopback URL means a remote
compute server). Everything that looks at the host itself (free memory before
a request, model-folder disk space, `nvidia-smi`) runs only when it is true,
and outage probing only when it is false. `services.ollama.endpoint()` returns
the server to use now together with that server's own token, so the outage
fallback never receives the compute token; model downloads and deletions
always use the primary server.

`services.supervisor` runs one thread per process that renews queue and chat
leases and applies Stop requests; `services.background` runs periodic jobs in
one elected process (`@background.job(name, every=seconds)`).

Model access: `services.access.AccessContext.load(user)` answers
`can_use(model, surface)` for the whole catalog with a constant number of queries.

## Models

The catalog is `ai_models` (`db/catalog.py`); `ollama_name` is the public
identifier and `provider` the service that runs the model (`ollama`, or
`comfyui` for image checkpoints). `services/model_lifecycle.py` decides what
happens to a model, `db/model_lifecycle.py` stores the administrator's policy
(one JSON row), the ignore list and the state-change events.

* **Detection.** The `ollama-sync` job (and "Sync now", and every finished
  download) lists `/api/tags` on the primary server and reconciles the catalog
  in one transaction. New models, and models whose digest changed, are read with
  `/api/show` (bounded per sync, one failing model never stops the others):
  capabilities, context length, family, parameter size, quantisation and
  `reasoning_levels` (a JSON list: `["off","low","medium","high"]` for models
  whose `think` takes levels, such as gpt-oss; `["off","on"]` for other
  thinking models; `[]` otherwise). Embedding-only models are never offered for
  chat. A listing that fails changes nothing; an empty listing is believed only
  when it repeats. The result, with per-model errors, is kept in
  `runtime_state` (`model_sync_last`) and shown in Admin → Models.
* **Enrollment** (`enrollment`). With the `manual` policy (default) a new model
  is `new`: hidden until an administrator enables it (name, categories, access,
  features, limit preset). With `automatic`, chat models are published at once
  as `auto` ("enabled automatically — review") with a limit preset from their
  size (`heavy` from 30B parameters, `standard` from 7B, else `light`).
  Publishing a `new` model reviews it. Names or patterns on the ignore list
  (`*:latest`, `*-embed*`) are recorded as `ignored` and never offered; ignored
  models can be restored (which takes the exact name off the ignore list), and
  only a restored model can be published. A `new` model is never chosen
  automatically (`auto`, fallbacks, agents, preferred-model lists), not even
  for administrators, who may still name it explicitly to try it.
* **Limit presets.** `model_lifecycle.apply_limit_preset(model_id, preset)`
  stores the preset in `ai_models.limit_preset` and calls
  `apply_model_preset(model_id, preset)` from `services.limits` when it exists;
  a failure there leaves the stored preset for the administrator.
* **Missing.** Absent from `missing_syncs` consecutive successful syncs (syncs
  within 20 seconds count once, so two processes syncing together do not double
  count) or for `missing_minutes`: `missing_at` is set. The model is already
  unavailable (`backend_available=0`) and returns to exactly its previous state
  when it is listed again. After `retention_days` the row is deleted; a trigger
  copies its display name into `chat_messages.model_label`, so history keeps it.
* **Failing.** `inference` reports each answer: a success resets the count
  (written only when there were failures), a failure that concerns the model
  (not connection errors, 404 (the server no longer has it: the sync
  marks it missing), 429/502/503/504, cancellations, worker PCs, or an
  outage seen by the health probe) counts. A timeout (no first token, a
  stalled stream) counts only when the system is not overloaded: no request
  waits for a slot in the inference queue and no other model timed out within
  five minutes. `failure_threshold` failures within
  `failure_window_minutes` set `failing_at`, which hides the model from
  selection and fallback. The `model-lifecycle` job re-tests it with a tiny
  `/api/generate` through the inference queue at the slowest priority, only
  when a slot is free at once (off-peak), backing off up to six hours.
* **Deprecated and retired.** A deprecated model (optional replacement and
  retirement date) leaves the chat picker and automatic choices; chats that ask
  for it move to the replacement with a notice (`chat.notice_model_deprecated`);
  the API serves it until it is retired, then answers 404 naming the replacement.
  Retiring points personalities that preferred it at the replacement.
* **Deleting.** `inference_queue.model_name` records the model of every waiting
  or running request, in every process. Deleting hides the model first, then
  refuses while requests use it, or (`when_idle`) leaves `delete_requested_at`
  for the `model-lifecycle` job to finish. The row stays as missing; its
  personalities move to the replacement or the default model.

`services.access.offered()` is the one check for "withdrawn" (ignored, failing,
retired, being deleted); `is_text_model` and `is_image_model` include it.

**Downloads** (`services/pulls.py`, `db/pulls.py`) run in the background leader
in queue order, `download_concurrency` at a time. A partial unique index keeps
one active job per model across processes, so queueing (single or bulk) is
idempotent. Claiming a job gives it a new `claim_token` and everything the run
writes names it, so a thread left over from a previous leader cannot change a
job that was claimed again. Shutdowns, pauses and the queue-wide pause put a
job back in the queue (Ollama resumes partial downloads); transient errors and
stalls (no progress for `stall_minutes`) are retried with back-off up to
`download_retries` times (while the health probe sees a remote model server as
down, Ollama downloads wait in the queue without using a retry); errors naming
the model, a full disk (checked locally
before and during a download, or reported by the compute server through the
gateway) and refusals fail at once. A download is done only when `/api/tags`
lists the model with a digest (kept on the job). Only an administrator's cancel
ends a job; it removes the partial download of a model that was not installed
before unless they keep it.

## Limits

Usage is recorded in `credit_ledger` (one row per request) and counted in
**tokens** (prompt + completion) per **pool**: `api` (API, playground,
images), `chat` (chat and no-history chats) and `agent` (request type
`agent`). A row keeps the raw `tokens_in`/`tokens_out` and, in
`credits_used`, the tokens counted against the pool / 1,000 (tokens × the
model's weight, or 0 for a model outside the pool; the column keeps the credit
unit of earlier releases). Storage lives in `db/limits.py`, decisions in
`services/limits.py`.

* **Pool policies** (`limit_policy`, one JSON document per pool, format 2,
  edited in Admin → Limits → Services) hold three limits: `rate` (a list of
  rules `N requests per second | minute | hour | day` with a burst; all must
  pass), `window` (regular and slow tokens per **5-hour window**) and `weekly`
  (tokens per rolling week, off by default). Each has `enabled`; `dynamic`
  (adjust to demand); the token limits also `auto_tiers`. Defaults: API 1
  request/s (bursts of 10), 30k + 15k slow tokens per 5 hours; chat 1
  request/s (bursts of 5) and agents 1 request/s (bursts of 10), no token
  limits.
* **Model policies** (`model_limit_policy`, Admin → Limits → Models): the
  model's `weight`, `counts_toward_pool` (None: yes for local models - an
  empty or `ollama`/`comfyui`/`local` `ai_models.provider` - and no for other
  providers), optional limits of its own (`rate_rules`, `window_tokens`,
  `weekly_tokens`, applied when `enabled`; counted in raw tokens over every
  pool), `dynamic` with a `sensitivity`, `auto_tiers`, and `effort_default`.
  `limits.apply_model_preset(model_id, preset)` fills them from the
  *light* / *standard* / *heavy* presets (heavy is strict); a model without a
  row follows `ai_models.limit_preset` (read when that column exists), else
  *standard*.
* **Per account** (`user_limits`, `user_limit_overrides`,
  `user_model_limits`): custom limits per pool and per model (NULL columns
  follow the policy; a custom value also switches that limit on for the
  account), model locks, tier and tier lock, speed (`slow`, `normal`,
  `fast`), dynamic exemption, effort gating off, and usage-reset times.
* **Windows** (`limit_windows`, per account and scope `pool:<pool>` or
  `model:<id>`): `credits.charge` (and image reservations) open the 5-hour
  and weekly windows of the pool (when the model counts toward it) and of the
  model when none is open; usage counts ledger rows since the window opened
  (and since the account's or the site's last reset). A reset closes the open
  windows (the next request opens new ones); it never deletes ledger rows.
* **Tiers** (`limit_tiers`): an ordered ladder, each with one token multiplier
  and promotion requirements (account age, active days and tokens in the last
  30 days, days since the last suspension). Active days and tokens count only
  usage in services whose policy has token limits on
  (`limits.counted_types`); the personal dynamic factor counts the same.
* **Grants** (`limit_grants`): for one account or everyone (`user_id` NULL),
  one pool, all pools or one model (`model_id`: only that model's limits), one
  limit or all, `unlimited`, `multiplier` or `extra` (tokens; the request rate
  takes multipliers only), from `starts_at` until `ends_at` (NULL: forever)
  unless revoked. Unlimited use for every service (`pool` and `model_id`
  NULL) also lifts the models' own limits; other grants for services leave
  model limits alone.

`limits.effective(user, pool)` and `limits.model_limits(user, model)` return
every number with its reasons (`Reason` codes rendered from the
`account.reason_*` catalog keys), in this order: custom limit, else policy ×
tier (when `auto_tiers`) → dynamic multiplier (model limits: its deviation ×
the sensitivity) → provider capacity (model limits) → music bonus (pools) →
grants (unlimited wins, then multipliers, then extras) → administrators
unlimited. `db.credits.budget(user, pool)` is the pool's short form
(`available`, `next_is_slow`, `weekly_exhausted`, `blocked_until`).
**Admission** is `limits.admit(user, pool, model)`: the pool's tokens (unless
the model is outside the pool), then the model's lock and tokens, then one
request from the model's rate buckets; it returns a `Refusal` (API code,
status, `Retry-After`, a translated message) or the budget for the queue lane.
The pool's request rate is checked where requests arrive
(`limits.check_rate`: API authentication, playground, chat send, image page,
agent starts). Fallback models are filtered with `limits.usable_fallbacks`;
`auto` skips models locked or used up for the account, or counting toward a
service whose tokens are used up (then any other model the request may use).
Admission is a handful of indexed queries: inside `limits.snapshot()` (chat
send, API/playground admission) each setting, window and usage figure is read
once; the check after the queue wait and the charge read everything afresh
(`snapshot(fresh=True)`), and a charge never runs the 30-day query behind the
personal factor while it holds the write lock.

Request rates are token buckets in `rate_buckets` (key
`<pool or model<id>>:<user_id>:<rule>`, the rule as configured, so a changed
rule starts full), taken all-or-nothing in one short write transaction, so
every Gunicorn process shares them. The API adds `x-ratelimit-*-requests`
headers for the tightest rule and `x-ratelimit-*-tokens` for the 5-hour
window. `queue.priority_for` puts `slow` accounts and slow tokens in the slow
lane and `fast` accounts ahead of API requests.

Dynamic adjustment multiplies a demand factor - the inference queue's running
+ waiting requests per `BC_MAX_CONCURRENT` (averaged over ~15 minutes) times
the number of people active in the last 15 minutes per inference slot, both
sampled every minute by the `limits-demand` job into `runtime_state` - by a
personal factor (share of the account's last 30 days of usage in the site's
peak hours, from the `limits-peak-hours` job, and its active days; cached per
process for 10 minutes). The product is clamped to 0.5–2 and rounded to 5 %
steps; model limits scale its deviation by their sensitivity. Custom limits
are a floor, and request rates are only ever lowered.
`limits.register_capacity_provider(provider, report)` lets a provider (for
example a future hosted-model provider with its own 5-hour and weekly
allowance) report the capacity it has left; below half, its models' limits
shrink in proportion. Without a registered provider nothing changes.

**Reasoning effort**: levels `off < low < medium < high < max`. A model's
levels come from `ai_models.reasoning_levels` (a JSON list, filled by the
model catalog; `on` counts as medium), else `("off", "on")` for thinking
models (`limits.supported_efforts`). An account may use levels up to its
ceiling (`limits.effort_ceiling`): its own level for the model, else for all
models (`user_effort_levels`), else the model's `effort_default`, else the
site default (medium); a level from a request or an automatic unlock only
ever raises (an administrator's level is exact and may lock below the
default); without gating (site or account) every level.
`limits.resolve_effort` picks the level for a request (medium or the ceiling
when lower, or raises `EffortLocked`), and `inference.think_for` is the one
place that maps it to Ollama's `think` (false, true, or `low`/`medium`/`high`).
The `limits-tiers` job also runs `limits.auto_unlock_effort`: the next level
for accounts with enough active days and tokens with a model in the period
and no recent suspension, one level at a time, never above the ceiling and
never for levels an administrator kept (`pinned`).

Quota requests (`quota_requests.kind`: `window`, `weekly`, `rate`,
`temporary`, `effort`) become custom limits, a grant (`temporary`) or an
effort unlock (`effort`, for one model or all models: that level and every
level below it); 5-hour and weekly requests within the site's
automatic-approval amounts are approved at once. An automatically approved
amount is a floor (`user_limit_overrides.automatic`, migration 11): the
account gets it or the policy × tier, whichever is higher, so it never holds
the account below later promotions; amounts an administrator sets (or approves)
are exact and may be lower. A request needs the limit it
raises to be on, and approval is refused once an administrator switched that
limit off, since a custom value would switch it back on for one account.
Migration 9 converted credits (1 credit = 1,000 tokens) and kept the credit
columns of earlier releases.

A request's `model_id` also targets one model's own limits for the `window`,
`weekly`, `rate` and `temporary` kinds (approval sets `user_model_limits`, or a
grant for that model). Requests can be withdrawn (`status='cancelled'`).

**Community consent** (`services/community.py`, board in `web/community.py`,
migration 14): a request sent with `community=1` is open for votes until
`community_until`. Each person has one row in `quota_request_votes` (support or
object; a support may renounce `tokens` in the request's `scope` of its `pool`
or `model_id`, capped by `community_max_pledge_percent` of their own base limit
across everything they hold). `community.consent` counts supporters, the share
in favour and the renounced tokens against the increase ×
`community_coverage_percent`; when reached, `community.apply` grants the
increase for `community_boost_hours` (a temporary request: its own hours; an
effort request unlocks the level) with `resolution_source='community'`, and the
supporters' pledges get `starts_at`/`ends_at`, which `limits.effective` and
`limits.model_limits` subtract from their limits (reason `renounced`). An
administrator's decision, a cancellation or the `community-quota` job closing
expired voting release unapplied pledges (`released_at`); a closed request
stays pending for administrators.

**Quota fallback** (`limits.quota_fallback`, chat only): a model chosen by
name that is blocked for the account switches to a usable candidate, the same
kind (local/cloud, `limits.is_local`) first, then the other kind when
`quota_fallback_to_local`/`quota_fallback_to_cloud` allow it; unblocked choices
get fallbacks of the other kind for failures before any output. The API keeps
named models strict.

## Personalities

A personality (`db/personalities.py`, rules in `services/personalities.py`,
pages in `web/personalities.py`) is text layered *below* the model's own
system prompt: `services.chat.system_prompt` appends it with the note to apply
it only where it does not conflict with the instructions above, so neither
users nor administrators can override a model's system prompt with one.
Its response style never passes raw options: the length becomes one short
instruction and the creativity a temperature derived from the model's
administrator setting (`style_temperature`, within 0–2); a temperature the user
sets for a message still wins. Avatars are validated single emoji, accent
colours are keys of a fixed palette (`static/css/personalities.css`), and the
preferred model is only selected in the browser when a chat starts, if the
user may use it.

Rows have a `kind`: `user` personalities are private to their owner;
`featured` ones are published by administrators for everyone (read-only, users
copy them) and are stored under the administrator who created them — a
trigger from migration v7 hands them to another administrator when that
account is deleted. Share links, "Duplicate" and JSON import always create
copies; every path goes through `services.personalities.clean`, so forms,
copies and files share one set of limits. A share link works only while the
personality is not disabled by an administrator and its owner is neither
suspended nor without the `custom_personality` capability; the shared page names
the preferred model only to viewers who may use it. `personality_defaults` records the
personality new chats start with. Use of any kind requires the
`custom_personality` access policy.

## Agents

`services/agents/` runs coding agents (see [Agents](agents.md)): `loop.TaskRun`
is one background thread per task run that calls the model through
`inference.generate` with `tools` (request type `agent`, lowest queue priority,
never on worker PCs) and turns validated tool calls (`tools.py`) into calls to
the sandbox runner (`runner.py`). Task leases and Stop requests use
`supervisor.register_watch`; `db/agents.py` holds the SQL. No agent code ever
runs on the web server.

## Tests

`tests/app/` holds the application tests. Fixtures (`tests/app/conftest.py`)
build an app on a temporary instance directory with an imitation Ollama
server (`tests/app/fake_ollama.py`) and provide a `Browser` that handles CSRF
and sign-in. `tests/fixtures/legacy_v4.sql` is a database written by the
previous release; upgrade tests start from it.
