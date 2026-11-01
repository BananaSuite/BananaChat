# Subscription pools: provider extensions and router design

BananaChat includes token accounting, account-wide request buckets, model
policies, reasoning unlocks and a deterministic subscription account scheduler
for two providers, Claude and Codex (ChatGPT). It includes opt-in connectors for
the official [Claude Code](claude-code.md) and [Codex](codex-cli.md)
command-line tools. Each provider stays disconnected until an operator
configures private subscription authentication profiles and enables its
connector. A website bot is not included. No subscription credentials are
included in the repository, the account table or the downloadable source
archives.

## Subscription routing constraint

Anthropic's [Claude Code legal and compliance documentation](https://code.claude.com/docs/en/legal-and-compliance)
states that third-party developers may not route their users' requests through
Free, Pro or Max credentials, or collect, store or intermediate Claude account
credentials or session tokens. Its [Agent SDK documentation](https://platform.claude.com/docs/en/agent-sdk/overview)
provides an exception for previously approved integrations. A shared pool of
three subscriptions therefore needs Anthropic's prior approval covering that
deployment. Buying three subscriptions alone does not establish that approval.

OpenAI's [terms of use](https://openai.com/policies/terms-of-use/) and its
[account sharing policy](https://help.openai.com/en/articles/10471989-openai-account-sharing-policy)
tie a ChatGPT account, and the Codex usage its plan includes, to the person who
created it; anyone else who needs the products registers an account of their
own. A Codex pool serving other people needs OpenAI's approval in the same way.

No paid API transport is substituted for the requested subscription transport.
If approval cannot be obtained, the supported product integration is an
[external API provider](external-providers.md); the ordinary Claude Code and
Codex integrations require each end user to authenticate with their own
credentials. Those are different deployment choices and need their own
configuration.

Claude Code supports [noninteractive execution](https://code.claude.com/docs/en/headless)
and Codex has `codex exec`; either can be an execution adapter for an
authorised integration. The scheduler must remain ordinary application code:
an LLM must not decide which account gets a request or whether a quota applies.

## Implemented extension boundary

Install a trusted Python module using the application's normal deployment
process, then set `BC_CLAUDE_EXTENSION=your_package.claude_adapter` or
`BC_CODEX_EXTENSION=your_package.codex_adapter`. The module exports a
synchronous `create_adapter(config)`, returning an object with these
synchronous callbacks (coroutines and async generators are rejected):

```python
class Adapter:
    provider = "claude"  # optional; an adapter declaring the other provider is refused

    def chat(self, account, model_name, messages, options, *, cancel=None):
        # Token fields are incremental deltas, not cumulative totals.
        yield {"text": "Hello", "tokens_in": 12, "tokens_out": 1}
        yield {"done": True, "tokens_out": 0}

    def discover(self):
        # Return only models actually available to these accounts.
        return [{"name": "provider-model-id", "family": "sonnet",
                 "reasoning": ["low", "medium", "high", "xhigh"],
                 "capabilities": ["completion"], "account_ids": [1, 2, 3]}]

    def quota(self):
        # Fractions from verified upstream telemetry, bounded to [0, 1].
        return {"window_left": 0.4, "weekly_left": 0.7}
```

This example defines the contract; it does not call a provider. The factory
should initialise its own private client state without making network
requests. All three callbacks are validated before registration.
Initialisation failure clears every callback of that provider, keeps it
disconnected and logs the exception type without a potentially secret-bearing
exception message; the other provider is not affected. An empty setting clears
a previously configured adapter. Loading an extension is installation of
trusted server code, never a user upload or browser-controlled module name.

`quota()` can also return `account_reports`, one observation per account ID,
so each account is routed by its own remaining share rather than the pool's.
An observation carries its source, the time it was made, the fractions left of
the five-hour and weekly allowances with their reset times, the plan and the
fractions left per model family.

Discovery declares actual model capabilities and optionally the account IDs
that can serve each model. Model identifiers and capabilities are validated;
`xhigh` is normalised to BananaChat's `extra`, and a level outside BananaChat's
scale is refused. Claude models belong to the fable, opus, sonnet or haiku
family; Codex models to astra, sol, terra or luna, and other `gpt-` models to
gpt. The curated list is reference metadata, not evidence that a subscription
has access to a model. Failed discovery must raise an error; an empty
successful listing means no models are available. Manual enrolment is the
default. Automatic enrolment installs the model's preset and publication state
together, with a review flag.

The chat callback receives the resolved effort in `options["effort"]` and the
request cancellation token. It must use bounded I/O, honour cancellation,
close its transport, emit an explicit terminal `done` chunk and provide finite,
nonnegative incremental token deltas. It can emit `thinking` alongside `text`.
Raise `subscription_pool.QuotaExhausted` for an authoritative upstream quota
error, including its reset timestamp when available. Errors before any output
may try another available account. Errors after text or thinking starts end
that answer without replaying it on another subscription.

A failed attempt rests its account for five minutes, except when the failure
cannot be the account's: raise `subscription_pool.RequestRejected` for a request
that no account can answer (too long, not text, text that is not valid
Unicode); it ends the request and rests nothing. An adapter that sets
`reports_cli_start = True` and calls `subscription_pool.cli_started()` just
before it starts the provider's process is never rested for failures before
that point (a temporary file, a sandbox, input it cannot encode). An error with
`timed_out = True` (`subscription_cli.ConnectorTimeout`) ends the request
without trying another account, so a request too long for the time allowed
rests at most one account, and none once output was streamed. The API refuses
text with lone surrogates with 400 `invalid_text` before any of this.

## Quotas and scheduling

Internal budgets use input plus output tokens, one five-hour window and an
optional weekly window disabled by default. Every API key shares its owner's
service rate buckets; model buckets also span services. Rate rules support
second, minute, hour and day and every configured rule must pass.

Subscription models default to independent model budgets and do not spend the
normal service token allowance. Administrators can enable service counting.
The service's request rate still applies. Opus and Fable receive smaller token
budgets and stricter request rules than Sonnet, and Haiku the most generous
defaults; for Codex, Astra is the strictest, Sol and Terra standard and Luna the
most generous. Provider capacity, demand and eligible usage patterns scale model
limits conservatively. Administrators can replace those presets and set global
or account-specific model policies.

Each provider is a separate pool: a Claude model is only ever answered by a
Claude account and a Codex model by a Codex account. The database grants an
exclusive, expiring lease per pooled account before execution. Heartbeats renew
ownership, and completion, error or cancellation releases it. This prevents
separate web workers from selecting the same subscription concurrently.
Priority and remaining capacity choose the account; the router does not ask a
language model to make that decision.

Manual account budgets are **local estimates**, not the provider's advertised
subscription capacity. An account needs an explicit five-hour cap before it
can serve requests; an unknown cap is not unlimited. Provider-reported snapshots
have a source and freshness timestamp for each quota window, expire after 15
minutes and require refresh after an upstream reset. A weekly-only observation
cannot refresh stale five-hour capacity, and a report cannot reactivate an
administrator-disabled account. The aggregate reporter may lower stored
capacity but may not raise it. Reporter failure blocks provider capacity until
a valid refresh. Actual provider exhaustion and missing or unavailable accounts
remain binding even when an administrator exempts a user from BananaChat's
internal quotas.

Administrators can also cap each account at a share of the provider's own
allowance: a percentage of the five-hour window, of the weekly window and, per
model family such as Fable, of that family's allowance. The share left under a
cap reaches zero exactly at the cap, so an account capped at 70 % stops
receiving requests once 70 % of its five-hour allowance is used and returns
after the provider reports a reset. A cap on an allowance the provider does not
report keeps the account out of rotation. Caps decide admission: an answer
already running can still finish above them. Per-family token budgets are local
estimates, like the account budgets.

A background job reads usage again every `BC_SUBSCRIPTION_PROBE_INTERVAL`
seconds and ends expired rest periods. While a provider has no adapter, or no
active account, the model sync stops offering its models without retiring them;
they return once an adapter reports them again.

Anthropic's [usage-limit guidance](https://support.claude.com/en/articles/9797557-usage-limit-best-practices)
describes session and weekly usage indicators under Settings → Usage. It does
not establish a universal subscription token allowance or a documented exact
token-quota endpoint. Codex likewise reports used percentages and reset times
for its five-hour and weekly windows. A percentage must not be presented as an
exact token count. The built-in connectors read native subscription percentages
and reset timestamps through each tool's own metadata requests. These capacity
signals remain separate from locally configured token budgets; authorised manual
telemetry adapters remain supported. Verify every subscription's actual
availability, quota errors, resets and observation freshness before deployment.

## Reasoning access

BananaChat orders supported levels as `off < low < medium < high < extra < max`.
Only levels declared by that model are exposed. Subscription defaults allow Low
and Medium where supported. Approving Extra unlocks High and lower supported
levels for the same scope; it does not invent capabilities on another model.
Administrators can set a ceiling for everyone, one model, one account or an
account/model pair, disable gating, and enable or disable automatic promotion.
Requests for higher levels use the existing quota request and approval flow.
Automatic promotion requires sustained eligible usage and respects the
configured time, usage, suspension and maximum-level requirements.

For both connectors `extra` maps to the tool's `xhigh`; `max` remains distinct.
Codex's `ultra` has no BananaChat level and is not offered. The Claude CLI's
`ultracode` option is a separate mode that requests xhigh effort, not another
universal access tier. Do not expose it unless the adapter verifies both its
availability and the exact deployed model's support.

## Built-in adapters, for provider-approved deployments

Run the unmodified official binaries under a dedicated service identity outside
the BananaChat repository, with one isolated, provider-approved authentication
profile per pooled account. Complete authentication through the provider's
native flow; do not scrape cookies or implement a substitute login page. Bind
the database's account IDs to private operator configuration. Keep
authentication state outside app data, public files, logs and source archives.

Use a fixed argument vector and stdin for prompts rather than shell commands.
An isolated working directory, disabled built-in tools, safe mode and disabled
session persistence reduce access to the host. Account execution should also
run in an OS/container sandbox with no repository, deployment or other-account
secrets mounted. Safe mode alone is not an OS sandbox. Verify CLI flags against
the installed version before activation. Claude Code's `--bare` requires an API
key and is not the subscription execution mode.

Parse bounded JSON output, including terminal errors, instead of treating exit
code zero or EOF as success. Normalise any cumulative usage into token deltas
once, bound stderr and result sizes, terminate the child process tree on
timeout/cancellation, and release the database lease only after the process
stops. Use the least effort the user selected and the provider model actually
supports. Explicitly isolate API-key environment variables so subscription
execution cannot silently switch to API billing; each connector also refuses a
profile that is signed in with an API key rather than a subscription.

Codex has no option to start without tools, so its connector disables the
shell, file editing, web search, image viewing and MCP servers in its
configuration and stops an answer the moment any tool activity appears. The
request is refused without resting the account, since the prompt asked for the
tool.
`codex exec` delivers each answer whole, so Codex replies appear at once rather
than streaming.

The built-in connectors implement official authentication checks, bounded
output conversion, process cleanup and admin profile selection. Automatic model
discovery and subscription usage use each tool's own metadata requests without
generating a model answer. Private manual model/telemetry settings remain
available for explicit operator overrides. Per-account and model-family usage
observations restrict routing without rewriting token budgets. Failed or stale
reports pause the affected account; other fresh subscriptions remain available.
Live account tests are still necessary. Local regression tests verify the
loader, scheduler, quota validation and stream behaviour using fake adapters.
They do not certify an unconfigured subscription connection.

## Signing accounts in from the administration pages

With a `managed_root` in a connector manifest (the setup script
`scripts/setup_subscriptions.py` adds one), administrators create a **new
private profile** for an account and sign it in from **Administration → Models
→ Claude** or **Codex**, without a shell on the server:

- the server starts the provider's own sign-in command in that profile, inside
  the same sandbox as the answers: `claude auth login --claudeai` (a link, then
  a code the administrator pastes back) or `codex login --device-auth` (a link
  and a one-time code to enter at OpenAI);
- the page relays only the provider's link and code; credentials are written
  by the official tool into the profile and never pass through BananaChat;
- a sign-in session ends after 15 minutes, on cancel, or when the tool exits;
  the account's login is then checked with the tool's own status command;
- starting a sign-in needs a recent confirmation (password or authenticator
  code); signing out runs the tool's own logout, and deleting the account
  removes its private profile.

Profiles made this way live in `managed_root/managed-<account id>`, owned by
the service and invisible to other accounts' sandboxes. Profiles listed in the
manifest still work alongside them. The **Setup** checklist at the top of each
page reports the connector module, the configuration, the official CLI, the
sandbox and sign-in support, each with how to fix it.

## Sandbox on Ubuntu

Bubblewrap needs unprivileged user namespaces. Ubuntu 23.10 and later restrict
them with AppArmor (`kernel.apparmor_restrict_unprivileged_userns=1`), and the
Setup checklist then reports that the sandbox cannot start. Either:

- run `sudo python3 /opt/bananachat/current/scripts/setup_subscriptions.py
  --allow-bwrap-userns`, which installs `/etc/apparmor.d/bananachat-bwrap`, a
  profile that lets only `/usr/bin/bwrap` create user namespaces, and reloads
  it; or
- write an equivalent profile yourself (`abi <abi/4.0>, include <tunables/global>
  profile bananachat-bwrap /usr/bin/bwrap flags=(unconfined) { userns, }`) and load it with
  `sudo apparmor_parser -r`.

Turning the restriction off for the whole system
(`sysctl kernel.apparmor_restrict_unprivileged_userns=0`) also works but is
broader than needed. Subscription answers stay off until the sandbox works.
