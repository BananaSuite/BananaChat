# Production validation

## Release check for 1.7.0 — 10 October 2026

This check covers the 1.7.0 source (one release commit on top of `164420e`,
not yet tagged), on Python 3.14.4 and Linux,
with disposable local services. Nothing was deployed to the operator's
servers. READINESS.md gives each checklist item with its command.

What changed since the 3 October assessment below: the Claude subscription
connector was removed; external providers gained presets, connection tests,
model discovery and encrypted keys; accounts can use two-step verification and
administrators confirm high-impact actions; API keys have scopes, expiry and
revocation on password change and two-step changes; secure cookies use the
`__Host-` prefix; sign-in limits no longer lock out shared networks; chat
deletion, account deletion and Gunicorn worker replacement no longer skip
charging (API and playground answers included) or stall the queue; attachment
limits count stored bytes; volunteer workers are paused fairly; encrypted
backups are authenticated, retention counts only authenticated snapshots and
restores check the source they deploy; managed updates follow signed,
annotated release tags and install hash-pinned wheels; a visual review of
every page fixed 248 layout, accessibility and wording issues. The schema is
at version 25. AUDIT.md (fourth and fifth rounds) lists each finding with its
regression test.

| Check | Command | Result |
|---|---|---|
| Tests | `BC_REQUIRE_NODE=1 python -m pytest -q -n 4` (under `eatmydata`) with `age`, Node.js 22.11.0, Playwright 1.62.0 and its headless Chromium | **2,177 passed, 13 subtests passed, 5 skipped, 0 failed** in 9 minutes (after the final pass; 2,175 before it). The skips need Docker (4) or root (1). CI runs the Docker cases |
| Browser check | `python scripts/check_browser.py` (the CI `browser` job) | passed: sign-in, streamed chat, Markdown escaping, model picker, phone layout, maintenance, reasoning selector, account, developer, administration and agent pages, with no script error, failed request or CSP violation |
| Visual crawl | Playwright crawl of 169 pages and states, seven roles, light and dark, English and Italian, at 375, 768 and 1440 px, then at 700, 950 and 1180 px | 375/768/1440: 1,118 visits, 8,898 checks, 0 issues (overflow, axe-core WCAG 2.1 AA, console and page errors, failed requests, CSP, untranslated text, 41 keyboard walks, the core chat flow in 12 variants). 700/950/1180 (the 113 pages crawled in every variant): 996 visits, 8,106 checks, 0 issues, the core flow included. |
| Lint | `ruff check .` with 0.16.6 (the CI pin) and 0.16.10, security rules (S) included | all checks passed |
| Shared sources | `scripts/sync_lifecycle.py --check`, `scripts/sync_backups.py --check` | verified |
| Dependency locks | `scripts/lock_requirements.py --check` and `--verify`; a clean install from both locks | complete; every pin has a wheel with a pinned hash for CPython 3.12 to 3.14 on x86_64 and aarch64; the clean install succeeds without warnings |
| Dependency audit | `pip-audit` 2.10.1 on `requirements.lock`, `requirements-build.lock` and the clean install | no known vulnerabilities |
| Production start | Gunicorn from the clean install, first-run setup and sign-in in Chromium | `/health` and `/login` 200, setup and sign-in work, clean stop |
| Documented schema | `tests/test_docs_schema_version.py` | CHANGELOG.md and docs/deployment.md name schema version 25 |

Not covered by this check, and still needed on the target installation: real
provider keys (Anthropic, OpenAI-compatible services, Gemini) and a real agent
run on Claude with thinking replay, which the tests exercise only against fake
servers; Docker, Podman and gVisor sandboxes (no container engine here; CI
runs Docker and, from this release, Podman); a live Ollama and real volunteer
workers; the managed install, update from 1.6 and rollback on a disposable
host, which was last done for the 3 October assessment (the upgrade path was
reviewed by the second audit and is covered by migration tests); HTTPS,
trusted proxies and the real hardware's capacity.

## Assessment of 3 October 2026

The sections below are the assessment made on 3 October 2026, before the
1.7.0 changes; their test counts and checks refer to that source.

This assessment covers the current source and disposable local services. The
changes were not deployed to a production server for this assessment.

The Claude subscription connector covered by the original assessment has since
been removed, because it pooled consumer subscriptions; its findings and live
checks were dropped from this report. Test counts below predate that removal.

### Current behaviour

Ordinary pages share a 1120px frame, consistent controls and short task names.
The composer groups model, reasoning and personality choices before secondary
tools. Common settings appear before optional controls; disclosures preserve
keyboard access, deep links, form values and native validation. Account, API and
administration pages use the same interface conventions. Both landing languages
include freshly generated screenshots using sample data, including external
provider settings and cloud sessions.

Token budgets use five-hour windows and optional weekly windows. Every API key
shares its owner's request buckets. Service, model and account/model token and
rate exemptions remain independent of access, reasoning gates, usage recording
and actual provider capacity. Higher effort approvals unlock supported lower
levels; automatic promotion respects administrator ceilings and opt-outs.

External OpenAI-compatible and native Anthropic API providers use private
server-side keys and enrolled models.

Administrators can globally show or hide model-server offline notices. Users
can hide a notice for 24 hours per account in their browser, with its original
expiry retained across reloads, polling and other tabs. These preferences only
change the warning display; health reports, maintenance and unavailable-model
refusals continue to apply.

Cloud sessions are disabled by default. Administrators can choose administrators
only, everyone signed in, or the existing Agents access lists. Fresh installations
select administrators only; legacy explicitly configured installations preserve
their capability policy. The audience does not override model access or quotas.
Ollama and external API models need tool support. Commands run in disposable containers on the
configured compute runner. Provider credentials stay outside workspaces.

### Confirmed fixes

Sensitive account changes recheck credentials, revocation and account status
inside the write transaction. Automatic password rehashing cannot undo a reset.
Account creation rechecks the current sign-up policy inside its transaction,
including a switch to invitations or disabled registration while hashing a password.
Malformed CSRF/bot tokens produce controlled errors. JSON rejects non-finite
numbers and excessive nesting; stored invalid preferences use safe defaults.
Selecting the same database from another app instance no longer invalidates
an active transaction; writes remain atomic while the second instance starts.

Queued requests and each fallback attempt recheck model availability, access,
effort, quotas and rates before execution. Interrupted fallbacks charge the
model that ran. Cloud sessions recheck permissions, account status, model
availability, tool policy, reasoning and token budgets after queuing, before
retries and before tools run. Lowering a run budget constrains outstanding
parallel reservations as well as completed steps. Revocation stops subsequent
work at the next safe checkpoint; repeated authorisation does not debit another
model request bucket. Stale leases cannot mutate a newer run. Cancellation retains
usage and closes provider resources.

Concurrent follow-ups cannot exceed the five-message pending bound. Enqueueing
an active session's message checks the bound under the database write lock.
Resuming a session atomically queues its message, rechecks the current account,
audience, ownership and task state, and takes its rate admission and task lease.
Refusals or storage failures roll back the message, rates and lease together.
Runner health requests remain outside the database write lock.
Automatic continuations also recheck the current terminal state: a callback
from an older successful run cannot override a newer Stop or consume another
start slot after a manual resume wins.

New session starts recheck the account, audience, model and repository policy
after runner health returns. Repository import limits apply to administrators
as well as ordinary accounts. Import reservations, start/model/pool admission
and task creation share one database transaction. Concurrent requests cannot
exceed the import limit or hide accepted imports from its accounting; quota or
capacity refusals and transaction failures leave no task, rate debit or launch.
Runner health and upload reads remain outside the write lock.

Hosted model publication preserves strict presets and custom limits. Hosted
requests can proceed during local RAM pressure without waiting behind a blocked
local request. External connections pin validated DNS, reject metadata
addresses even on trusted private networks, and do not forward credentials
through redirects. Schema-19 upgrades preserve IDs, deleted-ID high-water marks,
foreign keys, indexes and triggers.

Compute/checkpoint requests have hard header/body deadlines. Backup packages
retain update signer trust, and failed restores restore repository credentials
and trust settings. Sandbox capacity, isolation and cleanup remain covered.

Malformed configured backend addresses disable the affected endpoint with a
controlled error; pages and account access remain available. Credentials are
omitted for invalid addresses and unsafe bearer-token syntax, with warnings that
do not print token values. Incomplete or malformed Ollama inventory replies do
not withdraw previously enrolled models. Invalid stream JSON, message fields
and token counts fail without reporting a successful completion; partial output
retains accounting. Cancellation, read-gap limits and total response deadlines
reach the live socket after connection-close headers, including trickling headers,
bodies and error replies. Each deadline has independent ownership and is cleared
when its response closes.

Static assets share a content revision, including relative JavaScript imports,
so an ordinary reload after updating loads the current interface together.
Older asset links remain supported and revalidate. Dynamic HTML, including
public sign-in and setup forms, stays private and uncached.

Provider address checks reject IPv6 transition routes that could tunnel to
protected destinations, including when an operator permits trusted private
providers. Ordinary supported public and private provider addresses retain their
existing policy.

### Verification

The final Python 3.14.4 full current-source run passed **2,097 tests and 13 subtests**,
with one ownership case skipped under the non-root test user. The ownership
case separately passed on Python 3.12 in a disposable root container. All
**2,098 collected primary cases** were therefore exercised; all 466 recorded
source hashes and the repository HEAD matched before and after the full run.
This complete snapshot includes the follow-up admission and repository-import
admission fixes described above.
All configured Chromium regressions and required real Docker/agent-image
checks ran in the full suite. A focused transport, malformed-protocol,
accounting, image and streaming run passed **132 cases**. All nine real-socket
deadline, cancellation, cleanup and ownership checks also passed separately
with the production supervisor's one-second tick. The search-result rename
and share regression now checks English and Italian explicitly; its related
browser/chat regressions passed **42 cases**. Ruff, dependency compatibility
and whitespace checks passed; all **19 JavaScript modules** passed syntax
checks. No failures remain in these checks.

The source contains **14 actual SQLite concurrency and admission cases** for
follow-up bounds, rollback, eligibility, manual/automatic resume races and a
newer Stop. A further challenge reproduced an import limit of one accepting
three concurrent requests, creating three tasks but recording only two import
hits. The final import/start admission repair adds **nine cases**, including
ordinary/admin races, capacity refusal and transaction rollback, and policy
changes during health checks. All affected current agent modules passed
**135 cases** after that repair. The current collection is **2,098 primary
cases**, all covered by the complete final-source run and separate ownership
check. The swarm check holds pairs of actual HTTP requests at a bounded
barrier, so its concurrency assertion does not depend on a timing-only overlap.

The minimum Python **3.12** current-source selection passed **80 cases** in an
unprivileged read-only, network-disabled container with fresh compatible
runtime wheels. It includes every new start/follow-up case, runtime admission,
production authentication and actual HTTP swarm concurrency. Dependency compatibility passed.

Cloud-session regressions cover all audiences, legacy and malformed settings,
queued revocation, live reductions to run budgets, provider retries, reasoning,
model rollout and tool-policy changes without double rate charging. Thirteen
real Chromium cases exercise English and Italian at desktop and phone widths:
keyboard session creation, uploads, escaped file previews, downloads, archives,
Git import and patch export, follow-ups, revocation, admin audience changes,
missing-runner behaviour and cross-user workspace denial. The Git host, model
services and account metadata in these checks are simulated; the application
and browser interactions are real.

A fresh Python **3.12** run passed **205 cases** for endpoint configuration,
real-socket deadlines, Ollama protocol validation and authentication. Its
wheels-only runtime dependency audit covered **13 packages,
including pip, with no known vulnerability findings**. CI audits newly
installed runtime dependencies.

Offline-warning checks exercised global show/hide settings, original 24-hour
expiry across reloads and unchanged or failed status polls, account isolation,
blocked or malformed browser storage, maintenance, hidden login-page wrappers
and actual chat replies through a simulated hosted provider with workers
offline. English/Italian desktop and phone captures passed without layout or
script errors. Monitoring and local request refusals remain unchanged when
warnings are hidden.

Asset checks covered deterministic content revisions, shared module URLs,
legacy revalidation, content types, traversal protection, setup access and
fresh HTML with new security nonces.

External-provider creation, discovery, publication and connection editing were
previously checked at desktop (1280px) and phone (390px) widths. English/Italian
account, admin, selector, preference and quota forms passed browser checks.
All 36 landing views decoded current images and passed layout/script checks;
9 pages passed link, fragment, image dimensions, alt-text and cache-hash checks.

The latest local Gunicorn run used a 25,000-message database and imitation model
server. All **130 health probes returned HTTP 200** during request bursts.
Excess requests returned controlled `503 server_busy` or `429` responses;
no HTTP 500 occurred and database integrity was `ok`. The web processes used
**286 MiB** resident memory after the run. This measures the local
web tier rather than real inference capacity on the deployment hardware.

The sandbox-runner suites passed **109 tests**, including required disposable
Docker cases, and the real agent-image Git import and patch-export checks
passed **two cases**. Lifecycle/backup source manifests passed. The real
Gunicorn/Chromium upgrade check from the previous release passed **27 checks**,
preserving accounts, passwords, sign-ins, chats, shares, API tokens,
personalities, preferences and administration access. The standalone browser
check also passed against a disposable current application instance.

### Live Ollama verification

Three complete smoke runs used official Ollama **0.35.1** and the official
Qwen3 **0.6B** weight layer in a disposable localhost-only CPU container.
A derived model set `num_thread=2` to match the two-CPU quota; its weight-layer
checksum was identical to the base model. The container had a 2 GiB memory
limit and was removed after verification. Automatic eight-thread selection
previously caused long delays and a controlled backend timeout; see
[CPU inference tuning](capacity.md#cpu-inference-in-containers).

Real HTTP inventory, capability discovery and catalogue synchronisation passed.
The public completion routes streamed and returned complete replies; one
nonstream answer's **25 input + 21 output = 46 tokens** matched its ledger.
Saved chats retained their messages and exact completed usage. Stop returned
in **2–3 ms**, preserved the partial answer and recorded estimated interrupted
usage. Closing an API stream released resources and charged its partial output
once, including repeated close calls. The three runs recorded **15 matching
ledger entries and request metrics**, left no queue entries or active leases,
and retained SQLite integrity. Reusing the warm backend after cancellation
also passed. These exercised Flask routes and background chat workers with a
real Ollama service; the separate Gunicorn load run used an imitation backend.

### Deployment checks

The actual generated systemd deployment was exercised on a disposable Debian
13 host with Python 3.12. Three synthetic source revisions stayed isolated from
the real repository history. Thirteen checks covered CLI install, disabled
automatic updates, private portable backup, a successful update, a candidate
that mutated SQLite then failed to boot, automatic code/data rollback, manual
rollback and restore into a separate installation. The restore preserved
database records, upload bytes and the private application secret key; installed source
and virtual environments remained unwritable by the service identity. The
Ollama endpoint was a synthetic local contract fixture in these lifecycle
checks; live Ollama verification is reported separately above.

Separate compute and web installations passed **11 checks** using the actual
generated services and a synthetic local Ollama endpoint. They covered private
pairing, rejection of missing/wrong gateway tokens, token rotation, stale-pair
refusal and reconnect. The transport used loopback, corresponding to the
documented supervised tunnel layout; target SSH transport remains an operator
acceptance check.

Verify HTTPS and trusted proxies, backend pairing, backup restore, managed
update/rollback, hardware capacity and sustained provider load on the target
installation. Keep compute/sandbox management listeners private.

Token limits govern admission and record completed/interrupted usage. An
admitted response can finish past its remaining token allowance within the
configured output/deadline bounds. Provider-side spending limits are needed
when a strict monetary cap is required.

See [deployment](deployment.md), [configuration](configuration.md),
[external providers](external-providers.md) and [agents](agents.md). This source assessment does not certify an untested live
server configuration.
