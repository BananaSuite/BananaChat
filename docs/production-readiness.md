# Production validation — 2 October 2026

This assessment covers the current source, disposable local services and one
operator-authorized paid Claude subscription. The changes have not been deployed
to the operator's production servers.

## Current behavior

Ordinary pages share a 1120px frame, consistent controls and short task names.
The composer groups model, reasoning and personality choices before secondary
tools. Common settings appear before optional controls; disclosures preserve
keyboard access, deep links, form values and native validation. Account, API and
administration pages use the same interface conventions. Both landing languages
include seventeen freshly generated screenshots using sample data, including
external provider settings, the Claude account pool and cloud sessions.

Token budgets use five-hour windows and optional weekly windows. Every API key
shares its owner's request buckets. Service, model and account/model token and
rate exemptions remain independent of access, reasoning gates, usage recording
and actual provider capacity. Higher effort approvals unlock supported lower
levels; automatic promotion respects administrator ceilings and opt-outs.

External OpenAI-compatible and native Anthropic API providers use private
server-side keys and enrolled models. The optional official Claude Code
connector uses private subscription profiles, automatic model/effort discovery
and native plan/usage observations. Local token budgets are separate from
provider percentages. Failed, stale, reset-crossed and exhausted observations
restrict the affected account or family; healthy subscriptions remain eligible.
The connector is disabled until the server operator configures it.

Administrators can globally show or hide model-server offline notices. Users
can hide a notice for 24 hours per account in their browser, with its original
expiry retained across reloads, polling and other tabs. These preferences only
change the warning display; health reports, maintenance and unavailable-model
refusals continue to apply.

Cloud sessions are disabled by default. Administrators can choose administrators
only, everyone signed in, or the existing Agents access lists. Fresh installations
select administrators only; legacy explicitly configured installations preserve
their capability policy. The audience does not override model access or quotas.
Ollama and external API models need tool support; native Claude Code subscription
profiles currently serve chat only. Commands run in disposable containers on the
configured compute runner. Provider credentials stay outside workspaces.

## Confirmed fixes

Sensitive account changes recheck credentials, revocation and account status
inside the write transaction. Automatic password rehashing cannot undo a reset.
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
work at the next safe checkpoint; repeated authorization does not debit another
model request bucket. Stale leases cannot mutate a newer run. Cancellation retains
usage and closes provider resources before releasing the account lease.

Hosted model publication preserves strict presets and custom limits. Hosted
requests can proceed during local RAM pressure without waiting behind a blocked
local request. External connections pin validated DNS, reject metadata
addresses even on trusted private networks, and do not forward credentials
through redirects. Schema-19 upgrades preserve IDs, deleted-ID high-water marks,
foreign keys, indexes and triggers.

Claude metadata refreshes use four workers, a bounded overall deadline and
short shared snapshots; concurrent telemetry readers share one in-flight call.
The original native observation timestamp prevents cached usage from appearing
fresh. Validated old percentages remain visible to administrators with their
original timestamp and a stale label, while routing remains paused. Automatic
stale checks back off for five minutes; manual checks run at most once per
minute. Cached plan multipliers must follow the most recent login change.
Distinct authenticated identities are required. Re-authentication clears
cached models/usage and requires new admission before generation. Model-family
limits stay scoped. If process-group shutdown cannot be confirmed, the account
is disabled for review before releasing its lease.

An unavailable Claude quota, stale observation or occupied request slot returns
`503 provider_capacity_unavailable` with `Retry-After: 30`, without a token
charge or model health failure. Internal token limits remain `429`; failed
provider streams remain `502`. Configured fallback remains available before
output, while partial answers keep accounting and are never replayed.

Compute/checkpoint requests have hard header/body deadlines. Backup packages
retain update signer trust, and failed restores restore repository credentials
and trust settings. Sandbox capacity, isolation and cleanup remain covered.

Static assets share a content revision, including relative JavaScript imports,
so an ordinary reload after updating loads the current interface together.
Older asset links remain supported and revalidate. Dynamic HTML, including
public sign-in and setup forms, stays private and uncached.

Provider address checks reject IPv6 transition routes that could tunnel to
protected destinations, including when an operator permits trusted private
providers. Ordinary supported public and private provider addresses retain their
existing policy.

## Verification

The final full Python suite passed **1,956 tests and 13 subtests**, with one
ownership case skipped under the non-root test user. That same case passed
separately in a disposable root container, validating **1,957 unique cases**.
All configured Chromium regressions and required real Docker/agent-image
checks ran in the full suite. The focused agent, Git, audience and runtime
admission suite also passed **101 cases** before this run.
Ruff, dependency compatibility and whitespace checks passed; all eleven
changed or new JavaScript files passed syntax checks. No failures remain in
these checks.

Cloud-session regressions cover all audiences, legacy and malformed settings,
queued revocation, live reductions to run budgets, provider retries, reasoning,
model rollout and tool-policy changes without double rate charging. Thirteen
real Chromium cases exercise English and Italian at desktop and phone widths:
keyboard session creation, uploads, escaped file previews, downloads, archives,
Git import and patch export, follow-ups, revocation, admin audience changes,
missing-runner behavior and cross-user workspace denial. The Git host, model
services and account metadata in these checks are simulated; the application
and browser interactions are real.

A fresh audit of the installed Python environment found no known vulnerability
findings. Dependency compatibility, Ruff and whitespace checks also passed.
The separate Wiki image scan and its unresolved OS advisories are documented
in BananaWiki's production assessment.

Offline-warning checks exercised global show/hide settings, original 24-hour
expiry across reloads and unchanged or failed status polls, account isolation,
blocked or malformed browser storage, maintenance, hidden login-page wrappers
and actual chat replies through a simulated Claude connection with workers
offline. English/Italian desktop and phone captures passed without layout or
script errors. Monitoring and local request refusals remain unchanged when
warnings are hidden.

Asset checks covered deterministic content revisions, shared module URLs,
legacy revalidation, content types, traversal protection, setup access and
fresh HTML with new security nonces.

Desktop (1280px) and phone (390px) checks exercised three synthetic Claude
profiles, quota/model refresh and automatic enrollment, without overflow or
JavaScript errors. External-provider creation, discovery, publication and
connection editing were previously checked at both widths. English/Italian
account, admin, selector, preference and quota forms passed browser checks.
All 18 landing views decoded current images and passed layout/script checks;
9 pages passed link, fragment, image dimensions, alt-text and cache-hash checks.

The latest local Gunicorn run used a 25,000-message database and imitation model
server. All **125 health probes returned HTTP 200** during request bursts.
Excess requests returned controlled `503 server_busy` or `429` responses;
no HTTP 500 occurred and database integrity was `ok`. This measures the local
web tier rather than real inference capacity on the deployment hardware.

Earlier unchanged sandbox checks passed **121 tests** using disposable Docker
containers. Dependency compatibility, Ruff, JavaScript syntax, ShellCheck,
workflow YAML and lifecycle/backup manifests passed. The installed BananaChat
environment audit covered **25 packages with no known vulnerability findings**;
a fresh wheels-only runtime audit covered **12 third-party packages with no
known findings**. CI audits newly installed runtime dependencies.

## Live Claude verification

Official Claude Code **2.1.287** authenticated one native paid subscription.
A minimal low-effort response passed through BananaChat's public
`/v1/models` and streamed `/v1/chat/completions` routes. Its **498 input + 9
output = 507 tokens** matched the pool ledger exactly; its lease was released.
A separate live response cancelled after output began also released its lease.
No API key or paid-API fallback was used. Required proxy settings and certificate
trust are inherited while API credentials and provider overrides are excluded.

Automatic initialization and `get_usage` were then verified without generating
a model answer. The native CLI returned canonical models, supported efforts,
the verified Max plan, five-hour/weekly remaining fractions and reset times,
and a model-scoped weekly allowance. No exact subscription token entitlement
or unverified Max multiplier was inferred. Observations expire and must refresh.
A subsequent automatic API check received a fresh upstream observation of
zero five-hour capacity and correctly refused generation. This confirmed
that local unused tokens cannot override an exhausted subscription.
The final native automatic configuration returned the expected retryable
capacity error without starting a generation process, consuming tokens or
leaving an account lease.
Private authentication, temporary API keys and the downloaded binary remain
outside the repositories and source archives.

Regression fixtures test multiple-account selection, scoped limits, failover,
identity changes, concurrent metadata reads and cleanup. Only one actual paid
subscription and selected chat model have been exercised here. Other real
accounts and every model you publish require their own live checks.

The subsequent native usage read encountered upstream HTTP 429 and returned
Claude Code’s unchanged cached observation. At 12:52 UTC on 2 October, the
adapter correctly retained the verified Max plan, the original 12:19 UTC
observation, 100% five-hour usage and 16% weekly usage with `available: false`
and `status: stale`. It did not invent a fresh balance or authorize generation.
Desktop and phone checks separately confirmed that this last-known readout stays
visible while other healthy accounts can continue serving requests. A second
native read at 13:01 UTC, after the reported five-hour reset, still returned the
same stale observation; routing stayed paused rather than assuming a reset had
restored capacity. Neither metadata check generated a model answer.

## Deployment checks

Verify HTTPS and trusted proxies, backend pairing, backup restore, managed
update/rollback, hardware capacity and sustained provider load on the target
installation. Keep compute/sandbox management listeners private. Run the
Claude transport under an unprivileged identity in an OS/container sandbox
without host secrets, repositories or uploads; CLI safe mode alone is not an
OS sandbox. Review managed CLI hooks and authenticate each distinct profile.

Token limits govern admission and record completed/interrupted usage. An
admitted response can finish past its remaining token allowance within the
configured output/deadline bounds. Provider-side spending limits are needed
when a strict monetary cap is required.

See [deployment](deployment.md), [configuration](configuration.md),
[Claude setup](claude-code.md), [external providers](external-providers.md) and
[agents](agents.md). This source assessment does not certify an untested live
server configuration.
