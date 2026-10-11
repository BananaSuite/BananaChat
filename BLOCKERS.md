# Blockers

Open actions and decisions that only the owner can take before 1.7.0 is
released, as of 10 October 2026. The safest default is in place in each case.
Findings and their status are in [AUDIT.md](AUDIT.md), the checks in
[READINESS.md](READINESS.md) and the upgrade notes in
[UPGRADING.md](UPGRADING.md).

## Before tagging 1.7.0

- **Merge the release.** 1.7.0 is pushed as a single commit on the `release`
  branch, on top of `main` (`164420e`), with a pull request (AUDIT.md #39,
  #58). Merge it once CI passes. The commit is not signed; the release tag
  below is what managed servers verify.
- **Create and sign the release tag as an annotated, SSH-signed tag:**
  `git config gpg.format ssh`, then `git tag -s v1.7.0` on the release commit,
  and push it. A server that follows release tags with signatures required
  deploys only an annotated tag signed for its own name by an allowed signer;
  a signed commit under a lightweight or unsigned tag releases nothing
  (AUDIT.md #67). Change `## 1.7.0 (unreleased)` in CHANGELOG.md to the
  release date (AUDIT.md #38).
- **Protect `v*` tags on the forge** so that only maintainers can create,
  move or delete them. Servers try the 20 highest release tags; anyone who
  can push 20 unverifiable tags above the real release stops updates on
  servers that require signatures (they fail closed and say why; AUDIT.md
  #78).
- **Publish the maintainers' allowed signers** (for example in SECURITY.md or
  docs/deployment.md), so operators can run
  `updates enable --require-signatures FILE`. Without it, automatic updates can
  only be enabled with `--allow-unsigned`.
- **Watch the first CI run.** The `sandbox-podman` job has never run, and the
  `browser` job installs Playwright 1.62.0. If Podman's `inspect` output
  differs from what the unit tests model, adjust `_capabilities_dropped` in
  `compute/sandbox_runner.py` (AUDIT.md #54).

## On a production server after updating

- Update the web server before running the compute server's
  `compute rotate-token`, then reconnect it with the new pairing code; only
  then do model downloads need the separate management token. Pair a newly
  installed compute server only with a 1.7 web server.
- Run `sudo bananachat backups run` so the remote series holds authenticated
  snapshots, and consider `backups configure --append-only` with forge branch
  protection (AUDIT.md #50).
- Decide whether to sign everyone out at the update (UPGRADING.md, Session
  cookie): it removes the first-day cookie residual of AUDIT.md #68 at the
  cost of one sign-in per user. Worth doing if people you do not trust control
  other subdomains of the chat's domain (for example hosted wikis).
- Turn on **Require two-step verification for administrators** once your own
  account uses it (Admin → Site settings → Sign-in security).
- Delete invitations marked **Weak** in Admin → Invitations and any
  `BC_CLAUDE_*` line left in `config/app.env`.
- Check accounts that store many PDFs: attachment limits now count stored
  text, so some may be over `BC_CHAT_MAX_ACCOUNT_ATTACHMENT_MB` or
  `BC_CHAT_MAX_SESSION_ATTACHMENT_MB` after the update (uploads refused until
  files are removed or the limit is raised).

## Checks that need real infrastructure

The test suite uses fake providers, a fake Ollama and fake sandbox runners
for these:

- External providers with real keys: Anthropic `display: summarized` on a
  Claude model with adaptive thinking, Gemini's chat requests with bare model
  ids (and a 1.6 Gemini provider upgraded in place), OpenRouter `xhigh`
  effort, Azure OpenAI v1 without `api-version`, and `stream_options` on the
  local servers you use.
- One real agent task on Claude with adaptive thinking and one on an older
  budget-thinking model, to confirm that replayed thinking blocks cause no
  provider 400 on tool continuations.
- Rootless Podman and gVisor sandboxes (docs/agents.md has the manual check).
- A real volunteer worker that cannot load a model it advertises, to see the
  worker paused after three accounts and the answers served by the server
  (AUDIT.md #62).

## Decisions

- **Session cookie migration window.** Sessions from before the `__Host-`
  switch move for 24 hours (`HOST_COOKIE_MIGRATION_SECONDS` in
  `bananachat/security.py`). Setting it to 0 signs everyone out at the update
  and removes the residual of AUDIT.md #68. Current default: 24 hours, with
  the operator recipe in UPGRADING.md.
- **Volunteer worker breaker.** A worker on which three accounts' jobs failed
  "for the request's sake" within 30 minutes is paused
  (`REQUEST_FAULT_ACCOUNTS` in `services/remote.py`); three colluding accounts
  can therefore pause one worker for about a minute per 30 minutes. Current
  default: 3.
- **Regenerating an interrupted answer.** **Try again** resends the question
  through the normal send path; the interrupted answer, already charged, stays
  in the history. Replacing it in place needs a new endpoint and decisions on
  charging and on two answers in a row. Current default: resend.
- **Retired image checkpoints** answer `503 model_unavailable`, not a 404
  saying they were retired (that needs a new English and Italian error).
- **Site name at 375 px with 135 % text and extra letter spacing** ends in an
  ellipsis (its accessible name is complete). It could become logo-only below
  about 360 px.
- **Charging across worker replacements for image generation and agent
  tasks** was not reviewed in the fifth round (chat, API and playground
  answers were). A chat run on an external model that reasons without
  streaming checkpoints its usage only when output arrives, so a SIGKILL or
  out-of-memory kill during silent reasoning charges nothing (a normal stop
  or replacement is handed over and charged).
- **Type checker.** mypy reported 240 errors in the third round, none a real
  defect. Adopting one means annotating about 60 files and adding it to CI.
  Current default: none.
- **Lint strictness.** BananaChat keeps S101, S104, S105 and S704 on, with a
  reviewed `# noqa` on each accepted line; BananaWiki ignores them globally.
  Align the two repositories either way, or keep the difference.
- **Password to create an API key.** Keys are revoked on password change and
  can be read-only and expire, but creating or rotating one does not ask for
  the password again (AUDIT.md #40). Current default: no extra prompt.
- **Minimum Ollama version on the compute gateway.** Not enforced; model
  management can be gated by its own token or switched off instead
  (AUDIT.md #55).
- **Container base images** (`compute/sandbox-image`, the CI images) are
  pinned by tag, not by digest.
- **Credits in the published history.** NOTICE and README now credit "Luca
  Zani and contributors" and name no organisation, and
  `tests/test_project_names.py` keeps the tree that way. The commits already
  published (NOTICE and README in `origin/main`) still name the organisation
  where the project started: removing it from the history means rewriting
  published commits. Current default: the history is left as it is.
- **Plain HTTP to the compute host on a private network.** The deprecated
  `BC_*_ALLOW_INSECURE_TAILSCALE` flags still accept MagicDNS names
  (`*.ts.net`) without a warning, because the new `BC_*_INSECURE_NETWORKS`
  lists admit IP addresses only and have no replacement for names. Decide
  whether names should need HTTPS in a later release, and then warn about the
  flags. Current default: the flags keep working silently.
