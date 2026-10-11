# Changelog

## 1.7.0 (unreleased)

### Updating

Run `sudo bananachat update`. The installed updater backs up the server,
starts 1.7, checks its health and restores the old code and database together
if anything fails. On first start the database is upgraded in place
(schema version 25); accounts, chats, attachments and API keys are kept, and
so are sign-ins (on HTTPS servers, for browsers used within a day).
Read [UPGRADING.md](UPGRADING.md) before updating. In short: the Claude
subscription connector is gone (offer Anthropic models through the Anthropic
API provider), changing a password now revokes the account's API keys unless
the box is unticked, attachment limits now count the text stored from
documents (some chats or accounts may be over them after the update),
restoring onto a new server needs `--trust-source`,
backup snapshots made by 1.6 need `--allow-unauthenticated`, automatic updates
need signatures, and servers installed before 1.7 keep following their branch
until `source set --track tags`.

### Claude subscription connector removed

- The Claude Code subscription connector and its account pool were removed.
  They pooled consumer Pro/Max subscriptions to serve many users, which
  Anthropic's terms do not allow. Offer Anthropic models through an external
  API provider with an Anthropic API key (Admin → Models → External APIs).
  Development builds after 1.6.0 also had Bubblewrap sandboxing, subscription
  discovery, usage reading and live validation for the connector; all of that
  is gone too.
- The upgrade (migration `v21_remove_claude_connector`) retires the models the
  connector served: they are hidden from model pickers and `/v1/models`, and
  the API refuses them. Existing chats keep their history and model names.
  Personalities that preferred them use the default model. The connector's
  account, lease and usage tables and its stored state are deleted.
- Leftover `BC_CLAUDE_EXTENSION`, `BC_CLAUDE_CODE_CONFIG` or other
  `BC_CLAUDE_*` settings are ignored; BananaChat starts normally and logs one
  warning until they are removed. Managed installs no longer give the service
  write access to Claude profile directories.
- Removed docs/claude-code.md, docs/claude-code.example.json and
  docs/claude-router.md. The `provider_capacity_unavailable` API error is no
  longer produced: busy providers answer `provider_rate_limited` or
  `provider_overloaded`.

### External API providers

- Administrators configure OpenAI-compatible and native Anthropic API providers
  with base URLs, private server credentials, connection checks, model selection
  and optional automatic enrolment. Published models use BananaChat's existing
  chat, playground, agents and public API, alongside local models.
- Provider-scoped IDs prevent collisions. API keys share their owner's budgets
  and rate buckets. External presets enforce five-hour model limits before and
  after publication; weekly limits remain optional. Model capabilities are
  explicit, and fresh reasoning access is enforced before provider execution.
- Connections pin verified DNS destinations, require HTTPS for public endpoints,
  block metadata addresses and never forward credentials through redirects.
  Structured streams require terminal success; cancellation, usage and tools
  retain the existing accounting and authorisation rules.
- Hosted models remain available during a local-server outage. Migration 19
  adds provider metadata and model bindings while preserving existing data.
  See [provider setup](docs/external-providers.md) for compatibility options.
- Preserve hosted strict/custom quotas during publication and preset changes;
  permit hosted inference during local memory pressure; harden metadata address
  blocking and preserve model ID sequences during schema upgrades.
- Service presets for Anthropic, OpenAI, OpenRouter, Google Gemini, Mistral,
  Groq, DeepSeek, xAI, Together, Fireworks, Azure OpenAI (v1 endpoint,
  `api-key` header), LM Studio, vLLM, llama.cpp, LiteLLM and any other
  OpenAI-compatible service. A preset fills in the base URL, API format, key
  header and suggested extra headers (OpenRouter `HTTP-Referer`/`X-Title`);
  every value stays editable. Existing providers get a preset inferred from
  their address and keep their key header (migration
  `v23_external_provider_presets`).
- **Test connection** checks the key (OpenRouter through `/key`) and the model
  list and can stream a short test answer, reporting whether the provider
  sends token usage. Failures say what went wrong (rejected key, permission,
  credit, model, URL, rate limit, timeout) and quote the provider's message
  with keys removed.
- The provider page lists every model the provider returns with context
  window, maximum output, prices and features; select models and add them
  (optionally published at once) with the capabilities the list states. A
  model is withdrawn only after two listings in a row omit it, not after one.
- Streaming: Claude adaptive thinking shows its reasoning summary, older
  Claude models use a thinking budget, newer stop reasons are accepted, and
  sampling parameters are sent only where enabled per model (Claude models no
  longer get `temperature`/`top_p` by default). OpenRouter reasoning,
  DeepSeek/vLLM `reasoning_content`, Mistral thinking chunks, Groq `x_groq`
  usage and xAI reasoning tokens are handled; tool-call arguments reach agents
  decoded. Silent reasoning may last up to `BC_FIRST_TOKEN_TIMEOUT`. **Max**
  effort is sent as `xhigh` to OpenAI-compatible APIs.
- Agents on Claude models keep extended thinking through tool rounds. Each
  step's signed thinking blocks go back unchanged with its tool results, and
  thinking is requested again. They are sent only while Claude still accepts
  them: same system prompt, tools, model and provider settings, earlier steps
  not shortened, and at most 300,000 characters of thinking per round;
  otherwise the round continues without a thinking request, as before.
  Signatures stay in the running task's memory and are never stored. Expect
  more output tokens per agent step on Claude.
- Limits and credits use the provider's token counts, including for
  interrupted answers (the input count already reported is charged).
- Provider failures have clear codes: the API returns `provider_rate_limited`
  and `provider_overloaded` (503 with the provider's `Retry-After`),
  `provider_unavailable` (503), `provider_rejected_request` (400),
  `provider_misconfigured` (502) and `model_not_found` (404). The chat and the
  API playground explain them in the reader's language (English and Italian).
- Provider API keys are encrypted at rest with a key derived from the instance
  secret (`BC_SECRET_KEY` or `.secret_key`); key files from older versions are
  encrypted the first time they are used. New dependency: `cryptography`.
- Fixed: Gemini models enrolled with 1.6 (`external:<n>:models/gemini-…`) keep
  working after the upgrade, with their public ID, publication, limits and
  access. They are now called by the bare model ID, and automatic enrolment
  no longer publishes a second copy with default limits (migration v23). A
  provider that gets the Gemini preset later (one reached through another
  host) has its models renamed the same way.
- Answers whose provider reports no usage are charged the same way whether
  they complete or not: input, output, and the estimated reasoning the model
  did not stream. While streaming, the provider's settings are reread every
  2 seconds and before the final chunk, instead of on every chunk.

### Two-step verification

- Any account can turn on authenticator-app codes (TOTP) on Account → Two-step
  verification, by scanning a QR code or typing the key, then confirming with
  a code and the password. Ten one-time recovery codes are shown once and
  stored only as digests. Signing in then asks for a code after the password.
  Wrong codes are limited per account and per browser that signed in before
  (10 per 5 minutes, 50 per day), so someone who knows the password cannot
  lock the owner out. New recovery codes and turning it off take the password
  and a code. API keys do not ask for a code; turning two-step verification on
  offers to revoke the account's API keys, ticked by default. See
  [two-step verification](docs/two-step-verification.md). New dependency:
  `qrcode`.
- Administrators can require it for every administrator (Admin → Site
  settings → Sign-in security, off by default), and reset it for an account
  that lost both its app and its codes; the reset is audited, signs that
  account out and offers to revoke its API keys (ticked by default). Admin → Users has a **Two-step verification** column (On, Off,
  or "Off, required" for administrators who still have to set it up).
- High-impact administrator actions ask for the administrator's password, or
  an authenticator code, unless that browser signed in or confirmed in the
  last ten minutes: the database export, resetting another account's password
  or two-step verification, role changes, creating administrator accounts or
  invitations, suspending, reinstating or deleting an administrator,
  registering a worker, adding, changing or removing any external provider
  (with or without an API key; removing one deletes its key), opening sign-up
  to everyone, and changing the requirement. Confirmations and failed attempts
  are recorded in the audit log. Every administrator action is classified in
  `web/admin/_reauth.py`, and a test fails when a new one is added without a
  decision. When the ten minutes run out while the workers page is open,
  registering a worker shows the confirmation field instead of only an
  error.
- New server command `sudo bananachat reset-two-step USERNAME` (single or web
  server) turns two-step verification off for an account that lost its app
  and recovery codes, for example the only administrator. It signs that
  account out everywhere, revokes its API keys (reported as
  `api_keys_revoked`) and is recorded in the audit log as
  `operator.user_two_step_reset`. Without the managed installer, run
  `python -m bananachat.manage reset-two-step USERNAME` as the service user
  with the service's environment. It never creates or upgrades a database.

### Sign-in, sessions and API keys

- Sign-in throttling no longer locks out people who share a public address
  (school or office NAT, mobile CGNAT, VPN). A browser that signed in to an
  account before has its own rate and failure limits, so failures from
  strangers on the same network cannot block it. Only failed sign-ins count,
  and the account lockout is reserved atomically before the password check,
  so parallel guesses cannot exceed it. New settings:
  `BC_LOGIN_ATTEMPTS_PER_MINUTE` (60), `BC_LOGIN_FAILURES_PER_ADDRESS` (20),
  `BC_LOGIN_FAILURES_PER_ACCOUNT` (10), `BC_SIGNUPS_PER_HOUR` (30, accounts
  created) and `BC_SIGNUP_FAILURES_PER_HOUR` (20). They replace the fixed 30
  sign-ins per minute and 10 sign-ups per hour. A warning is logged when a
  same-host reverse proxy forwards requests while `BC_PROXY_MODE` is off.
- API keys can be read-only (they list models; anything that generates gets
  `403 insufficient_scope`) or have full access, and can expire after 7, 30,
  90 or 365 days. Changing the password, or signing out all other devices,
  also revokes the account's API keys unless *Also revoke my API keys* is
  unticked; the box is ticked by default. Turning on two-step verification, an
  administrator's *Sign out everywhere* and an administrator's two-step reset
  offer the same, and an administrator's revocation is audited as
  `admin.user_api_tokens_revoke_all`. Existing keys keep full access and
  never expire (migration `v22_api_token_scopes`). An account's "Active API
  keys" in Admin → Users no longer counts expired or revoked keys.
- With secure cookies (`BC_SECURE_COOKIES`, on by default behind the managed
  HTTPS proxy), the session and device cookies use the `__Host-` prefix, so
  other subdomains of the same domain, such as hosted wikis, cannot plant a
  session. Sign-ins from before the switch move to the new cookie only during
  the first day after it, only in the browser that signed in (a copy planted
  in another browser ends that session), once, under a new token, and one per
  account and User-Agent; otherwise people sign in again. A moved session
  counts as not recently confirmed. During that first day, someone who
  controls a sibling subdomain and kept their own pre-update cookie can still
  sign one fresh browser with exactly the same User-Agent into their account
  (one per account they hold); UPGRADING.md explains how to sign everyone out
  at the update instead.
- Invitations: custom codes need at least 12 letters or digits, at least 6 of
  them different. Administrator invitations must expire within 30 days and
  have a limited number of uses (1 by default). Invitations created under the
  old rules keep working but are marked **Weak** in Admin → Invitations.
- Personal data export: API keys show their scope, expiry and whether they
  have expired, and a `two_step` block says whether two-step verification is
  on, since when, and how many recovery codes are left. The authenticator
  secret and the recovery codes are never included.

### Chats and usage accounting

- Fixed: deleting or erasing a chat while it answers (no-history chats,
  `BC_DELETED_CHAT_RETENTION_DAYS=0`, "Delete all my chats", administrator
  deletion) no longer skips charging. The run's lease outlives the chat, and
  the run, or recovery if its process died, charges what was generated. Run
  leases now record their account (migration `v25_run_owner_worker_backoff`).
- Fixed: deleting an account while one of its answers is generating now stops
  that answer at once, in whichever server process is generating it; its
  agent tasks stop too.
- Fixed: replacing, reloading or stopping a Gunicorn worker no longer kills
  chat answers whose browser left, blocks the inference queue for 90 s, or
  leaves API and playground answers uncharged. A replaced worker gives running
  answers up to 45 s to finish (less when `timeout` leaves less room); on a
  stop or reload the hand-over happens shortly before the master's
  `graceful_timeout`, even while open streams keep the worker busy. Chat
  answers still running are saved as interrupted answers and charged; API and
  playground answers end with `503 server_restarting` and are charged for what
  they produced. Each API and playground answer records its usage every
  5 seconds (migration v25, table `completion_leases`): if the process is
  killed first, another process charges the last record, and nothing is
  charged twice. The worker frees its inference queue places once all its
  answers were handed over, and the master lets the places of a crashed worker
  stop counting within seconds. A worker is no longer replaced by
  `max_requests` before it has run an hour, so floods of cheap requests no
  longer force early replacements (and an answer cut by one is still
  charged).
- API: two new completion error codes, `503 interrupted` (another server
  process already charged a stalled answer) and `503 busy` (image decoding is
  busy, with `Retry-After: 10`); see [the API guide](docs/api.md).
- Fixed: the one-answer-per-account rule also counts an answer whose chat was
  erased while it was still generating.
- Security: one user can no longer withdraw models from everyone. Failures
  caused by the request (a 4xx refusal, input or an image the model server
  cannot read) and the fallback attempts of a request that already failed no
  longer count towards marking a model failing. Images sent to the API are
  decoded first, and an undecodable one gets `400 invalid_image`.
- Interrupted answers are charged more accurately. Each image counts as 765
  tokens and Chinese, Japanese and Korean text as about a token per
  character. External reasoning models that do not stream their reasoning are
  charged 20 tokens per second for the time before their first output, within
  the output limit. An external request stopped or abandoned after it was sent
  is charged at least its input. Recovered answers are charged with the usage
  of their last checkpoint. [docs/external-providers.md](docs/external-providers.md)
  explains why this is an estimate rather than an advance reservation.
- New setting `BC_CHAT_MAX_ACCOUNT_ATTACHMENT_MB` (default 1024): the total
  attachment storage of one account; deleted chats count until erased and
  administrators are exempt. Uploads over it are refused with
  `413 storage_limit`. This limit and `BC_CHAT_MAX_SESSION_ATTACHMENT_MB` count
  what is stored: the text extracted from a document and the stored image,
  not the uploaded file (a 1.5 KB PDF can hold 200,000 characters of text).
  Existing attachments are measured during the upgrade
  (`chat_attachments.stored_bytes`, migration v25).
- New setting `BC_CHAT_MAX_IMAGE_DIMENSION` (default 2048): image attachments
  are stored and sent at most this many pixels on their longest side, and
  large JPEGs are decoded at a reduced scale. Each process decodes at most two
  images at a time, which bounds the memory uploads take, and shares them
  fairly: each account decodes one image at a time, for chat uploads and API
  images alike. A request waits at most 3 s behind its own account, or 10 s for
  a free place, before it is refused as busy (the API answers `503 busy` with
  `Retry-After: 10`).
- Shared chat links stop working while the owner's account is suspended or
  deleted. Administrators can stop sharing a chat without deleting it
  ("Stop sharing" in the chat audit, recorded as `chat.admin_unshare`), and
  that lasts: the owner cannot share the chat again (`403 share_blocked`,
  translated) until an administrator chooses "Allow sharing", recorded as
  `chat.admin_allow_share`. Blocked chats show "Sharing blocked" in the chat
  audit (`chat_sessions.share_blocked_at`, migration v25). The block also
  applies when the owner turned sharing off just before the administrator's
  click, and a chat that is not shared can be blocked in advance
  (**Block sharing**).
- When a link's text names a different website than its real target, as in
  `[https://bank.example](https://evil.example)`, the real host is shown next
  to it. The text may name the site anywhere among other words or after a
  URL, as an IPv4 address, or with look-alike dots (fullwidth, one-dot leader,
  ideographic, Arabic full stop, Arabic-Indic zero), fullwidth letters,
  invisible characters (zero-width space, soft hyphen) or combining marks
  drawn into the underline or over a dot.

### Volunteer workers

- Workers that time out, fail or break the protocol are paused: no jobs or
  routed requests for 1 minute, doubling to 1 hour, cleared by a completed job
  or by enabling the worker again. A job is never offered back to the worker
  that handed it back, returns to the pool at most 3 times, and is answered by
  the local server once the claim and first-token timeouts have passed. The
  worker page shows "paused after failures".
- One account can no longer pause every worker. Failures a request causes do
  not pause the worker: input the model cannot read, an Ollama 4xx refusal,
  and memory a large `num_ctx` or context needs. An account's jobs hold at
  most one worker responsible per 30 minutes, and an account whose job a
  worker was blamed for is answered by the server for 30 minutes; after a
  failure held against its request, the account is not sent to that worker
  for 30 minutes and keeps the others. The worker reports why a job failed, so
  a worker on which the jobs of three accounts failed that way within
  30 minutes, without an answered job in between, is paused all the same. A
  worker that took over a handed-back job late is not blamed when the pool's
  time runs out, and neither is one whose lease ran out while the server was
  down. Jobs record their account and who a failure was held against
  (`worker_jobs.requested_by`, `fault`, migration v25).

### Compute servers

- Model management (pull, create, copy, delete) on the compute gateway can
  need its own token. New compute installations create it
  (`data/.compute-management-token`, `BC_COMPUTE_MANAGEMENT_TOKEN_FILE`); the
  pairing code carries it, and the web server stores it as
  `BC_OLLAMA_MANAGEMENT_API_KEY` after checking it against the gateway. The
  main token alone then cannot change the models. `compute rotate-token`
  replaces both tokens and adds the management token to compute servers
  installed before 1.7. `backend connect --management-token-file` and
  `install --backend-management-token-file` set it without a pairing code;
  `status` shows its fingerprint. `BC_COMPUTE_MODEL_MANAGEMENT=0` refuses
  model management with any token.
- After 20 wrong tokens in a minute from one address, further wrong tokens get
  429; the right token keeps working.
- Plain HTTP to the checkpoint agent and the sandbox runner is no longer tied
  to Tailscale. `BC_CHECKPOINT_AGENT_INSECURE_NETWORKS` and
  `BC_AGENTS_RUNNER_INSECURE_NETWORKS` list the private networks (CIDR) where
  it is accepted: a LAN, WireGuard, Tailscale or a container network. Only
  loopback, RFC 1918, `100.64.0.0/10` and IPv6 unique local ranges may be
  listed; public and link-local ranges, `0.0.0.0/0` and `::/0` are refused with
  a warning. Only IP addresses in the URL match; host names still need HTTPS.
  `BC_CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE` and
  `BC_AGENTS_RUNNER_ALLOW_INSECURE_TAILSCALE` keep working as deprecated
  aliases that add Tailscale's ranges (and still accept `.ts.net` names).
  Both clients log once per address that their token travels without TLS
  (the runner did not, and an upper-case `HTTP://` skipped the warning), and
  the sandbox runner's start-up warning on a non-loopback address says what
  a private-network setup needs.

### Agents

- The post-start sandbox check verifies dropped capabilities,
  no-new-privileges and the non-root user on Podman too, not only on Docker.
  CI also runs the real-engine sandbox tests on Podman.
- Sandboxes start on Podman 4: the workspace mount used `uid=`/`gid=`
  options that Podman refuses ("unknown mount option"); Podman now gets
  its `U` option, which mounts the workspace as the sandbox user.
- A command that times out, floods its output or is interrupted can no
  longer leave processes running when they cannot be stopped: the runner
  used to count a refused kill as done. On Ubuntu 24.04, Podman's AppArmor
  profile refuses signals between the processes of different commands, so
  there such a command now ends its sandbox (the log says why). Podman
  sandboxes are also removed at once instead of after a 10-second wait.
- Claude thinking through tool rounds: see External API providers above.

### Operations, backups and releases

- Managed updates follow release tags. New installations deploy the highest
  signed `vX.Y.Z` tag (`source set --track tags|branch`); existing servers
  keep their branch. `updates enable` requires `--require-signatures` (with
  release tags, an annotated tag signed for that version by an allowed key; a
  server that follows a branch needs signed commits), or an explicit
  `--allow-unsigned`; `status` warns while automatic updates deploy unsigned
  code.
- Security: with signatures required, a server that follows release tags
  deploys only an annotated tag SSH-signed for its own name. A signed commit
  under a lightweight or unsigned tag releases nothing. The highest release
  that verifies is deployed; unverifiable higher tags are skipped and
  reported (`skipped_releases`, and a warning). Only the 20 highest tags are
  tried, so let only maintainers create `v*` tags on the update source.
- Fixed: restore and rollback on a server that requires signatures accept a
  release whose tag is signed, not only signed commits, and the refusal says
  how to proceed. A restore onto an installed server keeps its update source
  settings, repository credentials and allowed signers (`source_warning` when
  the package's differ).
- Fixed: remote backup retention counts only authenticated snapshots and never
  deletes while fewer than `--keep` would remain. Future-dated or unparsable
  snapshot IDs are neither counted nor deleted and no longer break uploads.
  `--allow-unauthenticated` refuses snapshots that cannot predate
  authentication: dated in the future, or at or after the series' first
  authenticated snapshot, which every authenticated snapshot records (so
  retention deleting it changes nothing).
- Fixed: updates and restores refuse a 1.7 or later release without both
  dependency locks unless `--allow-unpinned` is given, also on
  `backups restore`, so a remote snapshot of such a release no longer has to
  be downloaded and restored by hand. Releases from before 1.7 install their
  ranges, with a warning.
- Hash-pinned dependencies. The managed updater installs `requirements.lock`
  and `requirements-build.lock` with `--require-hashes`, wheels only, and
  refuses source builds also for releases without locks. `psutil` is bounded
  below 8, and `scripts/lock_requirements.py` regenerates and verifies the
  locks for CPython 3.12 to 3.14. CI installs from the locks too.
- Security: encrypted Git backup snapshots are authenticated with an HMAC
  keyed from the recovery key (the same format as BananaWiki), checked before
  anything is decrypted. Anyone who could push to the backup repository could
  previously forge a snapshot whose restore ran their code as root. Snapshots
  made before this release are refused unless `--allow-unauthenticated` is
  given, which prints a warning.
- Security: a restore no longer deploys a package's bundled source unchecked.
  It reuses a release the server already prepared, or builds the revision from
  the server's own update source (signature-checked when signatures are
  required). When neither has the revision, it stops, shows the revision and
  asks for `--trust-source REVISION`, available on `restore`, `rollback`,
  `install --restore` and `backups restore`.
- Backups: new `backups configure --append-only` (uploads never delete
  snapshots, so the token needs no delete right and the branches can be
  protected on the forge) and `backups prune [--token-file FILE]` (applies
  retention, with a deleting token used only for that command).
- Backups: an upload named like a database (`notes.db`, `Thumbs.db`) no longer
  makes every package unrestorable. Only the application's own database is
  integrity-checked.
- Updates: packages taken before an update, restore or legacy import no longer
  compress the model weights. The weights stay in place and rollbacks keep
  them, and a restore that fails puts back the weights it replaced. Rollbacks
  move the extracted data instead of copying it again. Updates, restores and
  backups check for enough free space before stopping anything. Restoring a
  weight-free backup on the same server keeps the models it already has.
- The standalone compute server's model restore (`bananachat models`) and its
  model listing refuse an Ollama address that is not `http://` or `https://`;
  before, a `file:` address would have been read.
- Version 1.7.0.

### Development and documentation

- CI runs Ruff's flake8-bandit security rules. Failing cancellation callbacks
  and NVML or checkpoint-download cleanup errors are now logged instead of
  silently ignored.
- CI pins Node (22.11.0), and the Markdown sanitiser tests fail instead of
  skipping when Node is missing (`BC_REQUIRE_NODE=1`); they work on Node 18
  and 20 as well.
- README: providers (Anthropic, OpenAI, OpenRouter and other
  OpenAI-compatible services, with presets and encrypted keys), two-step
  verification, installation from the latest release tag, and a Maintainers
  section: started by Luca Zani; maintained by Luca Zani and contributors
  (the project's origin is told on the BananaWiki website's about page).
- New [UPGRADING.md](UPGRADING.md). The API guide describes interrupted-answer
  accounting correctly, and `.env.example` lists `BC_AGENTS_GIT_PROXY`.

### Interface review

- Sign-in, sign-up, two-step and setup errors now appear inside the form card.
  They are announced to screen readers and tied to the field to fill in; they
  no longer show as a toast in the corner.
- Interrupted answers have a **Try again** button (*Riprova*) while they are
  the newest message. It sends the question again through the normal send
  path, with the same limits and charging; a draft and staged files stay in
  the composer, and the interrupted answer stays in the history.
- Chat on phones: the top bar keeps its height, the no-history header fits,
  images no longer push the latest answer out of view, and service notices
  take less room above the conversation. On wider screens a one-line notice
  is centred on its Details and Hide buttons.
- Tables on phones, such as API keys, usage, invitations, audit, catalogue and
  personality moderation, show each row as a labelled card with its actions
  visible.
- Accessibility: the light-theme gold text on tinted badges and alerts now
  meets WCAG AA. The letter-spacing preference also reaches form controls and
  headings. Customisation choices never leave a single option alone on a row,
  and switches in the off state are clearly visible. Small links, check boxes
  and disclosures have larger touch targets.
- Administration: disclosures (personality instructions, invitation usage,
  limit editors, agent reasoning and tool calls, metrics tables, worker models,
  external provider settings and model cards) use the chevron of the API and
  personality pages instead of the browser's triangle.
- English: the interface and the documentation use British spelling
  throughout, as BananaWiki does: Customise (menu and page title), enrol and
  enrolment (models, external APIs, the music programme), catalogue,
  programme (the music scheme) and towards. Addresses, settings and stored
  values keep their names (`/customize`, `enrollment`).
- Italian: the reset times read naturally whether they show only a time or a
  full date ("I limiti si ricaricano tra 4 h 59 min (…)"). Quotation marks are
  consistently «…», and "5 ore" no longer splits across lines. Dates in
  sentences no longer break at a hyphen.

### Cloud sessions and production hardening

- Add explicit administrator-only, everyone-signed-in and existing-access-list
  audiences for cloud coding sessions. New installations keep sessions disabled
  and select administrators only; upgrades preserve enabled access policies.
- Keep session history, workspace files, patches, follow-ups, cancellation and
  runner limits in the existing isolated agent subsystem.
- Recheck fresh account, audience, model and token permissions after queue waits,
  on each model attempt and before tool work; respect tightened run budgets.
- Keep the five-message pending follow-up bound under concurrent requests.
  Resuming a session queues its message, validates current access and takes its
  lease/rate admission atomically; refusals and storage failures leave those
  changes unapplied. Automatic continuations cannot debit a losing lease or
  override a newer stopped or failed run.
- Reserve repository imports under the same database transaction as fresh
  access/model checks, start admission and task creation. Concurrent starts obey
  the per-user import limit, including administrators; quota/capacity refusals
  and failed task insertion leave no task or rate debit and launch no work.
- Show enrolled external tool-capable models in cloud-session administration.
- Reject IPv6 transition routes that could bypass provider metadata-address
  restrictions, including trusted-private provider configurations.
- Keep invalid backend URL or bearer-token settings from crashing startup or
  sending credentials to an unintended default service; pages remain usable
  while an operator corrects the reported setting.
- Recheck registration policy after password hashing so sign-up closure and
  invitation requirements take effect before an in-flight account is created.
- Refuse malformed or incomplete Ollama inventories without withdrawing existing
  models, and reject corrupted streams or invalid usage as controlled backend
  failures instead of reporting a completed answer.
- Apply backend response deadlines, Stop cancellation and read-gap timeouts to
  sockets retained by connection-close responses; trickling headers, bodies and
  error replies cannot hold requests indefinitely. Nested deadlines keep
  independent ownership.

### Model-server offline warnings

- Administrators can show or hide offline model-server notices in Site settings;
  the setting is enabled by default and upgrades preserve existing data.
- Users can hide these notices for 24 hours in their current browser. Reloads,
  status polling and other tabs keep the original expiry; separate accounts
  keep separate choices. Blocked storage falls back to the current page.
- Hidden notices leave health reporting, permissions and inference safeguards
  intact. Hosted external API models can continue during a local compute
  outage.
- Open pages pick up global notice changes without changing sending state.
- Version static assets together so an ordinary reload after an update loads
  current scripts, relative module imports and styles. Legacy links revalidate;
  dynamic pages, including public sign-in/setup forms, remain uncached.
- Keep active database transactions intact when another app instance selects
  the same database; only a different database invalidates connections.

### Interface refinement

- Keep deep-linked settings fields fully visible after mobile browsers adjust
  the initial scroll position, while allowing users to scroll away normally.
- Chat keeps model, reasoning and personality choices together before secondary
  tools. Pickers share keyboard and dismissal behaviour, show the selected item
  clearly and recover from invalid saved reasoning preferences.
- Quota forms group token accounting and limits before optional demand and
  reasoning settings. Shared template fields and a route-free quota UI helper
  keep validation and administration views consistent.
- Account usage summaries accommodate three services. API credentials are
  labelled as keys in English and Italian, distinct from model token usage.
- Primary button labels follow the selected colour's contrast instead of the
  theme. Disabled controls no longer brighten on hover. Landing conversation
  and model-selection screenshots show the current interface.

### Account and model controls

- Internal token and request-rate exemptions can be set independently for an
  account or for an account's own model limits. Models also have separate token
  and rate switches. Provider exhaustion, access rules, model locks and usage
  recording remain enforced. All four second/minute/hour/day model rate rules
  survive form saves.
- Reasoning supports a distinct Extra tier (`xhigh` API alias) between High and
  Max, restricted to each model's declared capabilities. Model gating and
  model/account automatic-unlock opt-outs are independent. A model with no
  permitted default effort is refused instead of bypassing its ceiling.
- Model enrolment installs presets before publication. Download cancellation,
  stale worker cleanup, backend deletion and cancelled-model discovery are
  coordinated so an old task cannot publish or delete a replacement task's
  model. Fallbacks register their running model before checking deletion state.
- Migrations 17 and 18 preserve existing data while adding provider leases,
  quota freshness, independent exemptions and the Extra effort tier.

### Single token allowance

- Services now use one five-hour token allowance, with optional weekly
  limits. The separate slow-token allowance and spillover are retired from
  settings, account displays and quota requests; exhausted allowances refuse
  further counted requests until the reset or an approved increase.
- Migration 16 combines the previous regular and slow allocations when the
  site's slow tokens were enabled. Disabled sites keep their regular amounts.
  Historical regular and slow usage count together without resetting open
  windows. Legacy columns remain inactive for compatibility and rollback.
- Request rates, model limits and weights, tiers, dynamic adjustment, grants,
  music bonuses, reasoning settings and appearance customisation remain
  supported. Explicit slow/normal/fast account priorities remain available;
  consuming tokens no longer changes a request's queue lane.
- Fixed music bonuses have separate five-hour and weekly amounts, preserving
  the previous weekly bonus while combining active five-hour allocations.
- Automatic approval compares the requested total against the combined
  configured approval caps. The old exception for keeping slow tokens unchanged
  is retired; administrators can adjust the total ceiling in Admin → Limits.

### Chat token consumption

- Admin → Limits has separate consumption switches for local and cloud chats.
  Local chats are unmetered by default; cloud chats consume configured token
  allowances by default. Regular and no-history chats follow the same rules,
  including model fallbacks. Site-owned remote Ollama workers remain local.
  Enable **Apply token limits to local chats** to keep deducting existing local chat
  allowances after upgrading; the configured amounts are preserved.
- Unmetered chats keep usage history and metrics without spending service or
  model token allowances. Enabling consumption does not charge earlier free
  chats. Request rates, access rules, model locks and provider availability
  remain active; API, playground, images and agents retain existing rules.

### Security and reliability

- Sign-in, password changes and account/chat deletion recheck credentials and
  session revocation inside the database transaction. A concurrent password
  reset cannot be overwritten by automatic password rehashing.
- Invalid CSRF and bot tokens return controlled errors. JSON documents have a
  nesting bound and reject non-finite numbers; malformed preferences use safe
  defaults instead of causing server errors.
- Queued and fallback completions recheck the current model, access, reasoning
  effort, quotas and request rates. They use current prompts and generation
  options; interrupted fallbacks account for the model that actually ran.
- Stale inference leases cannot stop newer runs. Losing a lease prevents
  backend contact, and backend cleanup failures preserve already-saved answers.
  Terminal backend records retain their answer and reasoning content.
- Dependency audits now run in CI. Local bootstrap paths use a patched package
  installer and install runtime dependencies from wheels.
- Compute and checkpoint HTTP requests have hard header and upload deadlines,
  preventing trickled bytes from holding every connection slot indefinitely.
  Model loading, response streaming and completed checkpoint processing keep
  their existing time limits. Checkpoint shutdown retains directory descriptors
  until a blocked download has saved its restart state; cleanup is idempotent.
- Managed backups preserve required SSH commit signers. A failed restore rolls
  back its repository URL, credentials and signer trust together with the data.
- Signing out or revoking a session also retires previous-release login cookies,
  preventing a copied old cookie from creating another session. Other current
  browser sessions remain signed in; unconverted old cookies require sign-in again.
- API and playground completions report a controlled storage error when usage
  accounting cannot commit. Backend cleanup failures preserve the completed
  response or original error, and interrupted requests avoid duplicate accounting.
- Markdown delimiter and block parsing uses bounded scans so malformed messages
  cannot freeze the browser through excessive regular-expression backtracking.
- Sandbox cleanup keeps capacity reserved until containers are removed and
  coordinates deletion with reconciliation to preserve newly created sandboxes.
  Shutdown rejects new sandboxes, waits for pending creation and retries failed
  removals; the example systemd unit allows time for this cleanup.

### Interface refresh

- Model and reasoning selectors have a dedicated module, with explicit inputs
  from the chat controller. Model capabilities use a plain metadata line;
  message corners and picker headings follow the shared visual conventions.
- Collapsed settings share fragment lookup and ancestor-opening helpers,
  keeping deep links and native validation consistent across pages.
- Initial phone deep links keep the requested setting in view after the
  browser's fragment adjustment. Admin fields stay clear of the sticky header;
  the correction stops when the user scrolls or interacts.
- Chat shows named Attach, Options, Share and Send actions on desktop, and
  labels the selected reasoning setting. The default personality uses a plain
  user icon. Phones retain compact controls and touch targets.
- Customise shows common preferences and the preview before expandable colour,
  highlighting and background controls. Optional API limit explanations and
  administrative settings open on request; deep links reveal their section.
- Account navigation uses short section names and a compact profile form.
  Administrative monitoring and quota details no longer dominate their pages.
- A more compact visual system across chat, personalities, images, agents,
  account, customisation, API, music and administration: quieter surfaces,
  smaller radii, lighter cards, consistent typography and subtle borders.
- Enabled tools are direct links in the header. Navigation switches to one
  menu only when the labels run out of room, including with larger text or
  another language. It closes on outside clicks and Escape returns focus.
- Account and customisation settings use separated sections instead of stacked
  boxes. The account navigator tracks the section being read.
- Ordinary pages share one 1120 px frame. Account and customisation settings
  span the page, with section links above them and the preview below. API and
  administration use the same frame and shared form controls;
  personalities use readable rows, with distinct work areas for images and agents.
- Customise has a labelled header shortcut on desktop and a compact icon on
  phones. Account menus indicate the current page. Administration links are
  grouped by purpose, with a compact section switcher on phones.
- Chat uses a flat composer without a glowing focus border, aligned message
  metadata and actions, wrapping tools on small screens, and a 250 px default
  history column. Saved custom widths
  are retained. Shared controls have consistent sizes and larger touch targets;
  chat notifications stay clear of the composer.
- Customisation has section shortcuts and aligned reading and colour controls.
  Personality templates use compact rows with clear selection controls.
- API quickstart examples expand individually, with the first example open.
  Playground sampling parameters collapse separately and reopen to focus
  invalid values before a request is sent.
- Preference saves, resets and background changes run in order, so delayed
  requests cannot overwrite newer changes. Native validation opens collapsed
  settings before focusing an invalid field.
- Site palettes, light and dark themes, text scaling, contrast controls,
  reduced motion, security policies and existing form actions are preserved.
- Server notices use short explanations and a plain status line without pulse
  effects. Cloud account forms use labelled controls and confirm account removal.

### New icons

- The favicon and the logo are BananaWiki's new banana: the wiki mascot's
  pixel art with its smile, every pixel a sharp square (the mascot itself
  stays in BananaWiki). `static/img/favicon.png` keeps its name and size
  (32x32); the new `static/img/apple-touch-icon.png` (180x180, on a light
  square) is the icon phones and tablets put on their home screen.
- The logos in the header and on the sign-in pages are 32x32, twice the pixel
  art, and scaled without smoothing, so they stay sharp on high-density
  screens. BananaWiki's `scripts/make_icons.py` draws both files;
  `tests/app/test_icons.py` checks them against BananaWiki's when that tree
  sits next to this one.

## 1.6.0

This release is a complete rewrite of the application for its open-source
release. It updates existing installations in place.

### Updating

Run `sudo bananachat update` (or let automatic updates pick it up). The
previous release's updater backs up the installation, starts the new version
and checks its health before letting users in; if anything fails it restores
the old code and database together.

On first start the database is upgraded in place. The upgrade keeps accounts, chats, attachments, API tokens, quotas, access
policies, models, settings and sign-ins; no configuration
change is required.

- **The former product name is gone.** Sites installed while the project was
  called BananaAI still had "BananaAI" stored as their site name, which every
  page title and the navigation showed. The upgrade renames exactly that value
  (any spelling) to BananaChat; a custom site name is kept.
- Existing sign-in cookies keep working once and are converted to revocable
  server-side sessions.
- Split deployments: update the web and compute servers in either order.
- Deployed worker daemons keep working; updating them is recommended.
- The previous release's requests-per-minute settings, daily credits, chat limits and custom API
  quotas become the new limit policies automatically (see Limits).
- `BC_INFERENCE_OUTAGE_MODE=fallback` now needs an explicit
  `BC_INFERENCE_FALLBACK_URL`; without one the server logs a warning and
  behaves as `shutdown` (it used to fall back to an Ollama on the web server).

### Two servers by default

The documented and suggested layout is now a compute server (the cluster with
Ollama) plus a web server (the VPS); one machine running both stays available.

- `install --mode compute` prints a **pairing code**; `install --domain … --pair`
  on the web server (or `bananachat backend connect` later) tests the
  connection before changing anything and stores it (the address in the code
  is shown for confirmation first; `--yes` without a terminal). `backend status` and
  `backend test` check it at any time and `compute rotate-token` replaces the
  gateway token in one step. `--backend-url`/`--backend-token-file` still work.
- **No more Ollama on the VPS.** A web server never treats itself as the Ollama
  host: a token or a non-loopback address means a remote compute server (SSH
  tunnels included), so the VPS is no longer checked for free memory, GPU or a
  model folder, and outage detection works. `BC_INFERENCE_LOCAL` overrides the
  detection. The fallback server has no default and gets its own
  `BC_INFERENCE_FALLBACK_API_KEY`, never the compute token.
- **Use an Ollama that already runs** on a compute or single server
  (`--ollama-url http://127.0.0.1:11434`): BananaChat then never starts, stops
  or reconfigures it. Edits to a managed Ollama's `OLLAMA_HOST`/`OLLAMA_MODELS`
  survive updates and restores.

### Restoring models

After a restore the web server checks by itself which models from the backup
are missing on the compute server. If none are, nothing is asked. Otherwise one
card on the Overview offers **Download them**, **Choose…** or **Dismiss**, with
the download size when known; the same card appears when a rebuilt compute
server lacks published models. Nothing stays pending forever. The compute-side
`models` command is only needed for compute servers without a web server.

### Limits

These notes describe the limits introduced in 1.6.0. The current release's
[single token allowance](#single-token-allowance) replaces its slow-token
spillover.

The daily-quota system is now a full limit system counted in **tokens**
(prompt plus answer; amounts can be typed as `50k` or `1.5M`). Chat, the API
and agents each have an administrator-editable policy:

- **Request rates**: rules such as "60 per minute and 1,000 per day" (per
  second, minute, hour or day; every rule must pass). One account's API keys,
  playground, chat and agents share them. They never rise by themselves;
  users can ask for more. API responses carry OpenAI-style
  `x-ratelimit-*-requests` and `x-ratelimit-*-tokens` headers, and refusals a
  `Retry-After`.
- **Tokens per 5 hours** (regular, then an optional slow allowance): the
  window opens with an account's first request and refills 5 hours later.
  It can rise automatically through **tier levels** (promotion by account
  age, activity and a clean record) or by an approved request.
- **Tokens per week**, off by default: a rolling week that starts with the
  first request after the previous one ended.

**Per-model limits**, globally or per account: models have a weight (a heavy
model can count ×3), their own rate rules and token limits, and can be locked
for one account; light, standard and heavy presets set them up quickly, and a
model can be kept out of the shared service limits. When a service's tokens
are used up, `auto` moves to a model that does not count toward them.

**Reasoning effort tiers** for reasoning models (off, low, medium, high, extra, max):
everyone gets up to medium; higher levels are unlocked by an administrator, by
a request from the account page or automatically after sustained use, and
unlocking a level unlocks every level below it. The chat composer shows locked
levels with a link to request them; the API answers
`403 reasoning_effort_locked`. Effort gating can be switched off, and levels
locked or unlocked, for everyone or one account.

Limits can also adapt **dynamically** to current demand (queue load and how
many people are online), to each person's usage pattern and, for heavy models,
more strongly (0.5×–2×, explained on the account page; approved custom limits
are never lowered). Administrators can create **grants** (unlimited, a
multiplier or extra tokens, for one person or everyone, for one service or one
model, from one hour to forever — for example unlimited use during a coding
jam; an unlimited grant for every service also lifts each model's own
limits), reset usage for one account or everyone without touching the history,
set custom limits, slow accounts down or speed them up, and assign or lock
tiers. Every action is audited. Users see "Your limits" on their account page
and can ask for more tokens per 5 hours or per week, a higher request rate, a
temporary increase or a higher reasoning effort. Amounts raised by automatic
approval are minimums, so later tier promotions still apply; amounts an
administrator sets are exact.

**Community consent for quota requests** (on by default; administrators can
switch it off or limit it to some kinds of request under Limits → Services).
A request for more — for a service or for one model's own limits, including a
boost such as unlimited use for 24 hours — can also be offered to the
community. Administrators can still approve it at once. Other people can back
it on the new Community page by renouncing part of their own quota in the same
service or model, or object to it. With enough consent (by default 3
supporters, two thirds of the votes, and the renounced tokens covering the
increase, within 72 hours) it is applied automatically as a time-limited
increase, and the supporters' renounced tokens come off their own limits for
the same time. When voting closes without consent the request keeps waiting
for an administrator until it is decided or its author cancels it; requests
can now be cancelled from the account page, which gives back any tokens
promised to them. Supporters must have had their account for a week and can
renounce at most half of a limit at a time (all adjustable).

**Model fallback when quota runs out**: when someone picks a model whose
quota is used up for them (its own limits, the Claude pool's shared quota, or
the service tokens it counts toward), the chat answers with a model they can
still use and says so — the same kind first, then cloud → local or local →
cloud, each direction switchable by administrators (both on by default). A
chosen model also gets fallbacks of the other kind when it fails before
answering.

The upgrade converts the previous release's settings: 1 credit becomes 1,000
tokens and the daily amount becomes the same amount per 5 hours, which is
more generous than before, so administrators may want to lower it.
`BC_IMAGE_TOKENS_PER_GENERATION` replaces `BC_IMAGE_CREDITS_PER_GENERATION`
(still read as its default).

### Models

**Claude provider pool** (removed in 1.7.0). The scheduler and extension boundary route Claude
models through an operator-installed, authorized provider adapter instead of
Ollama. Without that adapter and verified discovery they remain unavailable.
The pool aggregates configured active account capacity and rests accounts after
upstream quota errors; exclusive leases prevent simultaneous account use.
Unavailable Claude models can fall back to eligible local models. Claude models
never run on worker PCs or as agents. Claude Fable 5.1, Opus 5.5 and Sonnet 5.5
are curated references only until actual discovery confirms access, with Fable
receiving the strictest preset. Subscription transport needs provider approval
and live verification; it is not included or connected by this release.

- **Detection**: each new or updated model's capabilities, context length,
  family, size, quantisation and reasoning levels are read; a failed or empty
  listing never marks models missing, and one broken model no longer stops the
  sync (the last sync and its errors show in Admin → Models).
- **Enrollment**: new models either wait for review (the default) or are
  enabled automatically with limits for their size and flagged for review.
  Unreviewed models are never picked automatically (`auto`, fallbacks,
  agents); administrators can still try them by name. An ignore list (names or
  patterns such as `*-embed*`) keeps unwanted models out; an ignored model is
  published only after it is restored.
- **Retirement**: models missing for several syncs are hidden and come back
  unchanged, and leave the catalog after 30 days (chats keep their names);
  models that keep failing are hidden until a background test prompt succeeds
  (timeouts while the server is overloaded, and models removed on the model
  server, don't count as failures); fallbacks that stopped working while a
  request waited are skipped;
  administrators can deprecate a model with a replacement and a retirement
  date.
- **Downloads**: one job per model that survives restarts, retries network
  errors and stalls with back-off, checks disk space and is verified by digest;
  pause, reorder and cancel (with or without the partial files); several models
  can be queued at once, and queued downloads wait out a compute-server outage
  instead of failing. Deleting from the model server waits for running
  requests and moves personalities to the replacement; a chat whose model is
  removed mid-answer keeps saving, and API requests for a model that has just
  gone get `404 model_not_found`.

### Personalities

Personalities now have an emoji avatar, an accent colour, a description, an
optional greeting and up to four conversation starters, a preferred model and a
response style (length; precise, balanced or creative). Choose a default
personality for new chats, start from templates, preview changes live and try
a personality in a fresh chat. Share one through a revocable link or move it as
a JSON file. Administrators can publish featured personalities that everyone
can duplicate. Instructions still sit below each model's system prompt.

### Agents and agent swarms (optional, off by default)

Administrators can enable **Agents**: a person describes a coding task
(optionally with files or an archive) and a tool-capable model works on it in
an isolated sandbox on the compute server with bash, file, search and finish
tools, reporting back with a summary. **Swarms** (also off by default) let the
main agent delegate to a bounded number of sub-agents. The task page shows a
live timeline of every tool call and its output, sub-agent lanes, Stop,
follow-up messages and a workspace browser with downloads.

When an administrator allows it (off by default), a task can **start from a
public Git repository** on GitHub, GitLab, Codeberg or an allowed self-hosted
GitLab/Gitea server, at a branch, tag or commit, and the result can be
downloaded as a **patch** (`git diff --binary` against the imported commit,
computed without running anything the agent configured). The web server fetches
only source archives, over verified HTTPS, from allowlisted hosts whose
addresses must be public, with size and time limits; sandboxes still have no
network. `compute/sandbox-image` is a ready-made agent image with Git, a C
toolchain, Node.js and ripgrep.

Safety comes first:

- A new **sandbox runner** (`compute/sandbox_runner.py`, standard library only)
  on the compute server is the only component that talks to Podman or Docker.
  Every sandbox has no network, a read-only root, a size-capped in-memory
  workspace, a non-root user with all capabilities dropped, no-new-privileges,
  default seccomp and memory, CPU, process and file limits; each container is
  inspected to confirm this. The runner refuses a rootful engine unless
  explicitly allowed, never pulls images, kills timed-out commands with
  everything they started, caps output, confines file access to the workspace
  (symlinks included), validates uploads before extracting them inside the
  container and removes idle or old sandboxes.
- The web server never runs agent code. Tool calls are strictly validated;
  steps, tokens and minutes per run, concurrent tasks and task starts are hard
  limits; every model call counts against the new *agent* token limits and each
  step takes a request from the model's own rate (waiting when it is used
  up), at the lowest queue priority, and never goes to worker PCs.
- Users need the new *Agents* access capability (allowlist-only by default).
  Tasks can be stopped from any server process, survive restarts safely
  (marked interrupted), pause during maintenance and are deleted after a
  retention period. Admin → Agents shows the runner's health with warnings
  (network on, rootful engine), limits, model tool support and every task with
  its full log, plus a kill switch; turning Agents off also stops running
  tasks at their next step. See [docs/agents.md](docs/agents.md).

### Service status and downtime

- Maintenance mode and an unreachable AI (compute) server no longer take the
  site down or answer every page with an error. Users can still sign in and
  read their chats; a clear banner explains the situation and updates itself;
  only sending new messages is paused (HTTP 503 with a helpful message).
  Administrators can still use everything during maintenance.
- New public `GET /status` endpoint for monitors: always HTTP 200 with
  `ok`, `degraded`, `maintenance`, `outage` or `updating`.
- While the lifecycle tool updates the server, visitors see a short bilingual
  "back in a moment" page instead of a plain-text error.

### Security and privacy

- Server-side sessions: signing out, changing a password or being suspended
  really ends sessions; users can review and end their other sessions.
- Stricter Content-Security-Policy (no inline scripts or styles at all);
  CSRF protection without an expiry that broke long-open pages.
- Sign-in throttling per address and per account; no setup token in URLs.
- API tokens and worker tokens are shown once and never stored in cookies.
- No-history chats are never sent to volunteer worker PCs; worker job prompts
  are deleted as soon as a job ends.
- Deleted chats are erased after `BC_DELETED_CHAT_RETENTION_DAYS` (default 30);
  metrics after `BC_METRICS_RETENTION_DAYS` (default 30).
- Request bodies are limited per endpoint (the old global limit was 512 MB).
- Model downloads interrupted by a restart are resumed, never deleted.
- PDF text is extracted in a separate process with time and memory limits;
  API images with too many pixels are refused before reaching the model.
- Administrator invitations stop working when their creator is no longer an
  administrator; share-link tokens are kept out of the access log.
- Usage reported by worker PCs is capped at what the job can have used.
- Tokens reserved for image requests that died with their process are
  released automatically.

### Chat

- Rebuilt interface: collapsible sidebar with search across all chats,
  keyboard-accessible model picker, reasoning display, safer Markdown with
  tables and links, attachments by drag-and-drop or paste, voice input,
  read-aloud, PDF and Markdown export, share links that stay stable until
  revoked.
- Answers keep generating and are saved if the browser disconnects; reloading
  shows the progress. Stop works across server processes.
- Under heavy load, health checks and pages stay fast: generating requests
  never take the last threads of a server process, and requests over capacity
  are refused at once with HTTP 503 instead of hanging.
- Slow model loading no longer times out (`BC_FIRST_TOKEN_TIMEOUT`); models
  that fail before answering fall back to another one.
- No-history chats can be reloaded and ended explicitly.
- On phones, Enter starts a new line and the send button sends.

### API

- OpenAI-compatible errors with proper types, `created` fields, a single role
  chunk in streams, `stream_options.include_usage`, content arrays (text and
  images for vision models), `stop`, `seed` and penalties. `owned_by` is now
  `bananachat`. See [docs/api.md](docs/api.md).
- The token page moved to `/developer` (old `/api` links redirect).

### Administration

- New overview with service status, audit log, error log, session management
  per user, quota defaults applied correctly, access policies explained in
  plain words, metrics with CSV export, and a database export compatible with
  `restore --legacy-database`.
- Model names and descriptions edited by an administrator are no longer
  overwritten by the automatic model sync.

### Lifecycle tool

- Every kind of automatic backup package is pruned (not only `auto-`).
- A failed revision is retried only with `update --retry-failed`.
- Data permissions are fixed without following links; purging refuses system
  directories; Git errors are shown (with credentials removed).

### New optional settings

`BC_SESSION_DAYS`, `BC_DEFAULT_LANGUAGE`, `BC_QUEUE_TIMEOUT`,
`BC_FIRST_TOKEN_TIMEOUT`, `BC_MAX_NUM_CTX`, `BC_WORKER_CLAIM_TIMEOUT`,
`BC_DELETED_CHAT_RETENTION_DAYS`, `BC_METRICS_RETENTION_DAYS`, `BC_WORKERS`,
`BC_INFERENCE_LOCAL`, `BC_INFERENCE_FALLBACK_API_KEY`, `BC_AGENTS_RUNNER_URL`,
`BC_AGENTS_RUNNER_TOKEN_FILE`, `BC_AGENTS_MAX_UPLOAD_MB`, `BC_AGENTS_GIT_PROXY` (sandbox runner:
`BC_SANDBOX_*`, see docs/agents.md).
Invalid values no longer stop the server; they fall back to defaults with a
warning in the log. See [docs/configuration.md](docs/configuration.md).

### Removed

- The `/admin-access` sign-in page (it redirects to the normal sign-in, which
  now works during maintenance).
- Flask-WTF and the unused shared HTTP/logging helper modules.
- The per-user quota form and "apply defaults to everyone" in the old admin
  quota page; the new per-account limits panel and "Restore everyone's default
  limits" replace them.
