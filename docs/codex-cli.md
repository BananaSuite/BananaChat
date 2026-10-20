# Codex subscription connector

BananaChat includes an opt-in connector to the unmodified official Codex CLI,
answering from ChatGPT subscriptions signed in to that tool. It supports one
subscription or a pool of subscriptions, selected by an administrator, alongside
a separate [Claude pool](claude-code.md). Use this only within your
provider-approved deployment (see the
[routing constraint](subscription-router.md#subscription-routing-constraint)).
No website scraping, copied cookies, API keys or credentials in the source ZIP
are required. API billing is deliberately not a fallback.

## Server setup

1. Run the connector on a Linux/POSIX server under a dedicated, unprivileged
   service identity. Install the official Codex CLI and use its native
   executable, not the Node launcher: for example the
   `codex-x86_64-unknown-linux-musl` release binary from OpenAI's Codex
   repository, or the `vendor` binary inside the `@openai/codex-linux-x64`
   package. Pin and test the deployed version (this connector was checked
   against 0.163.0): it must support `exec --json`, `--strict-config`,
   `--ephemeral`, `--disable`, `login --device-auth` and the `app-server`
   requests below. Production requires Bubblewrap, as for Claude. Install the
   executable in a root-owned, non-writable directory such as
   `/opt/codex/bin/codex` (`0755`, parents `0755`).
2. Create one private directory per subscription for its `CODEX_HOME`, owned by
   that identity, with mode `0700`, under a root-owned, non-writable parent in
   `/srv`. Give each account a distinct directory and keep them apart from the
   Claude profiles (managed installations refuse an overlap):

   ```sh
   sudo install -d -m 0755 /srv/bananachat-codex
   sudo install -d -m 0700 -o bananachat -g bananachat /srv/bananachat-codex/account-one
   ```
3. Sign each profile in with the official device-code flow, as the service user:

   ```sh
   sudo -u bananachat env CODEX_HOME=/srv/bananachat-codex/account-one \
   /opt/codex/bin/codex login --device-auth
   ```

   Sign in with the intended ChatGPT account, then check with the same
   environment and `codex login status`, which should say it is logged in using
   ChatGPT. The connector asks the CLI which account a profile uses and accepts
   only a ChatGPT login; an API-key login is refused. Never upload these
   authentication files to Git.
4. Create a readable private JSON manifest (`0600`, same service owner), outside
   every profile. See [codex-cli.example.json](codex-cli.example.json): each
   profile names its `codex_home`. Leave the profiles' own `config.toml` empty
   or absent, and keep any system or managed Codex configuration minimal: the
   connector runs with strict configuration, so a setting this CLI version does
   not recognise stops every answer rather than being ignored.
5. Configure and restart BananaChat:

   ```sh
   BC_CODEX_EXTENSION=bananachat.services.codex_cli
   BC_CODEX_CLI_CONFIG=/srv/bananachat-codex/connector.json
   ```

   For a managed installation, add these values to its private
   `/opt/bananachat/config/app.env`, then run `sudo bananachat restart`, which
   validates the manifest and grants the service write access to the profile
   directories only. An unset extension leaves Codex disconnected.
6. In **Administration → Models → Codex**, add an account for each profile.
   Choose its profile, priority, five-hour token budget, optional weekly budget
   and, if wanted, caps at a share of the subscription's own five-hour, weekly
   and per-family allowance. Click **Check login**, then **Refresh models** and
   select the models to enrol.
7. Run a short real conversation per model/account before opening access to
   users, as for Claude.

## Models

Automatic discovery asks the CLI's app server for its model list, without
generating an answer. Models the CLI marks hidden are never offered. Models
belong to the astra (strictest), sol, terra and luna (most generous) families,
and other `gpt-` models to gpt; family caps and strict defaults follow that.
Reasoning levels map to BananaChat's scale (`xhigh` is Extra); levels BananaChat
cannot express, such as `ultra`, are not offered. Only text chat is supported.
A profile can instead list its models with `"discovery": "manual"`.

## Subscription usage

`"usage_source": "automatic"` reads the five-hour and weekly windows, their
reset times and the plan from the CLI's own rate-limit request, identifying
each window by its length. The used percentages feed the same routing, caps and
capacity as Claude's. A spend cap, a reached limit or the provider refusing
ordinary usage makes the account unavailable, and purchased credits never count
as subscription capacity, so the pool stops routing to an account whose plan
allowance is used. A request already running when the allowance runs out can
still finish on credits. A failed
or missing reading pauses the account. `"usage_source": "local"` keeps only the
local token budgets and leaves subscription capacity unknown, so percentage caps
keep the account out of rotation.

When an answer fails with the CLI's usage-limit message, the account rests
until its window resets (or for an hour when the reset is unknown) and, before
any text was produced, another account is tried.

## Process isolation and protocol

Answers come from one-shot `codex exec --json` runs with `--ephemeral` and
`--strict-config`, a read-only sandbox, approvals switched off and the profile's
user configuration ignored. The prompt goes on stdin and the conversation's
system prompt in a private temporary file, never in process arguments. Codex has
no option to start without tools, so the connector disables the shell, file
editing, web search, image viewing, plugins, apps, sub-agents and MCP servers
in its configuration, and stops the run the moment any tool activity appears.
A renamed or unknown setting aborts the run instead of quietly re-enabling a
tool. A few built-in helpers the catalogue still adds (a patch tool the
read-only sandbox refuses, a clock and tool search) cannot read files or reach
the network.

`exec` reports each message only once it is complete, so a Codex answer
arrives in one piece rather than streaming. Reasoning summaries are shown as
thinking. Usage comes from the terminal turn record. A provider-side reroute to
another model is not reported to the connector; its usage counts against the
requested model's family.

Only a small environment allowlist is inherited (proxy settings and certificate
trust); API keys, alternate base URLs, access-token overrides and log settings
are excluded. Stderr is drained and never forwarded or logged. Cleanup stops
every process the CLI started, including helpers in their own process groups,
and disables the account for operator review if shutdown cannot be confirmed.
Production runs inside Bubblewrap with only the active profile writable, as
described for [Claude](claude-code.md#process-isolation-and-protocol).

Regression tests use a fake CLI, not live subscription accounts. This connector
was checked against the source and help of Codex 0.163.0, and against the real
binary without a login: the app server handshake and login check, and every
argument and configuration key it passes to `exec`, which the CLI accepted
under strict configuration before stopping at the missing login. It has not
been checked against a signed-in subscription, so no answer, usage reading or
model list has been seen live: verify every published model and signed-in
profile on your deployment before opening access.
