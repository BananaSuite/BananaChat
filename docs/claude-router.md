# Claude provider extension and router design

BananaChat includes token accounting, account-wide request buckets, model
policies, reasoning unlocks and a deterministic Claude account scheduler. It
includes an opt-in [official Claude Code connector](claude-code.md). Claude stays
disconnected until an operator configures private subscription authentication
profiles and enables the connector. A website bot is not included. No subscription credentials are included in
the repository, the account table or the downloadable source archives.

## Subscription routing constraint

Anthropic's [Claude Code legal and compliance documentation](https://code.claude.com/docs/en/legal-and-compliance)
states that third-party developers may not route their users' requests through
Free, Pro or Max credentials, or collect, store or intermediate Claude account
credentials or session tokens. Its [Agent SDK documentation](https://platform.claude.com/docs/en/agent-sdk/overview)
provides an exception for previously approved integrations. A shared pool of
three subscriptions therefore needs Anthropic's prior approval covering that
deployment. Buying three subscriptions alone does not establish that approval.

No paid API transport is substituted for the requested subscription transport.
If approval cannot be obtained, the supported product integration is an
Anthropic API/cloud-provider adapter; the ordinary Claude Code integration
requires each end user to authenticate with their own credentials. Those are
different deployment choices and need their own configuration.

Claude Code supports [noninteractive execution](https://code.claude.com/docs/en/headless).
It can be an execution adapter for an authorized integration. The scheduler
must remain ordinary application code: an LLM must not decide which account
gets a request or whether a quota applies.

## Implemented extension boundary

Install a trusted Python module using the application's normal deployment
process, then set `BC_CLAUDE_EXTENSION=your_package.claude_adapter`. The module
exports a synchronous `create_adapter(config)`, returning an object with these
synchronous callbacks (coroutines and async generators are rejected):

```python
class Adapter:
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

This example defines the contract; it does not call Claude. The factory should
initialize its own private client state without making network requests. All
three callbacks are validated before registration. Initialization failure
clears every callback, keeps Claude disconnected and logs the exception type
without a potentially secret-bearing exception message. An empty setting
clears a previously configured adapter. Loading an extension is installation
of trusted server code, never a user upload or browser-controlled module name.

Discovery declares actual model capabilities and optionally the account IDs
that can serve each model. Model identifiers and capabilities are validated;
`xhigh` is normalized to BananaChat's `extra`. The curated list is reference
metadata, not evidence that a subscription has access to a model. Failed
discovery must raise an error; an empty successful listing means no models
are available. Manual enrollment is the default. Automatic enrollment installs
the model's preset and publication state together, with a review flag.

The chat callback receives the resolved effort in `options["effort"]` and the
request cancellation token. It must use bounded I/O, honor cancellation,
close its transport, emit an explicit terminal `done` chunk and provide finite,
nonnegative incremental token deltas. It can emit `thinking` alongside `text`.
Raise `claude_pool.QuotaExhausted` for an authoritative upstream quota error,
including its reset timestamp when available. Errors before any output may
try another available account. Errors after text or thinking starts end that
answer without replaying it on another subscription.

## Quotas and scheduling

Internal budgets use input plus output tokens, one five-hour window and an
optional weekly window disabled by default. Every API key shares its owner's
service rate buckets; model buckets also span services. Rate rules support
second, minute, hour and day and every configured rule must pass.

Claude models default to independent model budgets and do not spend the
normal service token allowance. Administrators can enable service counting.
The service's request rate still applies. Opus receives a smaller token budget
and stricter request rules than Sonnet; Haiku receives the most generous
defaults. Provider capacity, demand and eligible usage patterns scale model
limits conservatively. Administrators can replace those presets and set
global or account-specific model policies.

The database grants an exclusive, expiring lease per pooled account before
execution. Heartbeats renew ownership, and completion, error or cancellation
releases it. This prevents separate web workers from selecting the same
subscription concurrently. Priority and remaining capacity choose the account;
the router does not ask a language model to make that decision.

Manual account budgets are **local estimates**, not Anthropic's advertised
subscription capacity. An account needs an explicit five-hour cap before it
can serve requests; an unknown cap is not unlimited. Provider-reported snapshots have a source and freshness
timestamp for each quota window, expire after 15 minutes and require refresh
after an upstream reset. A weekly-only observation cannot refresh stale
five-hour capacity, and a report cannot reactivate an administrator-disabled
account.
The aggregate reporter may lower stored capacity but may not raise it. Reporter
failure blocks provider capacity until a valid refresh. Actual provider
exhaustion and missing or unavailable accounts remain binding even when an
administrator exempts a user from BananaChat's internal quotas.

Anthropic's [usage-limit guidance](https://support.claude.com/en/articles/9797557-usage-limit-best-practices)
describes session and weekly usage indicators under Settings → Usage. It does
not establish a universal subscription token allowance or a documented exact
token-quota endpoint. A percentage must not be presented as an exact token
count. The built-in official connector reads native subscription percentages
and reset timestamps through CLI control metadata. These capacity signals
remain separate from locally configured token budgets; authorized manual
telemetry adapters remain supported. Verify every subscription's actual
availability, quota errors, resets and observation freshness before deployment.

## Reasoning access

BananaChat orders supported levels as `off < low < medium < high < extra < max`.
Only levels declared by that model are exposed. Claude defaults allow Low and
Medium where supported. Approving Extra unlocks High and lower supported
levels for the same scope; it does not invent capabilities on another model.
Administrators can set a ceiling for everyone, one model, one account or an
account/model pair, disable gating, and enable or disable automatic promotion.
Requests for higher levels use the existing quota request and approval flow.
Automatic promotion requires sustained eligible usage and respects the
configured time, usage, suspension and maximum-level requirements.

For Claude Code, `extra` maps to CLI `xhigh`; `max` remains distinct. The CLI's
`ultracode` option is a separate mode that requests xhigh effort, not another
universal access tier. Do not expose it unless the adapter verifies both its
availability and the exact deployed model's support.

## Built-in Claude Code adapter, for provider-approved deployments

Run the unmodified official binary under a dedicated service identity outside
the BananaChat repository, with one isolated, provider-approved authentication
profile per pooled account. Complete authentication through Anthropic's native
flow; do not scrape cookies or implement a substitute login page. Bind the
database's account IDs to private operator configuration. Keep authentication
state outside app data, public files, logs and source archives.

Use a fixed argument vector and stdin for prompts rather than shell commands.
An isolated working directory, disabled built-in tools (`--tools ""`), safe
mode and disabled session persistence reduce access to the host. Account
execution should also run in an OS/container sandbox with no repository,
deployment or other-account secrets mounted. Safe mode alone is not an OS
sandbox. Verify CLI flags against the installed version before activation.
`--bare` requires an API key and is not the subscription execution mode.

Parse bounded `stream-json` output, including terminal result errors, instead
of treating exit code zero or EOF as success. Normalize any cumulative usage
into token deltas once, bound stderr and result sizes, terminate the child
process tree on timeout/cancellation, and release the database lease only after
the process stops. Use the least effort the user selected and the provider
model actually supports. Explicitly isolate API-key environment variables so
subscription execution cannot silently switch to API billing.

The built-in connector implements official authentication checks, bounded
streaming conversion, process cleanup and admin profile selection. Automatic
model discovery and subscription usage use the official CLI control protocol
without generating a model answer. Private manual model/telemetry settings remain
available for explicit operator overrides. Per-account and model-family usage
observations restrict routing without rewriting token budgets. Failed or stale
reports pause the affected account; other fresh subscriptions remain available.
Live account tests are still necessary. Local regression tests verify the loader, scheduler, quota
validation and stream behavior using fake adapters. They do not certify an
unconfigured subscription connection.
