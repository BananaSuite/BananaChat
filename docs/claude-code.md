# Claude Code subscription connector

BananaChat includes an opt-in connector to the unmodified official Claude Code
CLI. It supports one subscription or a pool of subscriptions, selected by an
administrator. Use this only within your provider-approved deployment. No
website scraping, copied cookies, API keys or credentials in the source ZIP
are required. API billing is deliberately not a fallback.

## Server setup

1. Run the connector on a Linux/POSIX server under a dedicated, unprivileged
   service identity. Install the official Claude Code CLI using Anthropic's
   installation instructions. Pin and test the deployed version: it must
   support `auth status`, `--safe-mode`, `--system-prompt-file`, `--effort`,
   `--include-partial-messages` and the other flags below.
2. Create a private home and configuration directory for each subscription,
   owned by that identity, with mode `0700`. Keep them outside the repository,
   web root, uploads and source backups. Give each account a distinct directory;
   do not point two profiles at the same authentication state.
3. Authenticate each profile with the official login flow, as the service user:

   ```sh
   HOME=/srv/bananachat-claude/account-one \
   CLAUDE_CONFIG_DIR=/srv/bananachat-claude/account-one/config \
   /opt/claude/bin/claude auth login
   ```

   Log in with the intended subscription account, then verify with the same
   environment and `claude auth status`. The connector requires `loggedIn: true`
   and `authMethod: "claude.ai"`; API-key/helper/third-party/token-override
   authentication is rejected. Never upload these authentication files to Git.
4. Create a private JSON manifest (`0600`, same service owner). See
   [claude-code.example.json](claude-code.example.json). Set the actual executable
   path and profile directories. Automatic discovery asks the official CLI for its supported models and
   reasoning levels through the SDK control protocol, without generating a
   response. Only text chat (`completion`) is supported by this connector.
   Models need not be identical across profiles; shared models offer only the
   effort levels reported by all their serving accounts.
5. Configure and restart BananaChat:

   ```sh
   BC_CLAUDE_EXTENSION=bananachat.services.claude_code
   BC_CLAUDE_CODE_CONFIG=/srv/bananachat-claude/connector.json
   ```

   Apply these as environment variables through your deployment's service or
   container configuration. An unset extension leaves Claude disconnected.
   Manifest changes require restarting all application workers.
6. In **Administration → Models → Claude**, add an account for each profile you
   want to use. Choose its profile, priority, five-hour token budget and optional
   weekly budget. One profile can belong to only one pooled account. Click
   **Check login**, then **Refresh models** and select the models to enroll.
   Publish them through the normal model review flow, or enable Claude’s
   automatic enrollment option. Previously unchecked models remain excluded;
   genuinely new models are enrolled with strict limits. Disable an account to remove it from routing;
   one enabled account works without any special pool configuration.
7. Run a short real conversation per model/account. Check text, cancellation,
   recorded tokens, upstream exhaustion and another-account failover before
   opening access to users. Authentication checks do not prove model access or
   remaining subscription capacity.

The CLI is an execution transport. BananaChat's database scheduler selects
accounts, enforces model/account limits and grants one renewable request lease
per account. No language model makes routing or quota decisions. Account
selection honors admin priority, capacity and model availability; retry is
allowed only before any text or thinking is emitted.

## Automatic model discovery

New profiles use `"discovery": "automatic"` without a `models` list. The CLI’s
initialization response supplies canonical model IDs, display names and effort
levels. No user prompt is sent and no model answer is generated. BananaChat
caches discovery for five minutes; **Refresh models** invalidates the cache.
Failed discovery cannot fabricate a catalog. Verified missing models become
unavailable without discarding their history or custom limits.

For a restricted automatic catalog, include a `models` allowlist. Entries must
still appear in the CLI’s current catalog; supported efforts are intersected
with the allowlist. Existing aliases can be preserved for older conversations.
An old manifest with an explicit `models` list and no `discovery` setting keeps
manual discovery for compatibility. Set `"discovery": "automatic"` to verify
those entries against the live CLI, or omit `models` to discover all supported
models. `"discovery": "manual"` requires an operator-verified model list and
cannot prove that a listed model still works.

Model availability can differ from entitlement on an actual request. Run a
short conversation for every model you publish. Unsupported models fail closed;
CLI version changes must be verified before upgrading.

## Automatic subscription usage

`"usage_source": "automatic"` is the default. The official CLI’s `get_usage`
control request reads subscription usage using its own native authentication
and refresh, without website scraping, reading OAuth tokens or sending a chat
prompt. The adapter reports five-hour and weekly remaining fractions, reset
times, verified subscription type, and model-specific weekly scopes when the
CLI supplies them. It does not invent a Max plan multiplier or convert provider
percentages into a fixed number of tokens.

The administration page separates **subscription usage** from **local token
budgets**. Observations are cached briefly and expire after 15 minutes or their
reported reset time. The original upstream observation time is preserved;
re-reading a cached CLI response cannot renew an old observation. An invalid,
missing or stale automatic report pauses only its account. Other fresh accounts
can continue. Model-scoped exhaustion restricts the affected family. Provider
weekly capacity remains binding even when local weekly token budgets are off.

If the provider returns HTTP `429`, the native CLI may retain its previous
usage snapshot. Administration keeps that snapshot's original timestamp,
verified plan and last-known percentages visible as **stale**. It remains
`available: false`: the account stays paused until a fresh observation is
verified. Retaining a readout never renews its timestamp or unlocks routing.

Automatic retries after a stale response wait five minutes. **Refresh quota**
can request another check at most once per minute; it does not bypass the
upstream CLI's cache or provider throttling. Only verified plan metadata can
supply a Max multiplier; usage percentages do not establish a token entitlement.

Metadata refreshes run at most four profile tasks concurrently and share a
bounded overall deadline. Concurrent usage readers share an in-flight request;
a short snapshot cache coalesces repeated refreshes. A slow or failed profile
cannot make a large pool perform serial network calls without a deadline.

BananaChat also records terminal CLI usage: input, cache-creation input,
cache-read input and output tokens. Interrupted responses conservatively
estimate unreported usage. Local five-hour token budgets and optional weekly
budgets remain administrator controls; observations never rewrite those
budgets or raise remaining capacity. Priority, observed capacity, model support
and exclusive leases determine routing. A quota rejection pauses the selected
account until its reset; failover is allowed only before output begins.
An explicit model-scoped observation restricts that family. A native quota
rejection without a verified scope conservatively rests the whole account
until its reported reset rather than guessing which models remain usable.
Provider capacity failures are temporary availability errors; they must not
mark a working model as failed or remove it from the catalog.
The public API returns `503 provider_capacity_unavailable` with
`Retry-After: 30` when subscription quota, fresh observations or an exclusive
request slot are unavailable. These refusals use no model tokens. Internal
user token/rate limits keep their `429` responses; malformed or failed model
streams remain provider errors. Configured fallback can run before output;
text or thinking already emitted is never replayed on another model/account.

If the deployed CLI cannot report usage, fix the version/login/network or
explicitly choose `"usage_source": "local"`. That mode retains token limits
but leaves subscription remaining capacity unknown. It is an operator choice,
not an automatic fallback after failed telemetry.

An authorized external telemetry integration can instead set `telemetry_file`
for a profile. The private `0600` file takes precedence and contains:

```json
{"observed_at": 1790928000, "window_left": 0.4, "weekly_left": 0.7}
```

Use the actual UTC observation timestamp, fractions 0–1 or `null` for unknown
scopes, and optional `window_resets_at`/`weekly_resets_at` Unix timestamps. Never
rewrite an old observation’s timestamp. Invalid/stale files pause that account.

Every active profile must use a distinct subscription. Different directories
alone do not make different accounts: repeated authenticated identities are
excluded from discovery and routing. Identity fingerprints stay inside the
adapter and are not displayed, persisted or included in audit details.
Authentication or identity invalidation clears cached usage observations,
including stale display data. Changing a profile's authenticated account also
invalidates its cached models. Before generation, the connector requires a fresh report
for that identity and checks the requested model's capacity again.

## Process isolation and protocol

Every request uses a fresh temporary working directory in its private profile
home. The connector sets `--tools ""`, an empty strict MCP configuration,
`--safe-mode`, `--disable-slash-commands`, `--no-session-persistence` and
`--max-turns 1`. It replaces the coding system prompt with the conversation's
system prompt and passes the role-tagged conversation as JSON on stdin.
Extra effort maps to CLI `xhigh`; unsupported effort levels are rejected.
Temperature and output-token sampling controls are not exposed by this CLI
transport; configured request deadlines and response size limits are binding.
Images and other non-text content are rejected explicitly.

Only a small environment allowlist is inherited, including operator-configured
HTTP(S)/ALL proxy settings, NO_PROXY and certificate trust. This preserves
required outbound network policy in proxied deployments. API keys, alternate base URLs,
OAuth environment overrides and provider switches are excluded. Native
subscription authentication is checked before each request. No prompt is
interpolated into shell code or put in process arguments. Stdout and stderr are
bounded; stderr and auth identity details never reach users or application
logs. Structured terminal success and a successful process exit are both
required before an answer is marked complete. EOF, malformed records, negative
usage, timeout and cancellation fail closed. The process group is terminated
and reaped before releasing the account lease.
On Linux, cleanup also waits for non-zombie process-group descendants to stop.
If shutdown cannot be confirmed, the pooled account is disabled for operator
review before its lease is released.

CLI flags are **not an operating-system sandbox**. For production, run this
provider service in an OS/container sandbox without host/deployment secrets,
repositories or user uploads. Mount only the needed private profiles and the
connector configuration. Managed Claude Code policy can still add managed
hooks; the operator must verify that policy before enabling the service. The
manifest/executable/profile files are trusted operator configuration, not
browser uploads. Separate OS identities/containers are preferable if stricter
separation between the three subscription credentials is required.

Regression tests use a real subprocess fixture, not live subscription accounts.
They verify auth isolation, streamed text, accounting, terminal errors, real
cancellation/timeout cleanup, duplicate-profile prevention and admin bindings.
A live subscription check on 2 October 2026 used official Claude Code 2.1.287
and the `sonnet` selector at low effort. The full BananaChat API streamed a
response, recorded its 507 reported tokens in the account-pool ledger and
released the account lease. A separate response was cancelled after output
began, with the lease released. This covers one account and one model selector;
it does not establish live multi-account failover or other model access.
Automatic initialization and usage discovery were also verified against this
native subscription without sending a model prompt: canonical model IDs,
supported efforts, the Max plan, five-hour and weekly fractions, reset times
and a model-scoped weekly allowance were returned. Those observations are
temporary capacity signals, not an exact token allocation. Verify every
published model and authenticated profile on your deployment.

Protocol references: [official CLI options](https://code.claude.com/docs/en/cli-reference)
and [structured SDK message types](https://platform.claude.com/docs/en/agent-sdk/typescript).
