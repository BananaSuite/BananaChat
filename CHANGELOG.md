# Changelog

## Unreleased

- Keep deep-linked settings fields fully visible after mobile browsers adjust
  the initial scroll position, while allowing users to scroll away normally.

- Production Claude Code calls now use an opt-in Bubblewrap filesystem/PID
  sandbox. Managed services grant write access only to validated private
  profile roots, retaining strict system protections. Unsafe overlaps,
  service-writable executables and a manifest inside a profile fail closed;
  custom public CA files remain available through exact read-only mounts.

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
  intact. Hosted Claude models can continue during a local compute outage.
- Open pages pick up global notice changes without changing sending state.
- Version static assets together so an ordinary reload after an update loads
  current scripts, relative module imports and styles. Legacy links revalidate;
  dynamic pages, including public sign-in/setup forms, remain uncached.
- Keep active database transactions intact when another app instance selects
  the same database; only a different database invalidates connections.

### Claude subscription discovery and pool completion

- Discover canonical Claude models and supported efforts through the official
  CLI’s unbilled control protocol; keep manual selection and allowlists.
- Read actual subscription usage and reset times automatically, preserving the
  provider observation timestamp; display plan, upstream allowances and local
  token budgets separately. Failed reports isolate their account.
- Retain last-known subscription values as stale when upstream throttling keeps
  an old CLI snapshot, while leaving that account paused. Back off automatic
  checks for five minutes and manual refreshes for one minute; authentication or
  identity invalidation clears cached observations.
- Respect model-family weekly limits, distinct native subscription identities,
  and per-Claude automatic enrollment without re-enabling excluded models.
- Bound concurrent profile refreshes and share in-flight usage requests;
  recheck changed logins before admission and quarantine accounts if process
  descendants cannot be stopped safely.
- Return a retryable capacity error when Claude has no eligible subscription
  quota or request slot, without counting it as a failed model.
- Preserve hosted strict/custom quotas during publication and preset changes;
  permit hosted inference during local memory pressure; harden metadata address
  blocking and preserve model ID sequences during schema upgrades.

### Claude Code live validation

- Preserve operator proxy settings in the subscription CLI transport while keeping API credentials and provider overrides isolated.
- Verify native subscription streaming through BananaChat’s API, exact pool token accounting and cancellation with the official CLI.

### External API providers

- Administrators configure OpenAI-compatible and native Anthropic API providers
  with base URLs, private server credentials, connection checks, model selection
  and optional automatic enrollment. Published models use BananaChat's existing
  chat, playground, agents and public API, alongside local and subscription models.
- Provider-scoped IDs prevent collisions. API keys share their owner's budgets
  and rate buckets. External presets enforce five-hour model limits before and
  after publication; weekly limits remain optional. Model capabilities are
  explicit, and fresh reasoning access is enforced before provider execution.
- Connections pin verified DNS destinations, require HTTPS for public endpoints,
  block metadata addresses and never forward credentials through redirects.
  Structured streams require terminal success; cancellation, usage and tools
  retain the existing accounting and authorization rules.
- Hosted models remain available during a local-server outage. Schema version 19
  adds provider metadata and model bindings while preserving existing data.
  See [provider setup](docs/external-providers.md) for compatibility options.

### Claude Code subscription connector

- An opt-in official CLI transport supports one or multiple private subscription
  profiles, selected by administrators. Native subscription authentication is
  verified; inherited API keys and alternate provider settings are excluded.
- Text and thinking stream without duplicate complete-message output. Actual
  input, cache and output tokens are recorded. Terminal success and successful
  process exit are required; cancellation and timeout terminate the process
  group before its account lease is released.
- Admin profile bindings are unique and cannot change during an active request.
  Operator-verified model lists and optional fresh authorized usage observations
  feed existing enrollment and strict five-hour/weekly quota controls. No exact
  subscription balance or live model catalog is fabricated.
- See [setup](docs/claude-code.md). The connector is disabled until configured;
  live deployment requires private logins and model/account validation.

### Interface refinement

- Chat keeps model, reasoning and personality choices together before secondary
  tools. Pickers share keyboard and dismissal behavior, show the selected item
  clearly and recover from invalid saved reasoning preferences.
- Quota forms group token accounting and limits before optional demand and
  reasoning settings. Shared template fields and a route-free quota UI helper
  keep validation and administration views consistent.
- Account usage summaries accommodate three services. API credentials are
  labeled as keys in English and Italian, distinct from model token usage.
- Primary button labels follow the selected color's contrast instead of the
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
- Claude account selection uses exclusive database leases. Quota snapshots
  validate bounds, reset timestamps and freshness; invalid reporter results
  block capacity. Discovery must confirm a model before enrollment or use, and
  streams require a valid terminal record and release resources on cancellation.
- `BC_CLAUDE_EXTENSION` loads an operator-installed adapter with chat, discovery
  and quota callbacks, disabled by default. An opt-in official Claude Code connector is now included; a
  website bot and subscription credentials are not included. Shared subscriptions
  require Anthropic's prior approval; see [router design](docs/claude-router.md).
- Model enrollment installs presets before publication. Download cancellation,
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
  music bonuses, reasoning settings and appearance customization remain
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
- Customize shows common preferences and the preview before expandable colour,
  highlighting and background controls. Optional API limit explanations and
  administrative settings open on request; deep links reveal their section.
- Account navigation uses short section names and a compact profile form.
  Administrative monitoring and quota details no longer dominate their pages.
- A more compact visual system across chat, personalities, images, agents,
  account, customization, API, music and administration: quieter surfaces,
  smaller radii, lighter cards, consistent typography and subtle borders.
- Enabled tools are direct links in the header. Navigation switches to one
  menu only when the labels run out of room, including with larger text or
  another language. It closes on outside clicks and Escape returns focus.
- Account and customization settings use separated sections instead of stacked
  boxes. The account navigator tracks the section being read.
- Ordinary pages share one 1120 px frame. Account and customization settings
  span the page, with section links above them and the preview below. API and
  administration use the same frame and shared form controls;
  personalities use readable rows, with distinct work areas for images and agents.
- Customize has a labeled header shortcut on desktop and a compact icon on
  phones. Account menus indicate the current page. Administration links are
  grouped by purpose, with a compact section switcher on phones.
- Chat uses a flat composer without a glowing focus border, aligned message
  metadata and actions, wrapping tools on small screens, and a 250 px default
  history column. Saved custom widths
  are retained. Shared controls have consistent sizes and larger touch targets;
  chat notifications stay clear of the composer.
- Customization has section shortcuts and aligned reading and color controls.
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
  effects. Cloud account forms use labeled controls and confirm account removal.

## 1.6.0

This release is a complete rewrite of the application for its open-source
release. It updates existing installations in place.

### Updating

Run `sudo bananachat update` (or let automatic updates pick it up). The
previous release's updater backs up the installation, starts the new version
and checks its health before letting users in; if anything fails it restores
the old code and database together.

On first start the database is upgraded in place (schema version 20). The
upgrade keeps accounts, chats, attachments, API tokens, quotas, access
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

**Claude provider pool.** The scheduler and extension boundary route Claude
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
