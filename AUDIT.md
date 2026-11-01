# BananaChat security and stability audit

Branch: `security-audit`. Audit started 2026-10-27.

Each finding lists severity, how it was reproduced, the fix and the test that
guards it. Severities: **Critical** (remote takeover or mass data exposure
without privileges), **High** (privilege escalation, other users' data,
easy site-wide denial of service), **Medium** (exploitable with conditions or
limited impact), **Low** (hardening, defence in depth, minor leaks).

## Method

* Read the documentation (README, ARCHITECTURE, SECURITY, CHANGELOG, docs/).
* Parallel code review per subsystem against the threat model (anonymous,
  user, editor, unapproved/suspended/deleted user, API token or share-link
  holder, federation peer, plugin, hosting tenant, worker PC, sandboxed code).
* Every finding reproduced against the application (test client or a local
  server on 127.0.0.1) before the fix and re-run after it.
* Static analysis: ruff, bandit, pip-audit.

## Findings

### C0-1 Dependencies with published vulnerabilities (filelock, pypdf)
- **Severity:** Medium (pypdf parses user-uploaded PDFs). **Attacker:** any user who uploads an attachment.
- **Repro:** `pip-audit -r requirements.txt` and `pip-audit --no-deps -r requirements.lock`:
  filelock 3.32.7 (CVE-2026-112940, -112913, -112907, fixed in 4.2.1) and
  pypdf 6.21.1 (CVE-2026-115572, -115585, -115580, fixed in 6.21.2).
- **Fix:** `requirements.txt` allows `filelock>=4.2.1,<5` and `pypdf>=6.21.2,<7`;
  `requirements.lock` regenerated with `scripts/lock_requirements.py` (hash-pinned
  wheels for every target). The filelock API used (`FileLock`, `Timeout`) is unchanged.
- **Test:** `tests/test_requirements_lock.py` (lock complete and consistent);
  pip-audit clean on both files.

### C0-2 A test wrote a worker settings file into the checkout
- **Severity:** Low (stability/hygiene). **Attacker:** none.
- **Repro:** running `tests/test_worker_service.py` left `bananachat-worker.env`
  (a worker token template, mode 0600) in the working directory.
- **Fix:** an autouse fixture points `HOME` and `APPDATA` at the test's temporary folder.
- **Test:** the module itself; the checkout stays clean after a run.

### C3-1 A worker can capture administrators' connect-helper sessions to another worker's network
- **Severity:** Medium. **Attacker:** owner of any registered worker PC.
- **Repro:** two workers, "Evil PC" registered first and advertising `192.168.0.0/16`, "Office PC" advertising `192.168.1.0/24`; an admin access key sends `POST /remote/v1/sessions {"host":"192.168.1.20","port":22}` (what `ssh -o ProxyCommand="bananachat_connect.py proxy %h %p" admin@192.168.1.20` does). Observed: the session went to Evil PC (first in registration order whose networks contained the address) and `proxy` printed nothing about which worker answered. Expected: the session never reaches a worker that merely claims the network.
- **Fix:** `services/worker_remote.resolve()` now refuses (409, naming the workers and suggesting `--worker NAME`) any address that more than one worker shares, online or not, instead of picking one. Longest-prefix matching was not used because the owner chooses what a worker advertises, so an attacker can always advertise a narrower network (`192.168.1.0/25`) than the victim. The connect helper (`static/downloads/bananachat_connect.py`) gains `--worker` for `proxy` and `forward` (sent as the API's existing `worker` field) and always prints the worker that answered, also in `proxy` mode and per SOCKS connection. Set-ups without overlapping networks behave as before; overlapping set-ups (two homes on the same `192.168.1.0/24`) must now name the worker. Docs: docs/workers.md, `web/remote_api.py` docstring.
- **Test:** `tests/security/test_cworker.py::test_c3_1_a_worker_cannot_capture_sessions_to_another_workers_network`, `::test_c3_1_an_address_only_one_worker_shares_still_resolves`, `::test_c3_1_the_helper_names_the_worker_that_answered_in_proxy_mode`

### C3-3 Open browser terminal/VNC sessions survive sign-out everywhere, session revocation and password change
- **Severity:** Low. **Attacker:** holder of a stolen, recently confirmed admin session cookie.
- **Repro:** open a shell session to a worker as the admin in the browser, then `users.revoke_sessions(admin)` (sign out everywhere / password change). Observed: `still_allowed(row)` stays True and the relay keeps the terminal open (up to 2 h idle, 12 h total). Expected: the session ends within seconds.
- **Fix:** the admin WebSocket route (`web/admin/workers.py`) passes the login session (hash) that carries the WebSocket, which must belong to the session's creator, to `_tunnels.client_bridge`; the periodic re-check (`_tunnels._closed_by_administrator` -> `worker_remote.still_allowed(row, auth_session=...)`) now also requires that login session to still exist (`db/worker_remote.login_session_live`). A browser session with no login session is refused (fail closed). No schema change. Access-key sessions are unchanged (they are separate, revocable credentials; docs/workers.md now says to revoke them after a suspected compromise). `tests/app/test_worker_remote_access.py::test_a_session_ends_when_its_administrator_loses_the_role` was updated to bind its browser session to a sign-in, as the route does.
- **Test:** `tests/security/test_cworker.py::test_c3_3_a_browser_terminal_ends_when_its_sign_in_is_revoked`

### C3-5 One user can take both runner archive slots, so other users' archives are dropped and Git imports fail
- **Severity:** Medium. **Attacker:** user with cloud-session access, using only their own task.
- **Repro:** (C3/b repro, real `compute/sandbox_runner.py`) Mallory loops two concurrent uploads of a tiny zip to `/agents/<her task>/workspace/upload`; Alice starts a task with `project.zip`. Observed: Alice's archive got 503 "busy" through the 3 retries (~15 s), `_push_uploads` logged "upload failed" and deleted her stored files; Git imports failed the same way. Expected: one user cannot hold the shared slots, and a task's own files are never thrown away.
- **Fix:** `web/agents.workspace_upload` takes a per-person upload lease (`db/agents.claim_upload`/`release_upload`, one at a time across processes, like downloads) and a per-person cap of 30 uploads an hour. In `services/agents/loop.py`, `_runner_call(..., wait_for_room=True)` treats a runner 503 `busy` like sandbox capacity (waits up to `SANDBOX_WAIT_SECONDS` = 180 s, growing backoff) for the stored uploads and the Git import archive. `_push_uploads` copies file by file and keeps whatever was not copied (`uploads.retain`) for the task's next run when the runner still cannot take it or the run stops; only files the runner refuses for good are dropped, each with its notice. New i18n strings `agents.error_upload_busy`, `agents.notice_upload_kept` (en/it/de); docs/agents.md updated.
- **Test:** `tests/security/test_cworker.py::test_c3_5_workspace_uploads_are_one_at_a_time_and_capped_per_hour`, `::test_c3_5_a_task_start_waits_for_a_busy_runner_instead_of_dropping_its_archive`, `::test_c3_5_uploads_not_copied_are_kept_for_the_next_run`

### C3-7 A Git import download can outlast its documented 120 s limit when the server drips bytes
- **Severity:** Low. **Attacker:** whoever runs an allowed (administrator-added) Git host, or a very slow one.
- **Repro:** local HTTPS server sending `\x1f\x8b` then one byte every 0.5 s, `TOTAL_TIMEOUT=2`. Observed: after 12 s the download was still running (`HTTPResponse.read(65536)` keeps receiving until 64 KB arrive; the deadline was only checked between such reads; header lines likewise). Expected: `agents.import_timeout` after about 2 s.
- **Fix:** `services/agents/gitfetch.py` hands `http.client.HTTPResponse` a socket wrapper (`_DeadlineSocket`/`_DeadlineReader`) whose every `recv` sets the socket timeout to `min(READ_TIMEOUT, time left)` and fails with a timeout once the deadline passed, so the 120 s total holds for the status line, the headers and the body alike. Matches the existing promise in docs/agents.md.
- **Test:** `tests/security/test_cworker.py::test_c3_7_a_dripping_server_cannot_outlast_the_total_deadline`

### C3-9 The Ollama download registry allow-list can be bypassed with a dotless or numeric registry host
- **Severity:** Low. **Attacker:** administrator (including a stolen admin session: model downloads are exempt from re-authentication).
- **Repro:** `validate_ollama_name("intranet-host/ns/model")` returned `"intranet-host/ns/model:latest"` (also `localhost/…`, `2130706433/…`, `0x7f000001/…`, `metadata/computeMetadata/v1`), while `evil.com/ns/model` was refused; the worker's own `valid_name` had the same rule. Ollama treats the first of three segments as the registry host whatever it contains. Expected: refused.
- **Fix:** `services/pulls.registry_host()` reads names the way Ollama's `ParseName` does (optional `:tag` after the last `/`, then everything before the last two `/`-separated parts is the host), and `validate_ollama_name` requires that host, when present, to be in `TRUSTED_REGISTRIES`; `worker/model_commands.valid_name` applies the same rule. The previous dot/colon check is kept, so every name accepted before except dotless/numeric hosts (and 4+ segment names, which Ollama would also send to a foreign host) is still accepted.
- **Test:** `tests/security/test_cworker.py::test_c3_9_dotless_registry_hosts_are_refused`, `::test_c3_9_library_and_hugging_face_names_still_pass`

### C3-10 A hard link inside data/ makes every backup and rollback package unrestorable; a failed update then leaves the site down
- **Severity:** Medium. **Attacker:** code running as the service account (or an operator's `cp -al`/sync tool inside `data/`).
- **Repro:** fixture deployment, `os.link(data/upload.bin, data/upload-copy.bin)`, then an update whose new revision fails its health check. Observed: `write_package` (via `tarfile.add`) stored the second name as a hard-link member, `extract_archive` refused it ("Archive links and special files are not supported"), so `recover()` raised: services stopped, failed revision current, maintenance flag and `transaction.json` left behind, and every later backup/update/restore failed the same way; `manager.backup()` succeeded but `restore()` refused the package. Expected: a restorable package and a working rollback.
- **Fix:** `banana_ops/files.write_package` opens each file with `O_NOFOLLOW|O_NONBLOCK`, requires a regular file, writes it as a regular member (`TarInfo` from `fstat`, `addfile` from that descriptor) and records the SHA-256 of exactly the bytes archived (also closing the check-then-open race between `regular_files`, `digest_file` and `tarfile.add`). `read_package` extracts with `hard_links=True`, which turns a hard link to a regular member extracted earlier from the same archive into a copy, so packages written by older releases (including the rollback package of an update in progress when the fix is installed) restore. Release-archive extraction still refuses links. BananaWiki's `bananawiki/ops/files.py` has the same flaw (its `write_package` uses `archive.gettarinfo(fileobj=...)`, which still emits a hard-link member for the second name; reproduced: read_package refuses the package) and needs the same fix there.
- **Test:** `tests/security/test_cworker.py::test_c3_10_hard_linked_files_are_packaged_as_restorable_copies`, `::test_c3_10_packages_with_link_members_from_older_releases_still_restore`, `::test_c3_10_a_failed_update_rolls_back_despite_a_hard_link_in_data`

### C1-1 Device cookies collected before two-step verification multiply the code-guessing budget
- **Severity:** Medium. **Attacker:** someone who knows the password and signed in while two-step verification was still off (phished or reused password; the owner later turns 2FA on or signs out everywhere).
- **Repro:** Signed in N times with the password and kept each device cookie, then turned 2FA on for the account. Observed: without a cookie 10 wrong codes were checked before 429, with 30 stockpiled cookies 300 (one 10/5 min and 50/day bucket per cookie). Expected: one account-wide budget; cookies issued before the 2FA change no longer count.
- **Fix:** Device cookies are signed with the password hash and a per-account device epoch (`users.device_epoch`, new migration `v28_device_epoch`, `bananachat/security.py`). `users.revoke_sessions` clears the epoch, so turning 2FA on/off, an admin 2FA reset, password changes, suspension and sign-out-everywhere (account and admin) retire every device cookie; the browser that made the change from its own session gets a new one (`security.renew_device_cookie`). A browser keeps its nonce while its cookie is valid (no new cookie per sign-in). `services/two_step.py` adds an account-wide ceiling of 200 wrong codes a day whatever the browser (`reserve_code_attempt`). Existing device cookies stop counting once after the upgrade (UPGRADING.md); `tests/app/test_foundation.py::test_each_sign_in_issues_a_new_device_cookie` asserted the insecure behaviour and now asserts the nonce is kept.
- **Test:** `tests/security/test_cauth.py::test_c1_1_device_cookies_collected_before_two_step_do_not_multiply_code_guesses`, `::test_c1_1_known_browsers_share_a_daily_ceiling_of_wrong_codes`, `::test_c1_1_signing_in_again_keeps_the_device_nonce_until_signing_out_everywhere`

### C1-2 Cycling device cookies bypasses per-address sign-in limits; sessions per account unbounded
- **Severity:** Medium. **Attacker:** any account holder (open sign-up or invitation).
- **Repro:** With `BC_LOGIN_ATTEMPTS_PER_MINUTE=5`, a client without a device cookie got 429 after 5 sign-ins; the same client rotating its device cookie got 60/60 successful sign-ins (60 scrypt checks, 66 `auth_sessions` rows). Expected: known browsers bounded by their address; sessions capped.
- **Fix:** `bananachat/web/auth.py` (`_allow_attempt`): sign-ins and code attempts from a known browser also count against per-address buckets (`login-known`, `login-fail-ip-known`) with ten times `BC_LOGIN_ATTEMPTS_PER_MINUTE` / `BC_LOGIN_FAILURES_PER_ADDRESS` (`KNOWN_BROWSER_ADDRESS_FACTOR`), so strangers still cannot lock owners out but cookies cannot multiply the address allowance. `users.create_session` keeps at most 50 live sessions per account (`MAX_SESSIONS_PER_ACCOUNT`) and ends the least recently used. Combined with the kept nonce from C1-1. Documented in docs/configuration.md.
- **Test:** `tests/security/test_cauth.py::test_c1_2_cycling_device_cookies_stays_within_the_address_limit`, `::test_c1_2_an_account_keeps_a_bounded_number_of_sessions`

### C1-3 Password and code confirmations have separate per-endpoint budgets and no daily cap
- **Severity:** Low. **Attacker:** holder of a stolen browser session.
- **Repro:** Code reading, then test: 5 wrong passwords on `/account/password` and 5 on `/account/chats/delete-all` (each under its own 10/15 min limit) were all checked, and the correct password was then accepted; admin reauth TOTP failures were counted only in `reauth-fail:<id>` (10/15 min, no daily cap, not in the sign-in code budget).
- **Fix:** `security.confirm_password` counts wrong confirmation passwords per account across every form (`confirm-fail`, 10 per 15 minutes; `confirm-fail-day`, 50 a day), used by the account page (password change, two-step change, delete chats, delete account) and admin reauth (`web/admin/_reauth.py`). Reauth authenticator codes are reserved through `two_step.reserve_code_attempt`, i.e. the sign-in `two-step-fail:<id>`, `two-step-fail-day:<id>` and account-wide day buckets. Documented in docs/two-step-verification.md.
- **Test:** `tests/security/test_cauth.py::test_c1_3_password_confirmations_share_one_budget_across_forms`, `::test_c1_3_confirmation_codes_count_in_the_sign_in_daily_budget`

### C1-4 / C2-3 Suspension does not stop answers, images and agent tasks already running
- **Severity:** Low. **Attacker:** suspended user (or the holder of their API key).
- **Repro:** Code reading, then test: suspending an account while its chat answer or API completion streamed let it run to the end (the supervisor only watched `stop_requested` and the lease); deletion stopped it.
- **Fix:** `users.suspend` now sets `active_streams.stop_requested` for the account's runs (shared `_stop_streams` with `delete`) and asks its active agent tasks to stop as an administrator (`agents_db.request_stop_for_user`), in the suspension's transaction. The API/playground completion lease checkpoint (`CompletionRun._checkpoint`) and a new supervisor watch on image jobs (`ImageJob._account_check`) cancel work whose account is suspended or deleted (`users.suspended_or_gone`) and report `account_suspended` (403). Documented in docs/api.md.
- **Test:** `tests/security/test_cauth.py::test_c1_4_suspending_an_account_stops_its_chat_answer`, `::test_c1_4_suspending_an_account_stops_its_api_answer`, `::test_c1_4_suspending_an_account_stops_its_agent_tasks_and_images`

### C1-5 / C3-6 Account deletion leaves agent sandboxes, stored uploads and (admin delete) the background file behind
- **Severity:** Low. **Attacker:** none (privacy and retention); affects every deleted account.
- **Repro:** (a) Alice's finished task kept its sandbox; after `POST /account/delete` and `reap_sandboxes()` the sandbox was still on the runner. (b) A task whose Git import failed kept `agent-uploads/<task>/00.bin` ("SECRET customer data") after account deletion and `purge_old_tasks()`. Admin deletion did not remove the background image. Expected: all removed.
- **Fix:** Both deletion views read the account's tasks under the deletion's lock (`agents_service.workspaces_of_user`) and after the commit stop local runs, delete the sandboxes and discard stored uploads (`discard_workspaces`); the admin view also removes the background file. A run whose task row vanished deletes the sandbox it holds and its uploads (`TaskRun._discard_deleted` on `LeaseLost`). Fallbacks: `reap_sandboxes` deletes runner sandboxes whose session is a task id (32 hex) without a row once past `UNKNOWN_SANDBOX_GRACE` (previously skipped as "not ours"), and the retention job removes `agent-uploads/<id>/` folders without a task row. Compatibility: a runner shared by several web servers would now have the other servers' task sandboxes reaped; docs/agents.md and UPGRADING.md say to give each web server its own runner (the pairing model already assumes this).
- **Test:** `tests/security/test_cauth.py::test_c1_5_deleting_the_account_deletes_its_sandboxes`, `::test_c1_5_deleting_the_account_deletes_its_pending_uploads`, `::test_c1_5_cleanup_sweeps_leftovers_of_deleted_tasks`, `::test_c1_5_an_administrator_deleting_an_account_removes_its_background`

### C1-6 Edge-of-calendar datetimes give HTTP 500 in administrator forms
- **Severity:** Low. **Attacker:** administrator (typo; no security impact).
- **Repro:** `POST /admin/users/<id>/suspend` with `until=9999-12-31T23:59-23:00` and `POST /admin/invites/create` with `expires_at=0001-01-01T00:00+01:00` both returned 500 (`OverflowError` in `astimezone`).
- **Fix:** `web/admin/_helpers.py::utc_datetime` turns `OverflowError`/`ValueError` from the UTC conversion into the form's "out of range" error (all 8 admin views use it).
- **Test:** `tests/security/test_cauth.py::test_c1_6_edge_of_calendar_dates_are_refused_without_an_error`

### C1-7 User-role invitations outlive their creator's administrator role; creator suspension used the raw flag
- **Severity:** Low. **Attacker:** holder of a user-role invitation made by an administrator who was later demoted or deleted.
- **Repro:** Code reading, then test: an unlimited user-role invitation stayed usable after its creator was demoted or deleted; an invitation of a creator whose timed suspension had ended (not yet lifted) was refused.
- **Fix:** `db/invites.py::_usable`: every invitation needs its creator to exist, be an administrator and not be suspended per `users.is_suspended`. Only administrators create invitations and `created_by` has existed since the first schema, so a NULL creator means a deleted one. Compatibility: such invitations stop working (UPGRADING.md); `tests/app/test_untrusted_input.py::test_administrator_invitations_stop_working_when_their_creator_is_no_longer_an_administrator` asserted the old behaviour for user-role invitations and was updated.
- **Test:** `tests/security/test_cauth.py::test_c1_7_user_invitations_stop_with_their_creators_administrator_role`

### C2-1 A streaming client that stops reading pins its inference slot and request thread past the generation timeout
- **Severity:** High. **Attacker:** API-key holder (also any signed-in user via the playground or the Images page stream).
- **Repro:** `scratchpad/C2/stall_stream.py` (`BC_MAX_CONCURRENT=1`, `BC_GENERATION_TIMEOUT=10`): a raw-socket streamed completion with `SO_RCVBUF=2048` reads 200 bytes and stops. Observed: the attacker's `inference_queue` row stayed `running` at 5/15/30 s and a second account got `503 queue_timeout`; the place cleared only when the attacker closed the socket. After the fix the row is gone by 5 s and the second account gets 200. With real Gunicorn and `BC_WRITE_TIMEOUT=3` the stalled connection is closed by the server after 3 s and the partial answer is charged.
- **Fix:** Streamed API, playground and Images-page answers continue, once admitted, in a thread of their own (`services/event_pump.py`, `CompletionRun.detach`, `ImageJob.detach`; called in `web/api_v1.py`, `web/developer.py`, `web/images.py`); the response only drains a buffer in which consecutive deltas / queue positions / progress states are merged (bounded by the answer size, the work is cancelled past 10,000 unmerged events). Generation keeps its deadline, is charged and frees its queue place whether or not anyone reads; closing the response early still cancels and charges what was produced. Gunicorn has no write timeout, so `bananachat/write_timeout.py` (installed in `gunicorn.conf.py` `post_worker_init`) sets a socket timeout once the app returned its response (`BC_WRITE_TIMEOUT`, default 120 s, 0 = off; bodies split into 64 KiB writes, `TCP_NOTSENT_LOWAT` for sendfile, HTTP/2 and WebSockets untouched): the thread and its long-request place are freed, which also covers the web chat stream (its run was already decoupled). Documented in docs/architecture.md (Inference), docs/configuration.md, `.env.example`.
- **Test:** `tests/security/test_cinfer.py::test_c2_1_streamed_answer_does_not_wait_for_the_reader`, `::test_c2_1_playground_answer_does_not_wait_for_the_reader`, `::test_c2_1_streamed_image_does_not_wait_for_the_reader`, `::test_c2_1_a_write_to_a_client_that_stopped_reading_times_out`, `::test_c2_1_write_timeout_is_configurable`

### C2-2 Over-charge of the 5-hour token allowance via concurrent API requests
- **Severity:** Medium. **Attacker:** API-key holder.
- **Repro:** `scratchpad/C2/race_api.py`: 300-token API window, 10 parallel completions of ~211 tokens. Observed before: 2 × 200, 422 tokens charged against 300. After: 1 × 200, 9 × `429 insufficient_quota`, 211 tokens charged.
- **Fix:** When an API or playground answer leaves the queue, `CompletionRun._authorize` now checks the pool and holds what the answer may cost (estimated prompt + `num_predict`, × the model's weight, at most what is left) in one `BEGIN IMMEDIATE` transaction (`credits.hold_answer`, stored in `image_credit_reservations` next to image reservations, so no migration and every usage query already counts it). The hold is renewed by a supervisor watch, released before a fallback re-checks, replaced by the real charge in the settling transaction (and released in `_release` on every other path); a hold whose process died expires with the image reservations' TTL. Documented in docs/api.md (Token limits) and docs/architecture.md (Limits). Chat and agent pools are unchanged (one run per chat; out of the finding's scope).
- **Test:** `tests/security/test_cinfer.py::test_c2_2_a_running_answer_holds_the_allowance_it_may_spend`, `::test_c2_2_the_hold_is_given_back_when_the_answer_ends`

### C2-4 Three ordinary accounts can grant a fourth a 30-day unlimited token boost with no tokens renounced
- **Severity:** Low. **Attacker:** a few colluding ordinary users.
- **Repro:** `scratchpad/C2/test_c2_community.py`: `submit_request(kind="temporary", unlimited=True, hours=720, community=True)` and three supporters renouncing 0 tokens. Observed: approved, a 30-day `unlimited` grant, nobody lost allowance. After: the request is not put to a vote (`community=0`), and a row put to a vote by an earlier release can no longer be approved by votes.
- **Fix:** Unlimited use lifts every token limit, which no pledge can cover, so it is never community-approvable: `community.offered(..., unlimited=True)` is false (used by `credits.submit_request`), `voting_open`, `_settle` and `apply` refuse unlimited rows, and `count_open` skips them. Such requests wait for an administrator; temporary token increases still need pledges like 5-hour ones. The account page hint (en/it/de) and the admin Community-consent text say so; docs/architecture.md updated. `tests/app/test_community.py::test_unlimited_for_a_day_on_one_model_by_votes` expected the insecure behaviour and was rewritten as `..._needs_an_administrator`.
- **Test:** `tests/security/test_cinfer.py::test_c2_4_three_friends_cannot_grant_unlimited_use`

### C3-8 One API or playground request with a lone UTF-16 surrogate puts every Claude/Codex subscription account on a 5-minute cooldown
- **Severity:** Medium. **Attacker:** any user allowed to use a Claude Code or Codex subscription model (API token or playground).
- **Repro:** `scratchpad/C3/c/tests/test_c3c_pool_poison.py`: `"content": "hi \ud800 there"` returned 502 and rested all three pooled accounts; the next request got `503 provider_capacity_unavailable`. Variant: a request running past the CLI timeout without output rested every account in turn. After: 400 `invalid_text`, no account rested, the next request is answered; a timeout rests at most one account.
- **Fix:** `completions.parse_chat_request` rejects text parts, string content, `stop` sequences and `model` that are not valid Unicode (400 `invalid_text`). Both adapters raise `RequestRejected` when the transcript/system prompt cannot be encoded and `subscription_cli.ConnectorTimeout` (`timed_out = True`) on timeouts. `subscription_pool.chat_stream` no longer rests an account for errors raised before the adapter calls `subscription_pool.cli_started()` (just before `Popen` of the chat process; opt-in through `reports_cli_start = True`, so third-party adapters keep the old behaviour), and a timeout ends the request instead of trying the next account (rests the one account only when it produced nothing). Documented in docs/subscription-router.md and docs/api.md.
- **Test:** `tests/security/test_cinfer.py::test_c3_8_lone_surrogate_is_refused_and_rests_no_account`, `::test_c3_8_adapters_reject_text_they_cannot_encode`, `::test_c3_8_failures_before_the_cli_starts_rest_no_account`, `::test_c3_8_a_timeout_rests_at_most_one_account`

### C3-2 A worker can charge a user up to (prompt JSON bytes + 4096) tokens per request
- **Severity:** Low. **Attacker:** Chat worker PC.
- **Repro:** `scratchpad/C3/a/test_c3a_workers.py::test_worker_inflates_usage_charged_to_the_user`: a job with one ~3 MB image completed with `tokens_in = tokens_out = 2**31-1` was charged 4,004,166 prompt tokens against a server estimate of 769. After: at most 2 × the estimate + 4,096.
- **Fix:** `remote.complete` caps the reported prompt with `remote.prompt_token_cap`: `PROMPT_ESTIMATE_FACTOR` (2) × the server's own estimate (four characters per token of text, `IMAGE_TOKEN_ESTIMATE` per image, never the base64 bytes) + `PROMPT_TOKEN_ALLOWANCE`. The answer stays capped at its relayed bytes + 16 (bounded by what the user actually received). Honest under-reporting stays accepted. Documented in docs/workers.md.
- **Test:** `tests/security/test_cinfer.py::test_c3_2_worker_cannot_charge_thousands_of_times_the_prompt`

### C3-4 Worker job long-polls are not counted against the long-request cap
- **Severity:** Low. **Attacker:** Chat worker PC (or normal load with several workers).
- **Repro:** code reading confirmed by test: a waiting `GET /worker/v1/jobs/poll` held a thread for 25 s without touching `bananachat.long_requests`; with the long-request places all taken a poll still waited (holding one of the threads reserved for short requests).
- **Fix:** `web/worker_api.py` claims at once without a place and waits only while holding one of the places `security.long_request` counts (new `security.long_request_place()`, sharing its state; `long_request` refactored onto the same helper). With none free the poll answers 204 at once, which daemons already handle. Documented in docs/workers.md.
- **Test:** `tests/security/test_cinfer.py::test_c3_4_job_polls_count_as_long_requests`

### C4-1 Attachment and export downloads were served under the page CSP, not a sandbox
- **Severity:** Low (defence in depth). **Attacker:** a user who uploads a file another person (or an administrator) later opens.
- **Repro:** found by the headless-browser XSS harness (`scratchpad/xss`, surface
  `chat.attachment_download`): `GET /chat/<id>/attachments/<id>` for a file named
  `<svg onload=...>.html` answered `text/plain`, `nosniff`, `attachment` (not
  exploitable: images are re-encoded to PNG/JPEG, other files served as plain
  text) but with the site's script-enabling page policy instead of the
  `default-src 'none'; sandbox` that BananaWiki sends with every file.
- **Fix:** `app._security_headers` gives every response carrying a
  `Content-Disposition` (attachments, chat, personality and account exports,
  message PDFs) `Content-Security-Policy: default-src 'none'; sandbox`.
- **Test:** `tests/security/test_downloads.py::test_c4_1_*`.

### R5-1 One request the provider refuses (e.g. a prompt above the context window) rested every subscription account
- **Severity:** Medium (denial of service). **Attacker:** any user allowed a Claude or Codex subscription model (API token or playground).
- **Repro:** found in the re-audit (same class as C3-8, not covered by its fix):
  the CLI starts, then reports an error result ("Prompt is too long"). The
  pool blamed the account, rested it for 5 minutes and retried the same
  request on the next one until all three test accounts rested; the next
  request by anyone answered 503 `provider_capacity_unavailable`. One request
  every five minutes kept the pool down.
- **Fix:** `subscription_pool.chat_stream` rests at most one account per
  request for a generic failure: when a second account fails the same request,
  the request is refused without resting it (quota exhaustion, which is about
  the account, still rests each account that reports it).
- **Test:** `tests/security/test_pool_failures.py::test_r5_1_one_failing_request_rests_at_most_one_account`.

### R5-2 Community votes unlocked reasoning effort permanently and multiplied the request rate at no cost to supporters
- **Severity:** Low. **Attacker:** a few colluding accounts (7 days old by default, or one person's accounts where sign-up is open).
- **Repro:** found in the re-audit (`scratchpad/R5/community/test_r5_community_zero_pledge.py`):
  effort and rate requests carry no tokens, so consent needed no pledges.
  Three supporters renouncing 0 tokens approved an "all models, max effort"
  request (ceiling `medium` -> `max`, no end date) and a 50x rate request
  (7-day multiplier), lifting controls an administrator had set.
- **Fix:** migration `v29_community_kinds` takes `rate` and `effort` off the
  community board on sites still at the original default (every kind), and
  `community.DEFAULTS` offers only `window,weekly,temporary`. An administrator
  who wants the community to decide them can tick them again on
  **Administration → Quotas** (docs/architecture.md says what that allows); a
  site that had chosen its kinds keeps them. Breaking change recorded in
  UPGRADING.md: the secure default wins over the previous one.
- **Test:** `tests/security/test_community_kinds.py::test_r5_2_*`.

### R4-1 Idle worker polls held the places user streams need (re-audit, side effect of C3-4)
- **Severity:** Medium (denial of service without an attacker). **Attacker:** none needed (4 idle workers), or one worker token opening 4 polls.
- **Repro:** with `BC_HTTP_THREADS=8` (4 long-request places) and four idle
  workers waiting for jobs, a user's streamed completion got 503 `server_busy`;
  with the default 16 threads, idle polls held 4 of the 12 places for good.
- **Fix:** waiting job polls may hold at most a quarter of the long-request
  places (`security.POLL_SHARE`, at least one); further polls answer 204 at
  once, as before. Streams always keep the rest.
- **Test:** `tests/security/test_reaudit.py::test_r4_1_idle_worker_polls_leave_room_for_streams`.

### R4-2 One failing request per account still took a subscription pool offline (re-audit, what R5-1 left open)
- **Severity:** Medium (denial of service). **Attacker:** any user allowed a Claude or Codex subscription model.
- **Repro:** R5-1 bounded one request to one rest, but three requests whose
  prompt is above the model's context window rested all three accounts; the
  next ordinary request answered 503 `provider_capacity_unavailable`.
- **Fix:** both adapters report a "too long for the model" result (`Prompt is
  too long`, `context_length_exceeded`, context window) as `RequestRejected`,
  which rests no account; and each person's failing requests may rest at most
  one account per provider per cooldown (`subscription_pool._may_rest_for`,
  shared by all processes through `runtime_state`). The R5-1 test now uses an
  unexplained error result.
- **Test:** `tests/security/test_pool_failures.py::test_r4_2_*` (and `::test_r5_1_*`).

### R4-3 The workspace-upload lease was never renewed (re-audit, bypass of C3-5)
- **Severity:** Low. **Attacker:** a user with cloud-session access.
- **Repro:** one upload of up to 20 files, each allowed the runner's full
  transfer time, outlived its 120 s lease, so the same person could start a
  second upload and hold both runner archive slots (reproduced with the lease
  scaled to 1 s and runner writes slowed to 2.5 s).
- **Fix:** uploads have their own lease (`UPLOAD_LEASE_SECONDS` = 360, longer
  than one archive transfer) renewed before each file
  (`service.upload_to_workspace(before_each=...)`, `db.agents.renew_upload`).
- **Test:** `tests/security/test_reaudit.py::test_r4_3_an_upload_keeps_its_lease_while_it_sends_files`.

### R4-4 Invalid Unicode in browser JSON and personality files crashed with 500 (re-audit, rest of C3-8)
- **Severity:** Low (crash only). **Attacker:** any signed-in user.
- **Repro:** a lone surrogate (`"\ud800"`) in the JSON body of `POST /chat/<id>/send`
  or in an imported personality file answered 500 (C3-8 only covered the
  OpenAI-compatible API).
- **Fix:** the application's JSON loader refuses text that is not valid Unicode
  in any key or value (`app.InvalidJSONText`, a malformed-JSON 400 for every
  view; the `/v1` API keeps answering 400 `invalid_text`), and
  `personalities.parse_import` refuses such files as invalid.
- **Test:** `tests/security/test_reaudit.py::test_r4_4_lone_surrogates_are_refused_everywhere_not_crashing`.

### S2-1 One person could still hold both runner archive slots through workspace downloads (third pass, rest of R4-3)
- **Severity:** Low. **Attacker:** a user with cloud-session access.
- **Repro:** the download lease (120 s) was renewed only after the first chunk,
  but the runner sends nothing until `tar` of the workspace ends (up to 300 s,
  slower on purpose with the user's own files): the lease ran out and the same
  person started a second download; uploads and downloads also had separate
  leases, so one of each held both slots.
- **Fix:** one per-person transfer lease (`db.agents.claim_transfer`, 360 s, longer
  than the runner's longest transfer) for downloads, patches and uploads; the
  old function names stay as aliases.
- **Test:** `tests/security/test_reaudit.py::test_s2_1_one_person_holds_one_runner_transfer_of_any_kind`.

### S2-2 A crafted PDF crashed the chat send with a 500 (third pass, rest of R4-4)
- **Severity:** Low (crash only). **Attacker:** any signed-in user.
- **Repro:** a PDF whose ToUnicode map turns a glyph into U+D800: pypdf extracts
  `'Hello\ud800'` (it decodes maps with `surrogatepass`), the extraction child
  sends it back as JSON, and measuring the text for storage raised
  `UnicodeEncodeError`; `POST /chat/<id>/send` answered 500.
- **Fix:** `attachments._extract_pdf` replaces lone surrogates in the extracted
  text with U+FFFD.
- **Test:** `tests/security/test_reaudit.py::test_s2_2_a_pdf_with_a_lone_surrogate_does_not_crash_the_send`.

### T2-2 Background-image uploads decoded without the image decoding limit (fourth pass)
- **Severity:** Medium (denial of service). **Attacker:** any signed-in user.
- **Repro:** a 70 KB PNG of 16 megapixels with transparency added 253 MB at
  peak while being re-encoded; 11 concurrent uploads by one account (all within
  the 12 per 5 minutes) added 2,715 MB to one web process, all answered 200.
  Chat and API images are decoded two at a time per process and one per
  account; `POST /api/preferences/background` had no such limit. PDF text
  extraction (a child of up to a few hundred MB each) had none either.
- **Fix:** `customization.replace_background` decodes inside
  `attachments.decoding_image("user:<id>")` and answers 503 `busy` with
  `Retry-After` when no place is free; `_extract_pdf` runs at most
  `IMAGE_DECODES` extractions at once per process (busy otherwise).
- **Test:** `tests/security/test_reaudit.py::test_t2_2_*`.

### T2-1 A task start's archive copy did not take the person's transfer (fourth pass, rest of S2-1)
- **Severity:** Low. **Attacker:** a user with cloud-session access.
- **Repro:** while the person's own task start copied its uploaded archive (or a
  Git import) into the sandbox, a workspace download of an earlier task answered
  200: one person held both runner archive slots, so others' downloads and
  uploads got "busy" and their task starts waited.
- **Fix:** the task start's archive copies (Git import, stored uploads) take the
  owner's transfer (`TaskRun._transfer`, `db.agents.claim_transfer`), waiting for
  it like a busy runner and renewing it before each file.
- **Test:** `tests/security/test_agent_transfers.py::test_t2_1_a_task_start_and_a_download_by_one_person_do_not_run_at_once`.

### U2-1 Ids beyond 64 bits gave a 500 and let any user flush the error log (fifth pass)
- **Severity:** Low. **Attacker:** any signed-in user.
- **Repro:** `<int:...>` URL ids had no upper bound; above 2^63-1 SQLite raised
  `OverflowError`, a 500 with about 1.5 KB of traceback in the private error log
  (11 user routes, about 60 admin routes, and the `personality_id`, `model_id`
  and replacement-model form values). 7,097 requests by one user rotated an
  earlier entry out of both log files.
- **Fix:** the application's `int` URL converter stops at 2^63-1 (404 beyond);
  `personalities.usable`, the admin effort form and `model_lifecycle` parse
  those values with `ascii_int(..., 18)`.
- **Test:** `tests/security/test_reaudit.py::test_u2_1_ids_beyond_sqlite_integers_answer_404_not_500`.

### U2-2 One person's kept workspaces filled every sandbox, so everyone else's tasks failed (fifth pass)
- **Severity:** Medium (denial of service). **Attacker:** any user allowed cloud sessions.
- **Repro:** the start check counts running tasks only, but every finished task
  keeps its sandbox 30 minutes for downloads and the runner holds 4 by default:
  four short tasks in a row filled it, and another person's task (admins'
  included) waited 3 minutes and failed with "No sandbox became free in time";
  10 starts an hour keep it full.
- **Fix:** when the runner answers "capacity" to a starting task, `TaskRun`
  reclaims the kept workspace due to go first (`db.agents.kept_sandbox_to_reclaim`)
  and tries again: kept workspaces are best-effort and never block a run.
  docs/agents.md says so.
- **Test:** `tests/security/test_kept_workspaces.py::test_u2_2_kept_workspaces_of_one_person_give_way_to_another_persons_task`.

### U2-3 A task's transfer lease could run out during its waits, letting a second transfer start beside the copy (fifth pass, rest of T2-1)
- **Severity:** Low. **Attacker:** a user with cloud-session access.
- **Repro:** `TaskRun._transfer` took the 360 s lease before the sandbox existed and
  never renewed it through the capacity and busy waits (up to 180 s each) or
  copy retries; once it lapsed, the person's download took the second runner
  archive slot while the copy held the first (reproduced with the lease scaled
  to 1.5 s).
- **Fix:** the lease is renewed right before every runner call of the copy
  (`_runner_call(before_call=...)`, after any wait); when it was lost meanwhile
  it is taken again, or the copy waits as for a busy runner.
  `db.agents.renew_transfer` reports a lost lease.
- **Test:** `tests/security/test_agent_transfers.py::test_u2_3_a_task_does_not_copy_once_its_transfer_was_lost`.

### V2-1 Freeing kept workspaces ran on any 503 and without limit (sixth pass, from U2-2)
- **Severity:** Low (Medium where `BC_SANDBOX_MAX` > `BC_SANDBOX_MAX_EXECS`). **Attacker:** any user allowed cloud sessions; also happens under load. Victims: other users with finished tasks.
- **Repro:** with free places but every create answered 503 `busy` (engine slots
  taken, or the runner stopping), one task start deleted all three of another
  user's kept workspaces in a tight loop and still failed; a follow-up resuming
  a task between the pick and the delete could lose its live sandbox.
- **Fix:** a kept workspace is freed only on a real 429 `capacity`, at most one per
  sandbox start; `db.agents.take_kept_sandbox` takes it off its task in one
  step (only while no run holds the task) before the runner deletes it.
- **Test:** `tests/security/test_kept_workspaces.py::test_v2_1_*`.

### X2-1 A failed runner delete let one start take every kept workspace off its task (seventh pass, rest of V2-1)
- **Severity:** Low. **Attacker:** any user allowed cloud sessions (also under load). Victims: other users with finished tasks.
- **Repro:** with the runner full (429) and its DELETE failing (timeout, 503 when
  its connections are taken, a proxy 502/504), each attempt took the next
  person's kept workspace off its task and left it orphaned: one start stripped
  all three of another user's workspaces.
- **Fix:** a start makes one reclaim try at most, freed or not; when the runner
  cannot delete the workspace it is given back to its task
  (`db.agents.give_back_kept_sandbox`).
- **Test:** `tests/security/test_kept_workspace_races.py::test_x2_1_a_failed_runner_delete_still_frees_at_most_one_workspace`.

### X2-2 Recovery wrote a stale sandbox id back over a resumed task's new sandbox (seventh pass)
- **Severity:** Low (narrow race; the owner loses their own workspace).
- **Repro:** `_stop_leftovers` read sandbox S1, interrupted it (up to 30 s), then
  wrote S1 back unconditionally; when a reclaim took S1 and a follow-up gave the
  task S2 meanwhile, the row named the dead S1, S2 became unknown and the reaper
  deleted it after 10 minutes (forced interleaving).
- **Fix:** recovery marks only the sandbox it read, and only while no run holds
  the task (`db.agents.expire_sandbox_now`).
- **Test:** `tests/security/test_kept_workspace_races.py::test_x2_2_recovery_never_writes_back_a_sandbox_the_task_no_longer_has`.

### Y2-1 / Y2-2 Giving a taken workspace back could name a deleted sandbox or undo "remove now" (eighth pass, rest of X2-1)
- **Severity:** Low. **Attacker:** any user allowed cloud sessions, with a full and overloaded runner (also happens by itself).
- **Repro:** while the reclaim's runner DELETE hung (up to 60 s) the workspace was
  named by no task: the reaper's unknown sweep deleted it, and the give-back then
  wrote the dead id back (downloads 404, a follow-up lost a reset, the next start
  spent its one reclaim on it); and when recovery had marked a crashed run's
  sandbox for removal now meanwhile, the give-back restored its 30-minute keep
  time with the leftover processes still running.
- **Fix:** a workspace the runner no longer has is not given back, and one given
  back is due for removal now (`db.agents.give_back_kept_sandbox`), so the reaper
  retries the delete within a minute and a give-back never lengthens a keep time.
- **Test:** `tests/security/test_kept_workspace_races.py::test_y2_1_*`, `::test_y2_2_*`.

### Z-1 A given-back workspace could still be deleted by a reaper pass that listed it as unknown (ninth pass, rest of Y2-1)
- **Severity:** Low (heals within one reaper interval).
- **Repro:** the reaper read its known sandboxes while a reclaim held S1, the
  reclaim's DELETE failed and gave S1 back, then the reaper deleted S1 from its
  stale list: the row named a dead sandbox and the next start's reclaim counted
  it as freed.
- **Fix:** after the reaper deletes a sandbox no task named, it clears any row
  that names it since without a run (`db.agents.forget_sandbox`).
- **Test:** `tests/security/test_kept_workspace_races.py::test_z_1_a_workspace_the_reaper_deletes_after_its_give_back_is_forgotten`.

### F-1 Large upload bodies were read before the caller was known (confirmation pass; same as BananaWiki W3-2/R2-1)
- **Severity:** Medium (denial of service). **Attacker:** anonymous visitor (a plain user on administrator uploads).
- **Repro:** `prepare_request` raised `max_content_length` and `max_form_memory_size`
  to the view's `body_limit` before loading the user, then `check_csrf` parsed
  the form: anonymous POSTs to `/admin/music/tracks` (50 MiB) and `/agents` (20 MiB,
  up to 200) were spooled in full before their 400, a plain user's to the music
  upload before its 403, and a plain 45 MiB field was buffered in memory; with no
  global per-client limit in front, one client could keep every thread reading.
- **Fix:** the larger limit applies only after `load_current_user()` and only to
  callers the view serves (`app._may_send_large_body`: a signed-in, unsuspended
  account, an administrator for `admin.*` endpoints, or the bearer-token
  blueprints, which authenticate before reading); everyone else is held to the
  2 MiB default before CSRF reads anything. `tests/app/test_untrusted_input.py`
  expected the larger limit for an anonymous caller and now uses a signed-in one.
- **Test:** `tests/security/test_large_bodies.py::test_f_1_*`.

### G-1 Signed-in users without cloud sessions still got the agents views' large body limit (confirmation pass, rest of F-1)
- **Severity:** Low. **Attacker:** any signed-in user (anyone, where sign-up is open).
- **Repro:** with cloud sessions off (the default) a plain user's 18 MiB POST to
  `/agents` or `/agents/<id>/workspace/upload` was written to a temporary file by
  the CSRF check before the 400/403; F-1 narrowed only `admin.*` endpoints.
- **Fix:** `security.body_limit(..., serves=...)` names who a view serves;
  `app._may_send_large_body` applies it, and both agents views serve only users
  `service.check_access` lets in. Everyone else is held to the 2 MiB default.
- **Test:** `tests/security/test_large_bodies.py::test_g_1_a_user_without_cloud_sessions_gets_no_large_body_on_their_views`.

### FC2-1 Remote-access polls took long-request places outside the poll share (full re-audit, rest of R4-1)
- **Severity:** Low (with `BC_WORKER_REMOTE_ACCESS=1`). **Attacker:** a worker PC (or idle workers armed by an administrator).
- **Repro:** one worker token looping one job poll and two `/worker/v1/remote/poll`
  held 3 of the 4 long-request places at `BC_HTTP_THREADS=8`; a second concurrent
  user stream got 503 `server_busy`.
- **Fix:** `remote_poll` claims waiting sessions at once and waits only inside
  `security.long_request_place(poll=True)`, the share job polls use; otherwise it
  answers 204 at once (which daemons already handle).
- **Test:** `tests/security/test_reaudit.py::test_fc2_1_remote_access_polls_share_the_poll_places`.

### FC2-2 A worker could write terminal escape sequences through the connect helper (full re-audit)
- **Severity:** Low. **Attacker:** a worker PC, against an administrator using `bananachat_connect.py` `proxy`, `forward` or `socks`.
- **Repro:** a worker's `{"type": "error", "message": ...}` text frame was printed to
  the helper's stderr unfiltered: OSC 52 (set the clipboard), line erase and
  cursor-up reached the administrator's terminal; a non-object JSON message
  ended the helper's receiving thread.
- **Fix:** the helper prints server and worker text through `clean()` (printable
  characters only, at most 500), including every `Problem`, worker names,
  addresses and list output, and ignores JSON messages that are not objects.
  The `shell` subcommand still passes the terminal's bytes, as a terminal must.
- **Test:** `tests/security/test_reaudit.py::test_fc2_2_the_connect_helper_never_prints_a_workers_escape_sequences`.


### FC1-1 A personal-data export held the whole account in memory about three times over (full re-audit)
- **Severity:** Medium (the process runs out of memory). **Attacker:** any signed-in user.
- **Repro:** an account storing 110 MB of attachment text (the default account
  limit is 1 GiB) raised the worker's peak memory by 339 MB per
  `/account/export/chats`, 403 MB by 1,220 MB; exports were not limited to one at
  a time, so a few parallel requests could exhaust memory, and the routes
  accepted GET (a cross-site link started one).
- **Fix:** `exports.stream()` writes the same JSON document piece by piece:
  chats, messages (100 at a time), attachment text and images (about 4 MB at a
  time) and the longer lists (ledger, audit log, no-history copies, agent steps)
  are read in batches while they are written. The account and administrator
  exports stream it, hold a long-request place, and run one at a time per
  account (a mark in `rate_limit_hits`, removed when the download ends, expires
  after 15 minutes); a second gets 429. They start only by POST; GET on the
  same addresses leads to the account or user page. Same output; `gdpr_export()`
  and `chats_export()` still return dicts. Measured after: +30 MB at 110 MB and
  at 403 MB stored.
- **Test:** `tests/security/test_reaudit.py::test_fc1_1_exports_are_streamed_with_bounded_memory`,
  `::test_fc1_1_one_export_per_account_at_a_time_and_only_by_post`.

### FC1-2 Chat message text had no per-account storage limit (full re-audit)
- **Severity:** Medium (disk exhaustion takes the site down). **Attacker:** any signed-in user.
- **Repro:** with the default limits, one account sent 100 KB messages as fast as
  the 1/s request rate allowed (stopping each run at once), storing about
  0.5 GiB an hour; only the per-message cap and request rate applied, and
  no-history chats kept a second copy for administrators indefinitely.
- **Fix:** `BC_CHAT_MAX_ACCOUNT_TEXT_MB` (new, default 1024) limits the message
  text an account stores: `chats.account_text_bytes()` sums the UTF-8 size of
  the messages in its chats (deleted ones until erased) and of its no-history
  copies (`octet_length`, which reads the size without the text, on SQLite
  3.43+). A send over it is refused with 413 `storage_limit` and a message in
  en/it/de; administrators are exempt. Existing accounts over the limit keep
  reading and deleting.
- **Test:** `tests/security/test_reaudit.py::test_fc1_2_message_text_counts_towards_the_account_storage_limit`.

### GC0-1 The installer lock pinned setuptools 84.0.0 (GHSA-xmj3-9gf7-h52w) (full re-audit, pip-audit)
- **Severity:** Low (Windows installs only: a wheel's `namespace_packages.txt` could create an empty `__init__.py` outside the target directory through setuptools' legacy wheel path). **Attacker:** a malicious package on the install path.
- **Repro:** `pip-audit -r requirements-build.lock --no-deps --disable-pip` reported it; the other lock files and requirement lists were clean.
- **Fix:** `requirements-build.lock` pins setuptools 84.2.1 with its PyPI hashes (the version BananaWiki pins); a hash-checked, wheels-only download of the lock succeeds.
- **Test:** pip-audit of every lock file reports no known vulnerabilities.

### GC1-1 Slow readers of chat downloads could take every server thread (full re-audit pass 2)
- **Severity:** High (site-wide denial of service). **Attacker:** anyone with a share link (anonymous), or a signed-in user on their own chats.
- **Repro:** a ~30 MB shared chat on a live Gunicorn (`BC_HTTP_THREADS=8`, one
  worker, default `BC_WRITE_TIMEOUT=120`): 8 clients reading
  `/share/<token>/download` at about 8 KiB/s each held all 8 threads, and every
  `/health` probe went unanswered for the whole 70-second run. The write timeout
  only ends a client that stops reading entirely, and the Markdown downloads were
  neither long requests nor limited per client.
- **Fix:** `/chat/<sid>/download` and `/share/<token>/download` are
  `@security.long_request`, so they never take the threads kept for short
  requests (health checks, pages), and each client (account, or address for share
  links) may have at most `security.DOWNLOADS_PER_CLIENT` (2) of them open per
  process (`security.download_place`); more get 429. The place is given back
  when the download ends or is closed.
- **Residual:** many addresses reading slowly can still fill the long-request
  places (then streams get 503 `server_busy` while pages keep answering), as with
  any per-client limit; a proxy-level per-address connection limit narrows it.
- **Test:** `tests/security/test_reaudit.py::test_gc1_1_chat_downloads_take_long_request_places_and_a_few_per_client`.

### GC1-2 The one-export-per-account mark expired during a long download (full re-audit pass 2, rest of FC1-1)
- **Severity:** Medium. **Attacker:** a signed-in user reading their exports slowly.
- **Repro:** with an export still downloading after 15 minutes (the mark's
  window), the same account started more exports, each holding a long-request
  place, until other users' streams got 503 `server_busy`.
- **Fix:** the download renews its mark (`users.renew`) at least once a minute
  while it is read, and hands the server at most 16,384 characters (64 KiB of
  UTF-8, one write bounded by `BC_WRITE_TIMEOUT`) at a time, so the mark is renewed
  however slowly the client reads; it is still removed when the download ends and
  expires 15 minutes after a server stopped without removing it.
- **Test:** `tests/security/test_reaudit.py::test_gc1_2_the_running_export_mark_is_renewed_while_the_download_goes_on`.

### GC2-1 Files added to cloud-session starts stayed on the web server's disk without bound (full re-audit pass 2)
- **Severity:** Medium. **Attacker:** any user allowed cloud sessions (off by default; administrators only by default).
- **Repro:** `POST /agents` with a 2 MiB file, then at once `POST /agents/<id>/stop` (the sandbox start takes about 0.5 s on a real engine), five times. Observed: every task `stopped` before its files were copied, `agent-uploads/<task>/` kept all 10 MiB, and `purge_old_tasks()` and `reap_sandboxes()` kept them too (the tasks exist and are younger than the 30-day retention). Only `BC_AGENTS_MAX_UPLOAD_MB` per start limited it, so repeating start and stop filled the web server's disk. Expected: files no run can use any more are deleted, and what one account keeps is bounded.
- **Fix:** `bananachat/services/agents/uploads.py`, `service.py`, `loop.py`, `config.py`. Copied files were already deleted at once (`TaskRun._push_uploads` -> `uploads.retain`); files a run could not copy stay for the task's next run (any ended task can be continued with a follow-up), but now only for `uploads.KEEP_UNCOPIED` (a day) after that run ended: the hourly retention job (`purge_old_tasks` -> `purge_stored_uploads`, which replaces `_purge_orphaned_uploads` and still removes folders whose task no longer exists) deletes them. The run's notice says so (`agents.notice_upload_kept_day`, en/it/de; the old key stays for stored steps). `BC_AGENTS_MAX_STORED_UPLOAD_MB` (new, default 100, never below `BC_AGENTS_MAX_UPLOAD_MB`) limits the files one account has waiting; a start with files over it gets 413 `storage_limit` (`agents.error_upload_account`), a start without files is not refused, administrators are exempt (as with the chat storage limits). To count concurrent starts without writing large files under the database write lock, `start_task` stages the files in `agent-uploads/incoming-<task>/` with an `owner` file naming the account, checks the limit inside the start's transaction (counting staged folders too), and moves the folder into place after the task row is committed (`uploads.stage` / `commit`); a refused or failed start discards it, and the retention job removes staged folders older than 10 minutes left by a crash. Workspace uploads into a live sandbox (`/agents/<id>/workspace/upload`) store nothing on the web server (they go straight to the runner under the person's transfer lease), so the limit does not apply there. Separate from the chat attachment and text limits, which count database rows rather than temporary files. Compatibility: files waiting from before the update are deleted a day after their task's last run; `uploads.store` is kept. docs/agents.md, docs/configuration.md, .env.example, CHANGELOG, UPGRADING.
- **Test:** `tests/security/test_gc2.py::test_gc2_1_stored_uploads_are_limited_per_account`, `::test_gc2_1_files_of_starts_being_decided_count`, `::test_gc2_1_uncopied_uploads_are_kept_for_a_day_only`, `::test_gc2_1_copied_uploads_are_deleted_and_runs_still_get_them`

### GC2-2 One person's workspace browsing took the sandbox runner's engine slots from everyone's commands (full re-audit pass 2)
- **Severity:** Medium. **Attacker:** a user with cloud-session access who owns a task with a workspace (a running task, or a finished one within `keep_workspace_minutes`).
- **Repro:** Real `RunnerServer` + `SandboxManager` with the runner tests' `FakeEngine` (each engine call 0.15 s), `BC_SANDBOX_MAX_EXECS=8`. Mallory requested `GET /agents/<her task>/workspace?path=/workspace` in a loop from 16 threads (tabs) while Alice's task ran four `bash` commands. Observed: Alice's commands were refused with 503 "The sandbox runner is busy" after the loop's retries, failing the task (`agents.stop_runner_failed`) or most of its commands. Expected: other people's commands keep running; the browsing person is told to wait.
- **Fix:** Both layers. Runner (`compute/sandbox_runner.py`): `SandboxManager._slot(box)` also counts the operations of each sandbox (`Sandbox.slots`); one sandbox may run at most `Config.exec_share` = `BC_SANDBOX_MAX_EXECS_PER_SANDBOX` (new; 0 = automatic: `BC_SANDBOX_MAX_EXECS` ÷ `BC_SANDBOX_MAX`, at least 2 so a command and its owner's browsing fit, at most `BC_SANDBOX_MAX_EXECS`) commands, file and archive operations at once, answering the existing 503 `busy` (`Retry-After: 2`) beyond, so the protocol is unchanged for existing web servers. With the defaults every sandbox's share fits in the slots, so no sandbox can starve another. Web server (`bananachat/services/agents/service.py` `_read_workspace`, used by `browse` and `file_bytes`, i.e. `/agents/<id>/workspace` and `/workspace/file`): the owner holds a lease in `runtime_state` (`db.agents.claim_workspace_read`, one read at a time across processes and tabs, `WORKSPACE_READS_AT_ONCE`) and at most `WORKSPACE_READS_PER_MINUTE` (60); beyond either the view answers 429 with `Retry-After` (`agents.error_workspace_reading`, `agents.error_workspace_rate`, en/it/de) instead of reaching the runner. A runner 503 `busy` on the workspace views is now 503 `busy` with `Retry-After: 5` (`agents.error_runner_busy`) instead of "unavailable" for 30 s. The task page retries a read refused as busy once (`agent-task.js`). Either layer alone keeps Alice's task finishing in the regression test. docs/agents.md (runner settings, exec API, threat table, workspace), CHANGELOG, UPGRADING (runner and web server can be updated in either order). Not changed: these read views still do not call `check_access` (people who lost access can still read and download their tasks, as documented).
- **Test:** `tests/security/test_gc2.py::test_gc2_2_one_sandbox_holds_at_most_its_share_of_engine_slots`, `::test_gc2_2_share_setting`, `::test_gc2_2_a_person_reads_one_workspace_path_at_a_time`, `::test_gc2_2_workspace_reads_are_rate_limited`, `::test_gc2_2_workspace_browsing_does_not_fail_other_peoples_tasks`

### GC2-3 A suspended account's stopped task could say "stopped by an administrator" (full re-audit pass 2, stability)
- **Severity:** Low (wrong reason shown; flaky test). **Attacker:** none.
- **Repro:** `tests/app/test_agent_runtime_admission.py::test_retry_rechecks_admission_after_first_provider_failure[suspension]`
  failed about once in three full-suite runs: suspending an account stops its tasks through the
  administrator stop (the fix for suspension cutting work off), which raced the task's own step
  check and recorded `agents.stopped_admin` instead of `agents.stop_account`.
- **Fix:** `loop._stop_for_cancel` reports `agents.stop_account` for an administrator stop whose
  account is suspended or deleted, as the step check does.
- **Test:** the test above, now deterministic (passing in repeated full-suite runs).

### IC1-1 Slow readers of attachments and other file responses could take every server thread (full re-audit pass 3, rest of GC1-1)
- **Severity:** High (site-wide denial of service). **Attacker:** any signed-in user, on their own files.
- **Repro:** live Gunicorn with the defaults (`BC_WRITE_TIMEOUT=120`, 8 threads): 8 clients reading
  their own 4.9 MiB image attachment (`/chat/<sid>/attachments/<id>`) at about 2 KiB/s held every
  thread for the whole 60-second run and every `/health` probe went unanswered; the same through
  `/customization/background/<file>` (sendfile). GC1-1 had bounded only the Markdown downloads.
- **Fix:** `security.download` for every view that sends a file or another large body
  (`chat.attachment`, `chat.message_pdf`, `personalities.export`, `customization.background`,
  `music.audio`, `admin.metrics_export`): the response takes a long-request place, so the threads
  kept for short requests stay free (503 `server_busy` when none is left), and one of the client's
  `security.FILES_PER_CLIENT` (6, about what a browser asks for at once) places per process (429);
  both are given back when the response is closed (for files sent with sendfile, when the server closes
  the file wrapper: Werkzeug hands it over without the response's own close, which a first version
  missed, found in review). Chat images that were refused are asked for once
  more after a moment (chat.js `retryOnce`).
- **Residual:** as for GC1-1, many accounts reading slowly can still fill the long-request places
  (streams and files then get 503 while pages and health checks answer).
- **Test:** `tests/security/test_reaudit.py::test_ic1_1_file_responses_take_long_request_places_and_a_few_per_client`
  (also checks that each listed view carries `security.download`).
  `::test_ic1_1_files_sent_with_sendfile_give_their_places_back`.

### JC1-1 Shared chats and chat pages had no size limit: anonymous slow readers took the site down (full re-audit pass 4)
- **Severity:** High. **Attacker:** anyone with a share link; a signed-in user on their own chats.
- **Repro:** a chat filled within the send limits (200 messages of 100 KiB of `&`, 20 MB) made
  `/share/<token>` a 97.8 MiB page (HTML escaping); on a live Gunicorn (1 process, 16 threads)
  16 anonymous slow readers held every thread, `/health` went unanswered for the whole 45 s, and
  the worker grew from 53 MiB to 1,965 MiB. `/chat/<sid>` was 29.4 MiB, `/chat/<sid>/messages` 4.9 MiB.
- **Fix:** `chats.message_page(max_bytes=...)` reads the messages' sizes first (`octet_length`) and
  stops a page at 1 MiB of text (`web.chat.PAGE_TEXT_BYTES`, at least one message; the rest
  behind "show earlier"), for the chat page, its message pages and share links (4 MiB for the
  administrator's view); share links answer 60 views a minute per address. Their large pages
  take download places like every large response (JC1-2).
- **Test:** `tests/security/test_reaudit.py::test_jc1_1_chat_pages_stop_at_a_size_and_share_views_are_limited`.

### JC1-2 One account could hold every long-request place with slow downloads (full re-audit pass 4, rest of GC1-1/IC1-1)
- **Severity:** Medium. **Attacker:** any signed-in user.
- **Repro:** with `BC_HTTP_THREADS=8` (4 long-request places) alice read her own 4.9 MB attachment
  slowly on 4 connections (IC1-1 allowed 6 files plus 2 downloads per client); every other
  user's download, stream and answer then got 503 `server_busy` (pages stayed up).
- **Fix:** large responses no longer use the answers' long-request places. Gunicorn runs
  `BC_DOWNLOAD_THREADS` (new, default 8) threads on top of `BC_HTTP_THREADS`, and every
  response over 64 KiB or streamed (except event streams, which hold a long-request place) takes
  one of that many download places, at most a quarter for one account or address
  (`security.hold_large_response` in `after_request`, released when the server closes the
  response, sendfile included). This replaces GC1-1's and IC1-1's per-view caps: no view can be
  forgotten. Over the places: 503, over a client's share: 429, both with Retry-After.
- **Residual:** four clients (accounts, or addresses for visitors) reading slowly can take every download place of a process;
  downloads then get 503 while pages, answers and streams keep their own threads.
- **Test:** `tests/security/test_reaudit.py::test_gc1_1_jc1_2_large_responses_take_download_places_not_the_threads_for_everything_else`,
  `::test_ic1_1_every_large_response_holds_a_place_until_it_is_closed`,
  `::test_ic1_1_files_sent_with_sendfile_give_their_places_back`.

### JC1-3 Small responses on slow connections stopped Gunicorn from accepting any connection (full re-audit pass 4, from the BananaWiki JW1-2 repro)
- **Severity:** Medium (High where Gunicorn is reachable without the reverse proxy). **Attacker:** anonymous.
- **Repro:** live Gunicorn 26.2.0 (`BC_HTTP_THREADS=8`): 24 connections each pipelining 12 requests
  for the 17 KB `/login` page and reading 512 B/s left `/health` unanswered while every request
  thread was idle. Gunicorn's gthread worker closes each finished connection on its main loop, the
  one that accepts connections, with `util.close_graceful`, which lingers up to 2 s until the
  client closes its side; small pages also blocked their thread once the client's tiny window
  left the default 16 KB send buffer full. `keepalive = 0` alone made it worse (every request
  then ends in that close). Gunicorn 26.2.0 is the latest release.
- **Fix:** `gunicorn.conf.py` replaces `gunicorn.util.close_graceful` with `close_without_waiting`
  (FIN, read only what already arrived, close: no reset that cuts responses short, never a wait),
  serves one request per connection (`keepalive = 0`; the local proxy reconnects at almost no
  cost), and `write_timeout.guard` gives each connection a 256 KiB send buffer so any response
  under the 64 KiB download threshold leaves the thread at once. Live after: `/health` 200 in
  0.0 s throughout the same attack.
- **Test:** `tests/security/test_reaudit.py::test_jc1_3_finished_connections_never_hold_the_accept_loop`,
  `::test_jc1_3_responses_get_a_send_buffer_for_small_pages`.

### LC2-1 A worker's claimed job, an API answer or an export could be refused after the work was done (full re-audit pass 5, rest of JC1-2)
- **Severity:** Medium. **Attacker:** anonymous visitors from four addresses reading slowly (or, with no attacker, three worker PCs behind one NAT address).
- **Repro:** with every download place held, `GET /worker/v1/jobs/poll` claimed a job over 64 KiB
  and `hold_large_response` then replaced the reply with 503 `server_busy`: the worker never saw
  the job, the daemon paused 60 s and the user waited for the claim timeout. Three workers on one
  address got 429 the same way. Non-streamed `/v1` answers and images, and account exports
  (marked running and audited), were also built before the refusal.
- **Fix:** answers of the token APIs (`worker_api`, `remote_api`, `api_v1`) are never refused by
  the download places (they take one when free; their views hold long-request places of their
  own). Views that do work before answering take their download place first
  (`security.download_first`: account and administrator exports, the agents workspace archive and
  patch), so a refused download is neither built, counted nor audited; a place they took is given
  back after a small answer or an error (`security.release_download_place` at teardown).
- **Test:** `tests/security/test_lc2.py::test_lc2_1_full_download_places_never_lose_a_claimed_job`,
  `::test_lc2_1_workers_sharing_an_address_get_their_claimed_jobs`,
  `::test_lc2_refused_exports_are_neither_built_nor_audited`.

### LC2-2 Slow readers of the agents workspace archive held the answers' long-request places (full re-audit pass 5, rest of JC1-2)
- **Severity:** Low (cloud sessions are off and administrator-only by default). **Attacker:** a person allowed cloud sessions.
- **Repro:** four such people opening their own task's archive without reading it held all 4
  long-request places at `BC_HTTP_THREADS=8`; a fifth user's chat send got 503 `server_busy`.
- **Fix:** `workspace_archive` holds a long-request place only while it fetches the archive from
  the runner (`security.long_request_place`), then sends the spooled file on its download place.
- **Test:** `tests/security/test_lc2.py::test_lc2_2_slow_archive_readers_leave_the_answers_places_free`.

### LC1-3 An export refused for lack of a download place still counted and was audited (full re-audit pass 5)
- **Severity:** Low. **Attacker:** none needed (busy server).
- **Repro:** with every place held, ten exports got 503 but used the hourly budget (the next got 429,
  `Retry-After: 600`) and wrote ten audit entries; the administrator's export the same.
- **Fix:** with LC2-1: the export views take their download place first (`security.download_first`).
- **Test:** `tests/security/test_lc2.py::test_lc2_refused_exports_are_neither_built_nor_audited`.

### LC1-2 The chat status poll had no size limit (full re-audit pass 5, rest of JC1-1)
- **Severity:** Low (own chats; the answer takes a download place). **Attacker:** a signed-in user.
- **Repro:** `GET /chat/<sid>/status?after=0` returned up to 50 messages whatever their size:
  50 messages of 100 KiB of control characters gave a 29.3 MiB answer and a 64 MiB peak.
- **Fix:** `chats.messages_after(max_bytes=...)` reads sizes first and stops at the text of one
  message plus its longest answer (`BC_CHAT_MAX_MESSAGE_KB` + `BC_CHAT_MAX_RESPONSE_KB`, at least
  1 MiB); the page asks again from the last message it got.
- **Test:** `tests/security/test_reaudit.py::test_lc1_2_the_status_poll_stops_at_a_size`.

### LC1-1 Slow request bodies hold server threads (full re-audit pass 5)
- **Severity:** High (reported). **Attacker:** anonymous.
- **Report:** Gunicorn reads request bodies on a blocking socket without a deadline (the write
  timeout is set only once the application has answered), and the shipped Caddy configuration
  passes bodies through as they arrive (`read_body 5m`), so clients sending bodies very slowly can
  hold the request threads.
- **Fix (partial) and accepted risk:** the generated Caddyfile (`banana_ops/profile.py`) now gives a
  request body at most 2 minutes (`read_body 2m`, was 5m), so a slow sender holds a request thread at most
  2 minutes per connection; the largest ordinary uploads (20 MB of cloud-session files) still fit at
  about 170 KB/s. **Accepted risk:** many connections sending slowly can still occupy threads for up to
  2 minutes each, as on any threaded server; bounding it fully needs request buffering in the proxy
  (`request_buffers`) or a body deadline in Gunicorn, which are deployment choices (see the summary).
  Gunicorn reached directly, without the proxy, is not bounded.
- **Test:** `tests/test_ops_cloudflare.py` checks the generated `read_body 2m`.
- **Later fix (decision: limit abusive slow behaviour):** `bananachat/read_timeout.py`, installed in
  `gunicorn.conf.py` next to the write timeout, wraps each request body. A read that stalls for
  `BC_BODY_STALL_TIMEOUT` seconds (default 20) fails, and a body that takes longer in total than 10 seconds
  plus its length at `BC_BODY_MIN_KBPS` (default 32 KB/s; no declared length: 2 minutes) is cut off by a timer
  that ends the connection's read side. The request ends with 408 and the thread is free. Checked live with
  Gunicorn: a client sending one byte every 5 seconds was answered 408 after 10 seconds (it held the thread
  indefinitely before), and an ordinary POST was unaffected. Gunicorn reached directly is now bounded for
  bodies too; slow request *headers* are still left to the proxy's `read_header 10s`.
- **Test:** `tests/security/test_read_timeout.py`
### MC1-1 The administrator's no-history audit page has no size limit (full re-audit pass 7)
- **Severity:** Medium. **Attacker:** any signed-in user; the victim is an administrator opening the no-history audit page.
- **Repro:** a no-history chat with 120 copies of 100 KB of `&` (sent through `POST /chat/<sid>/send`, then erased by its owner) made `GET /admin/incognito/<sid>` render all 121 entries in one 58.5 MiB page with a 175 MiB Python peak (5x the stored text from HTML escaping, about 14x in memory); only the 1 GiB account text limit bounded it. Expected: a page bounded like `/admin/chats/<sid>` (4 MiB, "show earlier").
- **Fix:** `chats.audit_entries` (bananachat/db/chats.py) now pages like `message_page`: it reads the entries' sizes first (`octet_length`), keeps at most 500 entries or 4 MiB of text (at least one), newest first, and returns whether earlier entries exist; `admin.incognito_session` takes a `before` id (400 if malformed) and the template links "Show earlier entries" / "Show the latest entries". Deleting an entry checks for remaining entries with `chats.has_audit_entries` instead of loading them all. URLs and delete buttons unchanged.
- **Test:** `tests/security/test_m5.py::test_mc1_1_no_history_audit_page_stops_at_a_size`

### MC1-2 A refused database (migration) export is still built in full and audited (full re-audit pass 7)
- **Severity:** Low. **Attacker:** none needed (a busy server, or an administrator with two large responses open); a confirmed administrator repeating the export makes it worse.
- **Repro:** with the administrator holding both of their download places, three `POST /admin/migration/export` answered 429 but each ran `sqlite_snapshot.snapshot` (a full copy of the database into `instance/.export-*`) and wrote an `admin.migration_export` audit entry before `hold_large_response` refused it. The per-user export under the same conditions answered 429 with no audit entry. Expected: refused before any work.
- **Fix:** `export()` in bananachat/web/admin/migration.py now has `@security.download_first` (under `@admin_required`, before the confirmation check), like the other export views: a refused export is neither copied nor audited, and an accepted one keeps its place until the stream is closed.
- **Test:** `tests/security/test_m5.py::test_mc1_2_a_refused_database_export_is_neither_copied_nor_audited`

### MC1-3 Any user can grow the never-purged audit log without bound (full re-audit pass 7)
- **Severity:** Low. **Attacker:** any signed-in user (anyone, where sign-up is open).
- **Repro:** 500 `POST /account/sessions/revoke-others` from one ordinary user in 1.3 s all answered 302 and added 500 `account.sessions_revoke_others` rows; 200 `POST /personalities/<id>/unshare` on a never-shared personality added 200 `personality.unshare` rows. A second review found the same through `POST /account/sessions/<id_hash>/revoke`: signing in again (throttled only at 60 a minute per browser, 600 a minute per address) and ending that session from another one added 61 `account.session_revoke` rows in 4 s with one browser and 305 in 18.8 s with five device cookies, up to about 600 rows a minute per address (about 864k rows, roughly 150 MB, a day). The audit log is never purged and survives account deletion. Expected: a bounded number of audit rows per account, without refusing the owner's sign-outs.
- **Fix:** neither sign-out is ever refused, so the owner can always end a stranger's session; instead their audit rows have per-account budgets, and further ones are written to the application log instead of the audit log. A 429 limit was not used because whoever holds a session in the account (with the password, or only a stolen cookie) could use up the owner's budget and keep it used up, renewing it every 15 minutes: an earlier version of this fix put `@security.rate_limit("account-sessions", 10, 900, per_user=True)` on *Sign out everywhere else*, and a review showed a thief's session sending 12 requests (10 x 302, then 429) left the owner's own sign-out answering 429 with the thief still signed in (password change, the other path that ends all other sessions, has its own separate bucket with the same numbers, `account-password`, 10/900 per user, and is exhaustible the same way). Now `revoke_other_sessions` (bananachat/web/account.py) always ends the other sessions, renews the device cookie and revokes the API tokens when asked, and writes `account.sessions_revoke_others` only within `SESSIONS_REVOKE_AUDITS` (10 per account in 15 minutes, `security.allow`); `revoke_session` does the same with `SESSION_REVOKE_AUDITS` (20 in 15 minutes, counted only when a session was really ended). `account.api_tokens_revoke_all` is written only when tokens were revoked, so it is bounded by token creation. `personalities.revoke_share` now clears the link only `WHERE share_token IS NOT NULL` and returns whether it did; `unshare` audits only then, so its entries are bounded by `share` (rate-limited, 20 an hour, and audited itself). The audit log thus gains at most 20 + 10 rows per account per 15 minutes from session sign-outs (80 + 40, 120 in all, an hour) instead of about 600 a minute per address.
- **Test:** `tests/security/test_m5.py::test_mc1_3_sign_out_everywhere_else_always_works_and_stops_adding_audit_entries`, `tests/security/test_m5.py::test_mc1_3_unsharing_a_personality_that_is_not_shared_writes_no_audit_entry`, `tests/security/test_m5.py::test_mc1_3_signing_devices_out_one_by_one_stops_adding_audit_entries`

### MC2-1 Chat search ran an unindexed backtracking LIKE over the whole account's message text (full re-audit pass 7)
- **Severity:** High. **Attacker:** any signed-in user (anyone where sign-up is open); amplified by a few accounts.
- **Repro:** one account seeded with 100 MB of 100 KiB `a` messages (a tenth of the 1 GiB `BC_CHAT_MAX_ACCOUNT_TEXT_MB` default). `GET /chat/search?q=<199 × a + b>` answered 200 `{"results": []}` after 11.7–12.8 s on every call, and two at once each took ~12 s (SQLite releases the GIL, so searches burn one core each). With the test fixtures, 20 MB: the crafted phrase 2.36 s, `zz` 0.003 s. `chats.search` matched `content LIKE '%…%'` over every message of every normal chat (no index can serve a match inside words), and the leading `%` makes LIKE compare most of the phrase again at every position: O(stored bytes × 200), about 2 minutes per request at the default limit. The view was limited to 60 a minute per account but held a short-request thread for the whole scan, so one or a few accounts could keep every request thread and CPU core busy. Expected: bounded work per request.
- **Fix:** `db/chats.py` `search()` stops the query after `SEARCH_SECONDS` (2 s) through a SQLite progress handler checked every 5 VM steps (one message; the LIKE of one message cannot be stopped halfway, about 0.4 s per MiB for a crafted non-ASCII or escaped phrase, so a search ends within about 2.4 s at the default 1 MiB answer limit, about 5.2 s with 8 MiB answers; 503 `search_too_slow`, en/it/de). Each process runs 2 searches at a time, one per account (503 `server_busy` when taken); an account's newer search stops its previous one (409 `search_replaced`, 429 `search_running` if it did not stop within 1 s; chat.js asks once more); and each account may spend 10 s of search time a minute across processes (`rate_limit_hits`, 429 `search_budget`). Results and shape are unchanged; ordinary searches over 200,000 short messages take about 60 ms. No FTS5 trigram index (it would triple the stored text and still scan for two-letter searches). **Accepted residual:** accounts that keep sending searches running to the stop can take the search places; other users' searches then answer 503 (the rest of the site is unaffected): about twelve accounts per process at the default answer limit, about nine with 8 MiB answers (docs/capacity.md).
- **Test:** `tests/security/test_m6.py::test_mc2_1_a_crafted_search_is_stopped_at_the_deadline`, `::test_mc2_1_searches_take_one_of_a_few_places`, `::test_mc2_1_a_newer_search_of_the_account_replaces_the_running_one`, `::test_mc2_1_only_the_newest_waiting_search_takes_the_place`, `::test_mc2_1_an_account_has_a_few_seconds_of_search_a_minute`, `::test_mc2_1_ordinary_searches_stay_within_the_budget`, `::test_mc2_1_the_last_tenth_of_the_budget_can_be_spent`, `::test_mc2_1_a_search_stops_within_a_message_or_two_of_long_answers`

### MC3-1 The cloud-session task page, its events poll and the administrator's task view had no size limit (full re-audit pass 7, rest of JC1-1)
- **Severity:** Medium. **Attacker:** a person allowed cloud sessions (off and administrator-only by default), on their own task; victims are the web process's memory and an administrator opening the task.
- **Repro:** with the suite's fake runner and tool model at default settings, one task of three runs
  (8 bash calls per model call, each printing 9,000 `<`) held 1,065 steps. `GET /agents/<id>` was a
  42.2 MB page built with a 212 MB Python peak (`tojson` writes each `<` as 6 bytes), built in full
  before `hold_large_response` refused a third concurrent load with 429; `/agents/<id>/events?after=0`
  was 2.2 MB and `/admin/agents/tasks/<id>` 14.4 MB (72 MB peak). The views were bounded by step
  count only (1,000 / 300 / 500).
- **Fix:** `agents_db.step_page(max_bytes=...)` reads the steps' text sizes first (`octet_length` of
  content, thinking, tool arguments, tool calls and result) and stops at 1 MiB of text
  (`web.agents.STEP_TEXT_BYTES`, at least one step) with an exact `more`, for the task page, each
  events answer and the administrator's view. The page's existing `events?after=` poll and the admin
  "Next entries" link fetch the rest. Same repro after: page 6.2 MB (32 MB peak), events 1.1 MB,
  admin view 4.2 MB (21 MB peak). The admin view also printed tool arguments and requested tool calls
  with `pretty_json` (indent 2), which repeats the indentation on every line: arguments nested 28 deep
  around a 20x20x20 tree (within the model client's depth and node limits), sent through the real loop,
  made one 1 MiB piece a 25.7 MB page (130 MB peak). `formatting.pretty_json` now streams the indented
  form only while it stays within about twice the one-line JSON and prints one line beyond that, so the
  admin view is bounded by a small factor of its text budget; same nested repro after: 1.1 MB (11 MB
  peak). Ordinary arguments are indented as before. No schema change; docs/agents.md says long
  timelines load in pieces.
- **Test:** `tests/security/test_m7.py::test_mc3_1_agent_task_views_stop_at_a_size`,
  `::test_mc3_1_a_step_larger_than_the_budget_is_still_shown`,
  `::test_mc3_1_admin_task_view_stays_small_for_nested_tool_arguments`.

### NC1-1 A grant for a model id beyond 64 bits gave HTTP 500 (full re-audit pass 8, rest of U2-1)
- **Severity:** Low. **Attacker:** administrator (typo or crafted form); no privilege gained.
- **Repro:** as administrator, `POST /admin/quotas/grants` with `target=user`, `username=bob`, `pool=model:9223372036854775808` (or `model:123456789012345678901234`), `kind=extra`, `amount=1000` answered 500 and wrote `OverflowError: Python int too large to convert to SQLite INTEGER` with a traceback to the error log; `_grant_form` (bananachat/web/admin/limits.py) parsed the value with `int()`, which U2-1 had replaced everywhere else. `model:9223372036854775807`, `model:abc` and `model:١` already answered 400. Expected: 400 "That model does not exist.".
- **Fix:** `_grant_form` reads the id with `ascii_int(..., 18)` like the other model id form values; an id beyond 18 ASCII digits is a model that does not exist (400). No compatibility impact.
- **Test:** `tests/security/test_n6.py::test_nc1_1_grant_for_a_model_id_beyond_64_bits_is_a_form_error`

### NC1-2 The metrics CSV export started from a GET link and was audited before its download place was taken (full re-audit pass 8, rest of LC1-3 / MC1-2)
- **Severity:** Low. **Attacker:** none needed (a busy server, or an administrator with two large responses open); a link on another site could make an administrator's browser start one.
- **Repro:** at `BC_DOWNLOAD_THREADS=8`, with the administrator's two download places held, three `GET /admin/metrics/export.csv?range=30d` with `Sec-Fetch-Site: cross-site` each answered 429 but wrote an `admin.metrics_export` audit row; with all places held by others, 503 and still audited. With a place free, a cross-site top-level GET without any CSRF token answered 200 `text/csv` (attachment) with 30 days of every user's request metrics, audited under the administrator's name (the SameSite=Lax cookie is sent on a top-level navigation; `check_csrf` and the Sec-Fetch-Site check apply only to unsafe methods). No data reached the other site. Expected: a refused export writes nothing, and the export starts only from a form.
- **Fix:** `admin.metrics_export` (bananachat/web/admin/metrics.py) is now `POST /admin/metrics/export.csv` under `@security.download_first`, like the account, user and database exports, so a refused export is neither built nor audited; the metrics page has an *Export CSV* form button with the CSRF token. `GET` on the same URL (`admin.metrics_export_link`) leads back to `/admin/metrics` with the same range. The export is classified in `_reauth.EXEMPT` (request records only, no prompts or answers; it never asked for a confirmation). **Compatibility:** scripts or bookmarks that fetched the CSV by GET must POST with the form's CSRF token (UPGRADING.md).
- **Test:** `tests/security/test_n6.py::test_nc1_2_metrics_export_is_a_post_and_a_refused_one_is_not_audited`

### NC1-3 Recovery codes let one ordinary account add about 800 audit rows an hour (full re-audit pass 8, rest of MC1-3)
- **Severity:** Low. **Attacker:** any signed-in user (anyone where sign-up is open).
- **Repro:** one ordinary account turned two-step verification on and looped: 9 sign-ins with the password and one recovery code each (over a few test browsers, so the per-browser sign-in limit never applied), then *New recovery codes* confirmed with the 10th code. In 12 s it wrote 180 `auth.two_step_recovery_code_used` and 19 `account.two_step_codes_renew` rows (the 20th renewal hit the 20/900 `account-two-step` bucket); repeated every 15 minutes, about 800 rows an hour to an audit log that is never purged. Expected: a bounded number of audit rows per account, as MC1-3 gives sign-outs.
- **Fix:** `two_step_code` (bananachat/web/auth.py, `POST /login/two-step`) never refuses the sign-in for this, but writes `auth.two_step_recovery_code_used` only within `RECOVERY_CODE_AUDITS` (10 per account in 15 minutes, `security.allow`, bucket `auth-recovery-code-audit`); further ones go to the application log (`bananachat.auth`, info) with the account id. Renewals were already bounded by the `account-two-step` limit (20 per 15 minutes, shared with turning it on and off). Together at most 10 + 20 rows per account per 15 minutes from this path (120 an hour). A full set of ten codes is always recorded unless the same account used more than ten in the window; who renewed codes is still audited.
- **Test:** `tests/security/test_n6.py::test_nc1_3_recovery_code_sign_ins_always_work_and_stop_adding_audit_entries`

### NC1-4 The access log kept personality share-link tokens (full re-audit pass 8)
- **Severity:** Low. **Attacker:** whoever can read the service's stdout/journald or the log collector it is shipped to; the victim is a user who shared a personality.
- **Repro:** with the real `gunicorn.conf.py` logger, `/personalities/shared/<token>` and `/personalities/shared/<token>/save` were logged unchanged (also before sign-in, as 302), while `/share/<token>` became `/share/[token]`; `//share/<token>` sent by a client was logged unmasked too. With a token taken from such a line, another ordinary account read the shared personality's instructions (200) and saved a copy. Expected: no share-link token in the access log, as the config's comment promises.
- **Fix:** `_SHARE_TOKEN` in gunicorn.conf.py is now `^(/+share/+|/+personalities/+shared/+)[^/]+`, so both share links are logged as `.../[token]`, also with doubled slashes. Only the log format changes.
- **Test:** `tests/security/test_n6.py::test_nc1_4_access_log_hides_personality_share_tokens`

### NC2-1 Message PDF rendering had no rate limit and cost about 1-2 s of CPU per request for a long answer (full re-audit pass 8)
- **Severity:** Low. **Attacker:** signed-in user, on their own chat (needs a stored assistant answer near `BC_CHAT_MAX_RESPONSE_KB` made of short lines).
- **Repro:** an assistant message of 1 MiB of `"\n"` stored with `chats.add_message`, then
  `GET /chat/<sid>/messages/<id>/pdf` as the owner: 25 calls in a row all answered 200 (no 429),
  about 1.04 s of thread CPU each and a 14.9 MB PDF; 1 MiB of `"\t"` took 1.65 s; 6 parallel calls from
  one account all answered 200 and kept one process busy for about 6 s. `render_pdf` wraps and emits one
  operator pair per line, so the cost follows the line count (an ordinary 1 MiB prose answer: 0.09 s), and
  scales to roughly 8-15 s per call with `BC_CHAT_MAX_RESPONSE_KB=8192`. The view had only
  `login_required`; the IC1-1 download place was taken by the response hook after rendering. Expected:
  rendering bounded per account before the work is done.
- **Fix:** `chat.message_pdf` (`bananachat/web/chat.py`) takes its download place first
  (`security.download_first`, so an account renders at most two PDFs at a time per process and a refusal
  costs nothing), and spends a time budget like chat search: an account may spend `PDF_BUDGET_SECONDS`
  (10) a minute rendering PDFs, in all processes together, counted in rate-limit events of 0.1 s (each PDF
  counts at least one); past that it answers 429 with `Retry-After: 10` without rendering. Ordinary PDFs
  (milliseconds) stay far below it, about 100 a minute. The PDF output is unchanged. Documented in
  `docs/capacity.md` and `CHANGELOG.md`.
- **Residual:** a render that starts within the budget runs to the end, so an account can go over it by
  at most its two concurrent renders per process (then waits until they age out of the minute); many
  accounts with such answers can still keep the process's download places taken (503 `server_busy` for
  downloads only).
- **Test:** `tests/security/test_n7.py::test_nc2_1_pdf_renders_spend_a_time_budget_per_account`,
  `::test_nc2_1_pdf_takes_a_download_place_before_rendering`,
  `::test_nc2_1_ordinary_pdfs_stay_well_within_the_budget`.

### NC3-1 Sub-agent lanes were outside the MC3-1 byte budget: every task page load and events poll carried all lanes of all runs (full re-audit pass 8, rest of MC3-1)
- **Severity:** Medium. **Attacker:** a person allowed cloud sessions, with swarms enabled by an administrator (both off by default), on their own task; victims are the web process's memory and threads.
- **Repro:** with the suite's fake runner and tool model, swarms on and `max_subagents=4`, each run delegated 4 tasks with 8,000 `<` of instructions and each sub-agent finished with 20,000 `<`; lanes pile up across follow-up runs (indexes are MAX+1). At 480 lanes (120 runs) `GET /agents/<id>/events?after=<last step>` returned 0 steps and 13.5 MB (40.7 MB peak), and the task page was 86.8 MB (435 MB peak, `tojson` writing each `<` as 6 bytes), all lanes with instructions and full reports in every answer, including the 1.5 s poll. Expected: bounded like the steps.
- **Fix:** `agents_db.recent_lanes` returns the newest lanes without the instructions (unused by the page; the lane's first step holds them) and with the report cut to `LANE_SUMMARY_CHARS` = 4,000 characters in SQL (marked with an ellipsis; the full report stays in the lane's `finish` step). The task page and the admin task view carry the newest `PAGE_LANES` = 64 lanes, each events answer the newest `EVENTS_LANES` = 16 (a run starts at most 8, so the current and the previous run). A lane card created from older steps gets its default "Sub-agent N" title (bananachat/db/agents.py, bananachat/web/agents.py, bananachat/web/admin/agents.py, bananachat/static/js/agent-task.js); docs/agents.md says so.
- **Test:** `tests/security/test_n8.py::test_nc3_1_sub_agent_lanes_in_the_task_views_are_bounded`

### NC4-1 The compute server's `models restore` still sent dotless-host model names to Ollama (the C3-9 rule was missing from a third copy) (full re-audit pass 8)
- **Severity:** Low. **Attacker:** anyone who can get a model whose registry host has no dot into the source compute server's Ollama (a holder of the compute management token, or an earlier manual pull); the victim is the operator running `bananachat models restore --yes` on a standalone compute server, whose `--yes` is meant to approve only Ollama-library and Hugging Face downloads.
- **Repro:** a fake source Ollama listing `intranet-host/team/model:latest`, `2130706433/ns/m:latest`, `metadata/computeMetadata/v1` and `llama3.2:3b`: `inventory(data, {}, upstream=...)` put all four in `ollama` and none in `manual`, and `compute_recovery(data, "restore", assume_yes=True, ...)` POSTed all four to `/api/pull` with `failed=[]`, while `pulls.validate_ollama_name` refuses the first three. The old check only refused a first segment containing a dot (`localhost:11434/ns/m` passed too). Expected: such names listed for review and not pulled.
- **Fix:** `banana_ops.models.downloadable` is a stdlib copy of the web server's rule (`pulls.validate_ollama_name` / the worker's `valid_name`: whatever precedes the last two `/`-separated parts is the registry host, dotted or not; only `registry.ollama.ai`, `hf.co` and `huggingface.co` are trusted). `compute_recovery` refuses names it rejects ("Review this model's registry manually"), and `inventory` moves catalogue and `/api/tags` names it rejects to `manual` with the custom-registry reason, as the manifest branch already did. Library and Hugging Face names are unaffected; docs/backups.md says so.
- **Test:** `tests/security/test_n8.py::test_nc4_1_compute_restore_uses_the_c3_9_registry_rule`, `tests/security/test_n8.py::test_nc4_1_an_existing_ollamas_custom_registry_models_need_review`

### OC2-1 An image request that waited in the queue is not re-checked for the model lock, access or the model's own limits (full re-audit pass 9)
- **Severity:** Low. **Attacker:** A user or API-key holder whose access to a model an administrator restricts while their image requests wait in the queue.
- **Repro:** With `BC_MAX_CONCURRENT=1` and the fake ComfyUI, a helper holds the only inference place and the artist's `POST /v1/images/generations` waits in `inference_queue`. The administrator then locks the model for the account (`user_model_limits.locked=1`), unpublishes it or closes the *image generation* policy, and the place is released. Observed: the queued request answered 200, ComfyUI received the prompt and the account was charged 5 000 tokens, while a fresh request got 403. Expected: the queued request is refused when it leaves the queue, as chat, API and playground answers are (`docs/architecture.md`: "the check after the queue wait ... read everything afresh").
- **Fix:** `ImageJob._authorize` (`bananachat/services/images.py`), which runs after the queue wait, now also re-reads the model from the catalog and refuses it when it is no longer an offered image model (503 `model_unavailable`), when `AccessContext.can_use(model, "api")` fails (403 `model_not_allowed`: unpublished, access policy, category, model policy) or when `limits.model_refusal` reports a lock (403 `model_locked`) or no capacity left at the provider (429 `insufficient_quota`, `Retry-After: 3600`). Token limits are not checked again there, because the image's own reservation already counts in the model's usage and would refuse an image admitted at the edge of its limit; they are enforced at admission and by the reservation. The reservation is refunded as for any failure, so nothing is charged. `docs/comfyui.md` says so.
- **Test:** `tests/security/test_o5.py::test_oc2_1_queued_image_is_checked_again_when_it_leaves_the_queue`

### OC2-2 Chat Markdown downloads (share link and owner) have no request limit, unlike the share page and the account exports (full re-audit pass 9)
- **Severity:** Low. **Attacker:** Anyone holding a share link (anonymous), or a signed-in user for their own chats.
- **Repro:** A shared chat of 200 messages of 100 KB (20 MB). From one address, `/share/<token>` answered 60 x 200 then 429, but 100 anonymous `GET /share/<token>/download` in a row (responses closed, so the JC1-2 download places never filled) all answered 200 and sent 2 GB in 0.7 s; 100 owner `GET /chat/<id>/download` likewise. Expected: a per-address limit on the share download and a per-account limit on the owner's download, as the share page and the account exports have.
- **Fix:** `bananachat/web/chat.py`: `shared_download` checks `security.allow("share-download", SHARED_DOWNLOADS_PER_HOUR=30, 3600)` per address before looking up the link, and `download` checks `security.allow("chat-download", CHAT_DOWNLOADS_PER_HOUR=60, 3600, key="user:<id>")` per account; beyond them both answer 429 with `Retry-After: 600` (the share download keeps its `noindex`/`no-referrer` headers). The limits are separate from the share page's views. CHANGELOG notes the new 429.
- **Test:** `tests/security/test_o5.py::test_oc2_2_markdown_downloads_are_limited_per_address_and_per_account`

### PC2-1 One account can run a chat answer, an API answer and an image at the same time (full re-audit pass 10)
- **Severity:** Low. **Attacker:** Any signed-in user with an API key (or using the playground or the Images page); the image part needs `BC_IMAGE_BACKEND=comfyui`.
- **Repro:** With fake Ollama and a hanging fake ComfyUI, one account sent a web chat message, a `/v1/chat/completions` request and a `/v1/images/generations` request at once. Observed: three `running` rows in `inference_queue` for that account (owner keys `user:<id>:chat`, `:api`, `:image`, all in lane `local`), filling every local place with `BC_MAX_CONCURRENT=3`, and another user's API answer waited until they finished; the account could also keep three admitted requests per surface (nine per lane). Expected (docs/capacity.md, docs/comfyui.md): one running and three admitted per account.
- **Fix:** New `queue.account_key(user_id)` (`user:<id>`) is the owner key of chat answers (`services/chat.py`), API and playground answers (`services/completions.py`) and images (`services/images.py`), so the queue's existing one-running rule, the three-admitted limit and the unique index on running owner keys apply per account and lane (hosted requests still get their own `:hosted` place). No migration: queue rows only live while a request waits or runs. An account's chat, API and image requests now wait for each other (noted in UPGRADING.md and CHANGELOG.md; docs/capacity.md says the share covers every surface).
- **Test:** `tests/security/test_p5.py::test_pc2_1_chat_api_and_image_answers_share_the_accounts_place`

### QC2-1 A hosted model's fallback to a local model runs on the local server without a local-lane place (full re-audit pass 11)
- **Severity:** Medium. **Attacker:** any signed-in user on a site offering a hosted model next to Ollama models (default `quota_fallback_to_local`); no attacker when the provider is down or rate-limited.
- **Repro:** `BC_MAX_CONCURRENT=1`, a fake OpenAI-compatible provider answering 503 or 400, another account holding the only local place. `POST /chat/<sid>/send` with `model=external:1:gpt-test` answered `start external:1:gpt-test`, then `start llama3.2:3b` and `done`: the fake Ollama got an `/api/chat` while the local lane was full. Three accounts at once ran three local generations together (peak 3 with a cap of 1), each holding only a `user:<id>:hosted` row. Expected: the local fallback waits for and holds a local-lane place.
- **Fix:** `queue.Slot.move()` gives up a place and queues under a new one in another lane (with the account's owner key of that lane). `inference._run_admitted` moves a fallback whose lane differs from the place it holds (or whose earlier move failed) and waits for admission there within what is left of the queue timeout, so the lane cap, the `BC_MIN_FREE_MEMORY_MB` check and the account's place apply; the new row also registers the model before the deletion check. When it gets no place the next fallback is tried; the last one raises the `QueueFull`/`QueueTimeout` the chat, API and agent callers already handle. Documented in docs/architecture.md (Inference) and CHANGELOG.md.
- **Test:** `tests/security/test_q6.py::test_qc2_1_local_fallback_of_hosted_model_needs_a_local_place`, `tests/security/test_q6.py::test_qc2_1_local_fallbacks_stay_within_max_concurrent`

### QC4-1 Snapshot index reads skip the 128 KiB index limit: a backup-repository writer makes every upload/prune load an index of any size (full re-audit pass 11)
- **Severity:** Low. **Attacker:** anyone who can push to the private backup repository.
- **Repro:** a local bare remote with `uploadpack.allowFilter` and one plausible snapshot branch whose `index.json` is a 100 MB `[0,0,...]`. `Git.index` fetched about 1.5 KB (the filter left the blob out), then `Store._authenticated_since` ran `cat-file blob`, which lazily fetched the whole blob: peak RSS went from 212 to 595 MiB. Without partial fetch the whole index was read too. Expected: an index over 128 KiB is refused, as `Git.fetch` refuses it, and never fetched.
- **Fix:** `Git.index` (banana_backup/git.py) now checks that the index blob is stored locally by reading the pack indexes (`git show-index`) before Git looks it up (reading a missing blob, even its size with `cat-file -s` or `ls-tree -l`, fetches it; `GIT_NO_LAZY_FETCH` needs Git 2.44), then refuses one over 128 KiB; `Store._authenticated_index` reads it with `bounded=True`. A refused index counts as "not authenticated", as before. `banana_backup/shared-files.json` recorded again (shared with BananaWiki and BananaVibe).
- **Test:** `tests/security/test_q6.py::test_qc4_1_oversized_snapshot_index_is_refused_unread`, `tests/security/test_q6.py::test_qc4_1_small_snapshot_index_is_still_read`

### QC3-1 Worker job-poll replies were written outside every place, so one worker token reading them slowly took every server thread (full re-audit pass 11, rest of LC2-1 / JC1-2)
- **Severity:** Medium. **Attacker:** a worker PC (holder of a worker token), with `BC_WORKERS_ENABLED=1`.
- **Repro:** live Gunicorn with the shipped `gunicorn.conf.py` (`BC_HTTP_THREADS=8`,
  `BC_DOWNLOAD_THREADS=8`), one registered worker, 20 pending 2 MiB jobs: one token opened 20
  slow-reading `GET /worker/v1/jobs/poll` connections; 16 got a job reply and `/health` and
  `/login` went unanswered for 60+ s until the worker closed them. The poll's long-request place
  ended when the view returned, before the job JSON (up to 16 MiB) was written, and
  `hold_large_response` sent token-API replies with no place at all once the worker's download
  share was used (`BC_WRITE_TIMEOUT` applies per 64 KiB write). Non-streamed `/v1` and
  remote-access answers over 64 KiB were sent the same way (`long_request` released its place on
  return). Expected: slow large replies bounded like other large responses.
- **Fix:** `remote.claim` takes a `reserve` hook: before claiming a job whose reply may exceed
  64 KiB (a bound: three times its stored text), the poll view takes a place with
  `security.take_reply_place` (a download place under `worker:<id>`, or a long-request place when
  the process has none, so other people's slow downloads never stop jobs reaching workers; one at
  a time per worker), and `hold_large_response` keeps it until the reply is closed. Without a place
  the job stays pending and the poll waits as usual (no protocol change; LC2-1's "a claimed job is
  never refused" still holds). `security.long_request` keeps its place until close for a token
  API's large non-streamed answer.
- **Test:** `tests/security/test_q7.py::test_qc3_1_one_worker_reading_slowly_holds_one_place_for_its_large_jobs`,
  `::test_qc3_1_small_jobs_need_no_place`,
  `::test_qc3_1_a_job_reply_uses_a_long_request_place_when_downloads_are_full`,
  `::test_qc3_1_a_large_api_answer_keeps_its_long_request_place_until_it_is_read`.

### RC1-1 A hijacked administrator session can leave lasting user-level access (open-ended user invitation or user account) without the confirmation that opening sign-up needs (full re-audit pass 12)
- **Severity:** Low. **Attacker:** someone holding an administrator's browser session that has not confirmed in the last 10 minutes (stolen cookie, unlocked computer).
- **Repro:** with the session marked as last confirmed 11 minutes ago, `POST /admin/settings` with `signup_mode=open` was refused, but `POST /admin/invites/create {max_uses: 0, role: user, expires: ''}` stored an unlimited, never-expiring invitation and `POST /admin/users/create {role: user}` created an account with a chosen password. After the session was signed out (and every session row deleted), anonymous browsers still signed up with the code and the new account signed in. A first fix that exempted bounded user invitations (at most 10 uses and 30 days) was still bypassable: a one-use, one-day invitation created without confirmation was redeemed at once from an anonymous browser for the same lasting chosen-password account, and 20 bounded invitations in a row (200 sign-ups over 30 days) went through, since nothing limits how many a session makes. Expected: lasting user-level access asks for the confirmation, as opening sign-up does.
- **Fix:** `admin.user_create` and `admin.invite_create` are both in the always-confirmed actions (`bananachat/web/admin/_reauth.py::ALWAYS`; checks in `bananachat/web/admin/users.py` and `bananachat/web/admin/invites.py`): every new account and every invitation, whatever its role, uses or expiry, needs a confirmation from a session that has not confirmed in the last 10 minutes. No per-invitation bound is kept, because any invitation gives the same lasting account and bounded ones could be repeated without limit. Form hints in `templates/admin/users.html` and `invites.html`, docs/two-step-verification.md and CHANGELOG.md updated. `tests/app/test_two_step.py::test_a_stale_session_must_confirm_before_high_impact_actions` expected an unconfirmed user account and an unconfirmed one-use user invitation to be created (the insecure behaviour) and now expects both refused.
- **Test:** `tests/security/test_r5.py::test_rc1_1_open_ended_user_invitations_need_a_confirmation`, `tests/security/test_r5.py::test_rc1_1_a_one_use_one_day_invitation_needs_a_confirmation_too`, `tests/security/test_r5.py::test_rc1_1_creating_a_user_account_needs_a_confirmation`

### RC1-2 An account export already streaming keeps sending the whole account after suspension, sign-out everywhere or a password change (full re-audit pass 12)
- **Severity:** Low. **Attacker:** someone holding a stolen browser session, or the owner of an account an administrator is suspending.
- **Repro:** an account with 25-30 chats of about 100 KB started `POST /account/export/chats` and read the first piece; the administrator then suspended it (or the owner used *Sign out everywhere* against a thief's session). The session rows were gone and `GET /account` redirected to sign-in, yet reading on returned about 3 MB more and every chat. Expected: the download ends once the session that started it is gone or the account is suspended or deleted.
- **Fix:** `bananachat/web/account.py::download_export` records the requester and browser session at the start and, at most every `EXPORT_CHECK_SECONDS` (1 s) while it streams, checks that the session still exists (`users.session_alive`, new in `bananachat/db/users.py`), the requester is not suspended or deleted (`users.suspended_or_gone`) and, for an administrator's export of another account, is still an administrator; otherwise the generator ends and the JSON file is cut short. Suspending the exported account does not stop an administrator's export of it. CHANGELOG.md updated.
- **Test:** `tests/security/test_r5.py::test_rc1_2_suspension_stops_a_running_export`, `tests/security/test_r5.py::test_rc1_2_signing_out_everywhere_stops_a_stolen_sessions_export`, `tests/security/test_r5.py::test_rc1_2_an_export_left_alone_is_complete`

### RC3-1 The tunnel relay started one thread for every local connection it accepted, with no limit (full re-audit pass 12)
- **Severity:** Low. **Attacker:** another local user on the web server host, with `BC_WORKER_REMOTE_ACCESS=1`.
- **Repro:** the real `Broker` in-process, with 50 (reporter: 2000) raw 127.0.0.1 connections that never authenticate and send one byte every 8 s. Observed: 50 `bananachat-tunnel-join` threads with `pairs == 0`, all still alive after 24 s. `MAX_PAIRS` only counts authenticated pairs, and `connection.settimeout(10)` applied to each one-byte read, not to the whole greeting. Expected: a small, bounded number of connections in the greeting, each with one deadline.
- **Fix:** `bananachat/services/tunnel_broker.py`: `Broker.serve` takes a slot from a `BoundedSemaphore(MAX_GREETINGS)` (`2 * MAX_PAIRS` = 128) before it starts the thread, and closes the connection at once when none is free. `_handle` gives the slot back when the greeting ends. `_line` takes an optional `deadline`, and `_hello` gives the whole challenge (both lines and the reply) one `GREETING_SECONDS` (10 s) deadline. The protocol and the client side (`connect`) do not change.
- **Test:** `tests/security/test_r6.py::test_rc3_1_connections_that_never_authenticate_are_bounded`, `tests/security/test_r6.py::test_rc3_1_a_greeting_sent_byte_by_byte_has_one_deadline`

### RC4-1 A few-KB tree bomb in the backup repository made every `backups run` (and verify/restore) buffer gigabytes of `git ls-tree` output (full re-audit pass 12, rest of QC4-1)
- **Severity:** Low. **Attacker:** anyone who can push to the private backup repository.
- **Repro:** a local bare remote whose identity branch holds `index.json` plus `data/`, three nested trees of 200 entries that all point to one subtree (1,746-byte pack, 8,000,000 lines from `ls-tree -r`). The real `Store.ensure_identity` raised "The backup tree exceeds the file limit." only after Python's peak RSS went from 22 to 1,309 MiB (reporter: 300 x 3 reached 4,260 MiB). `Git.index` and the `ls-remote` calls in `heads`/`ref_sha` also captured all output before they checked or sliced it. Expected: the listing is refused after its limit without being buffered.
- **Fix:** `banana_backup/git.py`: `Git.run(..., limit=)` reads Git's stdout as it comes, using `selectors` and stderr in a temporary file, and keeps the 900 s timeout. Once Git has printed more than `limit` bytes, it kills Git and returns those `limit + 1` bytes, so the caller's existing check refuses them. `fetch` uses it with 128 KiB and lists the tree without `-r`. Snapshot and identity trees are flat, so a subtree is a single `040000` entry that the existing mode check refuses ("Unexpected files or links"). `index` uses a 128 KiB limit. `heads` and `ref_sha` use 1 MiB, and `ref_sha` now refuses a larger listing as `heads` does. After the fix, the 200 x 3 bomb is refused in under 0.1 s with no RSS growth. `banana_backup/shared-files.json` was recorded again (shared with BananaWiki and BananaVibe). No compatibility change, since real snapshot trees hold at most 36 flat entries.
- **Test:** `tests/security/test_r6.py::test_rc4_1_a_tree_bomb_is_refused_without_listing_it`, `tests/security/test_r6.py::test_rc4_1_a_huge_flat_tree_is_read_only_up_to_the_limit`, `tests/security/test_r6.py::test_rc4_1_too_many_branches_are_refused_unread`, `tests/security/test_r6.py::test_rc4_1_a_real_snapshot_tree_is_still_read`

### SC1-1 Database (migration) export and metrics CSV export keep streaming after the requesting administrator is suspended, demoted or signed out everywhere (full re-audit pass 13)
- **Severity:** Low. **Attacker:** Holder of an administrator session (stolen cookie plus password for the confirmation), or an administrator being removed by another administrator.
- **Repro:** A second administrator started `POST /admin/migration/export` (unbuffered) and read the first piece; the main administrator then suspended her, which left 0 `auth_sessions` rows. Reading on still returned the whole archive, and its `bananachat.db` member came out complete. `POST /admin/metrics/export.csv` behaved the same way and returned the complete CSV. The RC1-2 fix had covered only `/account/export/*`. Expected: the stream stops at the next check and the file ends cut short.
- **Fix:** The RC1-2 check moved out of `bananachat/web/account.py::download_export` into `bananachat/security.py::requester_check(admin=...)`. It records the requester and the browser session, and returns False once the session has ended, the account is suspended or deleted, or (with `admin`) the account is no longer an administrator. `download_export` now uses it. The generators of `bananachat/web/admin/migration.py::export` (between `tar_gz_stream` pieces) and `bananachat/web/admin/metrics.py::export` (between 500-row batches) call it at most every `security.DOWNLOAD_CHECK_SECONDS` (1 s) and stop when it fails. The formats are unchanged, and a cut archive fails gzip/tar validation on restore. CHANGELOG.md updated.
- **Test:** `tests/security/test_s7.py::test_sc1_1_database_export_stops_when_the_administrator_is_suspended`, `::test_sc1_1_database_export_stops_when_the_session_ends`, `::test_sc1_1_metrics_export_stops_when_the_administrator_is_demoted`, `::test_sc1_1_metrics_export_stops_when_the_administrator_is_suspended` (plus `::test_sc1_1_database_export_still_completes`, `::test_sc1_1_metrics_export_still_completes`)

### SC2-1 Markdown chat download holds a whole 200-message batch in memory per slow reader (share link is anonymous) (full re-audit pass 13)
- **Severity:** Low (reported Medium). **Attacker:** Anyone holding a share link (anonymous), or any signed-in user for their own chats.
- **Repro:** One chat held 150 answers of 1 MiB each and was shared. An anonymous client requested `GET /share/<token>/download` unbuffered and read 3 pieces (about 1 MiB). tracemalloc showed the generator holding 151.8 MiB, because `chats.iter_message_batches` read 200 whole messages with no byte bound. The share page of the same chat peaked at about 4.4 MiB. Expected: the download's working set is bounded like the page's, whatever the size of the chat.
- **Fix:** `bananachat/db/chats.py::iter_message_batches` takes `max_bytes`, as `messages_after` does. It reads the message sizes first (`_text_bytes`), then only as many messages as fit (at least one). `bananachat/web/chat.py::_export` (owner download and share-link download) asks for `PAGE_TEXT_BYTES` (1 MiB, the chat pages' budget), so a paused reader pins about 1 MiB, or one answer of at most `BC_CHAT_MAX_RESPONSE_KB`. The Markdown output is unchanged. CHANGELOG.md updated.
- **Test:** `tests/security/test_s7.py::test_sc2_1_anonymous_share_download_reads_bounded_batches`, `::test_sc2_1_owner_download_reads_bounded_batches`, `::test_sc2_1_message_batches_hold_at_least_one_message`

### SC4-1 Pushed snapshot branches make every backup upload/prune download the same objects again per branch into one directory, with quadratic pack scans (full re-audit pass 13)
- **Severity:** Low. **Attacker:** Anyone who can push to the private backup repository (the QC4-1/RC4-1 threat).
- **Repro:** A local bare remote (`uploadpack.allowFilter`) held N snapshot branches dated now, each with a small `index.json` and `payload/` pointing to one shared subtree of small blobs. `Store._prune` was run through a real `Git`. At N=40 and 20k blobs, the 4.9 MB remote became a 191 MB disposable repository in 40 packs. The `show-index` calls grew as about N²/4, so 400 tiny branches took 40 s and about 9,000 would take about 5.6 h, past `TimeoutStartSec=7200`. Expected: branches pushed by someone else cost a bounded amount of disk and time per run.
- **Fix:** `banana_backup/git.py::Git.index` now fetches each snapshot into a fresh bare repository of its own under the work directory. It checks the index there as before, copies only the index blob (at most 128 KiB, same object ID) into the main disposable repository, and removes the scratch repository in `finally`. Fetched packs therefore never accumulate: disk holds one snapshot at a time, which the existing free-space checks cover, and `_stored` searches a single pack. `Git.run` and `Git._stored` take an optional `repository`. `banana_backup/store.py` reads at most `MAX_UNVERIFIED` (40) unauthenticated snapshots per call. Releases before authentication pruned a series to `--keep` (at most 30), so genuine series stay below that. Past the cap, `_prune` stops: it reads and deletes nothing older and adds a warning. `_authenticated_since` raises a `ValueError` instead of guessing the series start. It does so in an upload, before anything is encrypted or pushed, and in a restore's downgrade check with `--allow-unauthenticated`. Taking the date of whatever it last saw would let a flood of branches move the recorded `authenticated_since` forward for good. This keeps the RC4-1 approach of failing closed. docs/backups.md (which promised that such branches "cannot break later uploads") and CHANGELOG.md were updated. Residual: each examined branch can still cost one fetch of up to the snapshot limit. A small-blob subtree is not removed by `blob:limit` filtering, and GitHub does not reliably support `tree:<depth>` filters. So a run with an attacker present downloads at most about 40 × (`max_mib` + 4 MiB), freed after each fetch.
- **Test:** `tests/security/test_s7.py::test_sc4_1_snapshot_indexes_do_not_pile_up_fetched_objects`, `::test_sc4_1_retention_reads_a_bounded_number_of_unauthenticated_branches`, `::test_sc4_1_series_start_is_refused_behind_too_many_unauthenticated_branches` (plus `::test_sc4_1_genuine_series_behind_a_few_unauthenticated_branches_still_work`)

### TC1-1 Markdown chat downloads kept streaming after the link was revoked, the owner suspended or the session ended (full re-audit pass 14)
- **Severity:** Low. **Attacker:** holder of a leaked share link (anonymous); holder of a stolen browser session.
- **Repro:** a 20-message shared chat: an anonymous reader of `/share/<token>/download` kept receiving
  every message after the owner revoked the link and after the owner was suspended (`/share/<token>`
  already answered 404); the owner's `/chat/<sid>/download` kept going after every session of the account
  was ended. RC1-2 and SC1-1 had added the requester check to the account and administrator exports only.
- **Fix:** `web/chat._export` takes a check and asks it again between batches, at most every
  `security.DOWNLOAD_CHECK_SECONDS`, and ends the file once it fails: the owner download uses
  `security.requester_check(admin=False)`, the share download checks that the token still names this chat
  and that its owner is still in good standing. Output format unchanged.
- **Test:** `tests/security/test_reaudit.py::test_tc1_1_chat_downloads_stop_once_the_link_or_the_session_ends`.

### UC2-1 A prompt that makes Codex try a tool rests the Codex account for 5 minutes (full re-audit pass 15)
- **Severity:** Medium. **Attacker:** Any user allowed a Codex subscription model (API key, playground or chat).
- **Repro:** With one pooled Codex account, a request where the CLI emits a `file_change` item (what the model does for "create hello.txt with apply_patch") failed with "The adapter could not answer." and set the account's `cooldown_until` 5 minutes ahead; another person's ordinary request right after got "Codex has no available quota right now." Expected: the request is refused and no account is rested.
- **Fix:** `bananachat/services/codex_cli.py` raises `subscription_pool.RequestRejected` ("Codex tried to use a tool, so the response was stopped.") for any non-answer item, so the pool refuses the request without resting the account. The process group is still killed in the same `finally`, and a cleanup that cannot be verified still raises `ProcessCleanupError` and quarantines the account. `docs/subscription-router.md` says the account is not rested. The existing connector test now expects `RequestRejected`.
- **Test:** `tests/security/test_u2.py::test_uc2_1_tool_attempt_does_not_rest_the_account`

### VC1-1 An API answer or image already running kept streaming, and was charged, after its API key was revoked (full re-audit pass 16)
- **Severity:** Low. **Attacker:** Someone holding a leaked API key. The victim is the owner who revokes it, from the developer page or with "Also revoke my API keys".
- **Repro:** Fake Ollama sent a 1500-chunk answer at 5 ms per chunk, and the supervisor tick was 0.1 s. `POST /v1/chat/completions` with `stream=true` was sent, the first chunk was read, and then `tokens.revoke()` was called. The stream went on for 7.65 s more and delivered all 1500 chunks and `[DONE]`, while the same key already got 401 on `/v1/models`. Expected: the answer stops as it does for a suspended account.
- **Fix:** The supervisor watches of `CompletionRun._checkpoint` and `ImageJob._account_check` now also return `token revoked` when the request's token is revoked or expired. For this they use the new `tokens.is_live`, one indexed query per lease tick; it also replaces the list scans in both admission checks. The answer then ends with 401 `invalid_api_key`, and what it produced so far is charged, as for `account_suspended`. An image ends with the existing `images.error_token_revoked` message. docs/api.md is updated.
- **Test:** `tests/security/test_reaudit.py::test_vc1_1_a_running_api_answer_stops_once_its_key_is_revoked`, `tests/security/test_reaudit.py::test_vc1_1_a_running_image_stops_once_its_key_is_revoked`

### VC2-1 A Codex request ended by a tool attempt was charged nothing, so per-person model budgets did not limit use of the shared subscription (full re-audit pass 16)
- **Severity:** Medium. **Attacker:** Any user allowed a Codex subscription model, through an API key, the playground or chat.
- **Repro:** With the suite's fake Codex CLI and one pooled account, five `POST /v1/chat/completions` calls sent a 200 KB prompt, and the fake CLI emitted a `file_change` item each time. All five answered 502, and all five `codex exec` runs received the prompt. `api_usage.charge_text` was never called, and no usage was recorded on the account. The same prompt answered normally was charged.
- **Fix:** The tool attempt now raises `RequestRejected(..., spent=True)`. When the Codex process had started, `subscription_pool.chat_stream` then records the estimated prompt on the account, through `usage.interrupted` and the existing settle. It raises `RejectedAfterSending`, which the adapter turns into `UpstreamError(sent=True)`. Inference marks that attempt as interrupted, so `billable()` holds and the person is charged `interrupted_usage()`, the prompt estimate. That attempt also no longer falls through to a fallback model. Rejections before the process starts are still free: too long, invalid Unicode, unsupported effort. So is Claude Code's "too long for this model". The account is still not rested (UC2-1). docs/external-providers.md is updated.
- **Test:** `tests/security/test_v2.py::test_vc2_1_a_codex_tool_attempt_is_charged_its_prompt`

### XC2-1 A Codex request that ran past the CLI timeout before any output was charged nothing (full re-audit pass 17, rest of VC2-1)
- **Severity:** Medium. **Attacker:** Any user allowed a Codex subscription model.
- **Repro:** The suite's fake Codex CLI ran with `timeout_seconds` 10, in a scenario that hangs after `thread.started` and `turn.started`. `POST /v1/chat/completions` was sent with a prompt of about 4000 words. It answered 502 after 10.1 s; one `codex exec` run received the prompt. `api_usage.charge_text` and `subscription_pool.record_usage` were never called. `codex exec --json` sends no deltas, so a long answer or hard reasoning gives nothing until the timeout, which is up to 900 s of free generation per request.
- **Fix:** When the process had started and produced nothing, a timed-out attempt in `subscription_pool.chat_stream` is now handled like VC2-1's tool attempt. It sets `usage.interrupted`, so the estimated prompt is recorded on the account, and raises `RejectedAfterSending`. That becomes `UpstreamError(sent=True)`, so inference charges the person `interrupted_usage()`. The existing single account rest for a timeout is kept. docs/external-providers.md is updated.
- **Accepted residual (external APIs):** A generation's total time limit for an external API provider already cancels through the request's own cancel token, and that is charged as an interruption. A first-byte or between-read timeout is still not charged. From the socket, BananaChat cannot tell a provider that read the prompt from a network or DNS failure. Charging every such timeout would bill people for platform faults, and each one is bounded by `first_token_timeout`.
- **Test:** `tests/security/test_v2.py::test_xc2_1_a_codex_request_that_times_out_before_any_output_is_charged_its_prompt`

### YC2-1 A timed-out Codex request was still free when the same person's earlier failure had rested an account in the last 5 minutes (full re-audit pass 18, bypass of XC2-1)
- **Severity:** Medium. **Attacker:** Any user allowed a Codex subscription model.
- **Repro:** In `chat_stream`'s generic handler, the "not resting another account for this person" branch, which `_may_rest_for` returns False for 5 minutes after a rest, raised a plain `RuntimeError` before XC2-1's charge was reached. So every second back-to-back request that hung until the CLI timeout (180 s by default, shorter than the 300 s cooldown) was recorded nowhere and charged nothing. The XC2-1 test with a fresh `subscription:rested-by:codex:<user>` marker got 0 charges.
- **Fix:** Whether a timed-out attempt is charged (`spent`: the process had started, nothing was produced) is now decided before the rest logic, and both raising branches use it. A spent attempt sets `usage.interrupted` and raises `RejectedAfterSending`, whether or not the account is rested. The rest rules are unchanged.
- **Test:** `tests/security/test_v2.py::test_xc2_1_a_codex_request_that_times_out_before_any_output_is_charged_its_prompt[after_a_rest_yc2_1]`

### ZC1-1 A stolen session could retire the owner's device cookies without the password, then keep the owner from signing in (full re-audit pass 19)
- **Severity:** Medium. **Attacker:** Someone holding a stolen browser session cookie, without the password.
- **Repro:**
  1. alice signed in, which gave her browser a device cookie.
  2. The thief, with one of her sessions, sent `POST /account/sessions/revoke-others`. It needs no password, by design (MC1-3), but it cleared `device_epoch`, which retires every device cookie except the thief's, re-issued to the thief's own browser.
  3. Any client then sent 10 wrong passwords for alice.

  alice's sign-in with the right password and her old device cookie got 429, while the thief's session kept working. Expected: the owner's browser keeps its own failure budget.
- **Fix:** `users.revoke_sessions` takes `keep_devices`, and "Sign out everywhere else" passes it. That action still ends every other session and old-release cookie (the session version moves on), but no longer retires device cookies. A device cookie grants no access, only its browser's own budget of failed sign-ins. Password changes, resets, suspensions and two-step changes still retire them, as before. The C1-1 test is updated to this rule.
- **Test:** `tests/security/test_reaudit.py::test_zc1_1_a_stolen_session_cannot_retire_the_owners_device_cookies`, `tests/security/test_cauth.py::test_c1_1_signing_in_again_keeps_the_device_nonce_until_signing_out_everywhere`

### AC2-1 A Codex answer too large for one record was free and rested the account (full re-audit pass 20, rest of VC2-1 / XC2-1)
- **Severity:** Low. **Attacker:** Any user allowed a Codex subscription model.
- **Repro:** `codex exec --json` sends each answer as one `agent_message` line. When that line is over `MAX_LINE` (1 MiB), `_records` raised a plain `ConnectorError` after the model had finished generating. In `chat_stream` that is neither a tool attempt nor a timeout, so the attempt was not counted as spent. The account was rested for 5 minutes and the request was retried or failed, and the person was charged nothing.
- **Fix:** An oversized record is now raised as `RequestRejected(..., spent=True)` (`codex_cli._oversized`). As for a tool attempt (VC2-1), the estimated prompt is recorded on the account and charged to the person, and no account is rested, because the request was at fault. The total-bytes limit, which also counts the CLI's stderr, and `turn.failed` keep their existing handling: they can be provider faults.
- **Test:** `tests/security/test_v2.py::test_vc2_1_a_codex_tool_attempt_is_charged_its_prompt[oversized_record_ac2_1]` (also checks that no account is rested)

### AC1-1 An Images-page image (JSON mode) was generated and charged, then refused by the download places (full re-audit pass 20, rest of LC2-1)
- **Severity:** Low. **Attacker:** None needed: other people's slow downloads holding every place, or the account's own share held. The victim is the person generating.
- **Repro:** The account's download share was held, and `POST /images/generate` was sent without `Accept: text/event-stream`. ComfyUI generated the image and the reservation was charged. The JSON body (a base64 image over 64 KiB) then went through `hold_large_response`, which closed it and answered 429 or 503. The person paid and received nothing, and every retry was charged again.
- **Fix:** The JSON branch now takes its download place before the image is made (`security.take_download_place`, which is `download_first` for a view that knows only part way whether its answer is large). When no place is free, the request is refused before ComfyUI is called and its reservation is given back. A small answer gives the place back, as before. The SSE branch, which the page's own script uses, and `/v1/images/generations` are unchanged.
- **Test:** `tests/security/test_reaudit.py::test_ac1_1_an_images_page_image_takes_its_download_place_before_it_is_made`

### AC1-2 Share links made from a stolen session outlived sign-out everywhere and a password change, and were not audited (full re-audit pass 20)
- **Severity:** Low. **Attacker:** Someone who briefly held a signed-in session.
- **Repro:** The thief, using the owner's session, sent `POST /chat/<sid>/share` for a chat. It had no audit entry. The owner then used "Sign out everywhere else" with "Also revoke my API keys" (the documented remedy) and changed the password. The anonymous `/share/<token>` link kept working, and because share pages are live, it showed every later message of that chat.
- **Fix:**
  - The first creation of a chat's share link is now recorded as `chat.share_created` (chat id, address). Fetching an existing link again is not recorded.
  - "Sign out everywhere else", the password change and turning two-step on now offer "Also stop my chat share links" next to the API-key box. It is ticked by default and shown only when the account has shared chats. It takes every share link of the account's chats offline (`chats.revoke_all_shares`), records `account.share_links_revoke_all` (count) and confirms in a flash message. Labels are added in en, it and de.

  Personality share links are left as they are: they share a personality's settings, not messages.
- **Test:** `tests/security/test_reaudit.py::test_ac1_2_share_links_made_from_a_stolen_session_can_be_stopped_with_it`

### BC1-1 Creating and revoking a chat share link in a loop wrote unlimited `chat.share_created` audit rows (full re-audit pass 21, from AC1-2)
- **Severity:** Low. **Attacker:** Any signed-in user.
- **Repro:** One user alternated `POST /chat/<sid>/share` and `{"action": "revoke"}` 300 times. Each create made a new link and wrote one `chat.share_created` row: 300 rows in 0.45 s, in an audit log that is never purged.
- **Fix:** The entry now has a per-account budget, as for sign-outs (MC1-3): 10 per 15 minutes (`SHARE_AUDITS`, `SHARE_AUDIT_SECONDS`). Further new links are noted in the application log instead.
- **Test:** `tests/security/test_reaudit.py::test_bc1_1_share_link_audit_entries_have_a_budget_per_account`

### BC1-2 When an administrator handled a compromised account, the thief's chat share links kept working (full re-audit pass 21, rest of AC1-2)
- **Severity:** Low. **Attacker:** Someone who briefly held a session, where the owner is locked out and an administrator steps in.
- **Repro:** The thief shared a chat from alice's session. An administrator then used "Sign out everywhere" with "revoke API keys", or reset the password. The anonymous `/share/<token>` link kept working and showed every later message of that chat.
- **Fix:** The administrator's password reset, sign-out-everywhere and two-step reset forms now offer "Also stop their chat share links". It is ticked by default and shown only when the account has shared chats (`share_links`). It calls `chats.revoke_all_shares` and records `admin.user_share_links_revoke_all` (count). This is the same choice AC1-2 gave the owner: links shared on purpose stop too, which is why it is a box and not automatic.
- **Test:** `tests/security/test_reaudit.py::test_bc1_2_an_administrator_signing_a_compromised_account_out_can_stop_its_share_links`

### DC1-1 A list or object as `hours` in a JSON request to create a worker access key answered 500 (full re-audit pass 22)
- **Severity:** Low. **Attacker:** An administrator who has re-authenticated, sending crafted JSON (no privilege gained).
- **Repro:** `POST /admin/workers/keys` with `{"hours": [1]}`: `int()` raised a `TypeError` that the view did not catch (HTTP 500, traceback logged).
- **Fix:** `workers_key_create` catches `TypeError` with `ValueError` and answers 400 `invalid_key`.
- **Test:** `tests/security/test_reaudit.py::test_dc1_1_a_non_number_of_hours_for_an_access_key_is_refused`

### EC1-1 A JSON body that is not an object made the worker enable/disable route answer 500 (full re-audit pass 23, sibling of DC1-1)
- **Severity:** Low. **Attacker:** An administrator sending crafted JSON (no privilege gained).
- **Repro:** `POST /admin/workers/<id>/state` with `[1]`, `5`, `true` or `"x"`: `(payload or {}).get(...)` raised `AttributeError` (HTTP 500).
- **Fix:** the route reads its fields through the file's `_payload()` helper, which falls back to the form for anything that is not an object.
- **Test:** `tests/security/test_reaudit.py::test_ec1_1_a_json_body_that_is_not_an_object_does_not_fail_the_worker_state_route`

### FC1-1 The new request-body deadline ended every WebSocket tunnel after 2 minutes (full re-audit pass 24, found in the LC1-1 fix)
- **Severity:** Medium (availability of worker terminal, VNC and connect-helper sessions; no attacker needed). **Found in:** `bananachat/read_timeout.py`, the LC1-1 mitigation.
- **Repro:** A request with no body got the 120 s "unsized" timer; the tunnel view runs inside the application call, so the timer fired with nothing read and shut the connection's read side down: the tunnel saw EOF at exactly the limit (reproduced on real Gunicorn with a stub tunnel and the limit set to 5 s).
- **Fix:** Only requests that have a body (`Content-Length` above 0, or chunked) and are not upgrades are timed; others are passed through untouched, and no timer thread is started for them (FC1-2: one `threading.Timer` per request, bodiless ones included).
- **Test:** `tests/security/test_read_timeout.py::test_fc1_1_a_websocket_upgrade_or_a_request_without_a_body_is_never_timed`

### GC1-1 An `Upgrade:` header excused a request with a body from the new body deadline (full re-audit pass 25, found in the FC1-1 fix)
- **Severity:** Low. **Attacker:** anonymous. **Found in:** `bananachat/read_timeout.py`.
- **Repro:** A POST with `Content-Length: 100`, 3 bytes sent and `Upgrade: h2c`, to a handler that reads the body, stayed blocked for 35 s with no deadline (without the header: cut after 3 s). Caddy forwards `Upgrade` headers.
- **Fix:** The Upgrade exemption is gone. A real WebSocket handshake has no body, so it is already skipped for that reason.
- **Test:** `tests/security/test_read_timeout.py::test_fc1_1_a_websocket_upgrade_or_a_request_without_a_body_is_never_timed` (the `h2c` case).

### HC1-1 A body that had arrived whole was refused with 408 when the handler read it late (full re-audit pass 26, found in the LC1-1 fix)
- **Severity:** Low (no Chat handler waits before reading its body today). **Found in:** `bananachat/read_timeout.py`.
- **Repro:** A POST with all 1000 bytes sent, to a handler that slept 12 s before `get_data()`, got 408 at 12 s: the total deadline ran on the wall clock from the start of the call, not on time spent waiting for the body.
- **Fix:** The total time now counts only the time spent inside reads. The timer starts with the first read and, if it fires while the handler is away from the body, waits for what is left.
- **Test:** `tests/security/test_read_timeout.py::test_hc1_1_the_total_time_counts_only_the_time_spent_waiting_for_the_body`

## Static analysis

* **ruff** (0.17.1): clean.
* **bandit** (`bandit -c bandit.yaml -r bananachat compute worker banana_ops banana_backup sqlite_*.py -ll`):
  0 issues. B608 is skipped with the reason in `bandit.yaml` (every SQL string
  builds column lists or placeholders from names checked against an allow-list
  or written in the code; values always go through parameters). The WebSocket
  handshake's SHA-1 (required by RFC 6455) says `usedforsecurity=False`; fixed
  `/tmp` mounts inside sandboxes and release directory modes carry an inline
  `# nosec <id> - <reason>`.
* **pip-audit**: filelock and pypdf raised for published CVEs (C0-1); no known
  vulnerabilities left in `requirements.txt` or `requirements.lock`.
* **Headless browser XSS** (Playwright/Chromium against the running app, 14
  surfaces: user messages, model replies rendered by the client Markdown,
  chat titles, shared chats viewed anonymously, personalities and their JSON
  import, attachment names and downloads, admin user pages, audit log, quota
  requests): nothing executed, no dangerous markup survived, no CSP violation;
  it found the missing sandbox policy on downloads (C4-1).
* **Role x endpoint matrix**: `tests/security/test_authz_matrix.py` probes every
  route and method as anonymous, suspended (session row still present), user and
  admin, with an explicit list of public endpoints.

## Summary

State of branch `security-audit` at the end of the audit (142 commits on top of `main`, nothing pushed).

**Found and fixed.** 103 entries: 6 High, 33 Medium, 64 Low. All are fixed and verified (each with a
regression test that fails before its fix and passes after), except three accepted residuals: MC2-1 (search
places under many accounts), LC1-1 (slow request bodies, below) and the external-API part of XC2-1
(first-byte timeouts are not charged). LC1-1 is now also mitigated in code (below). The serious classes were found and fixed in
the early passes: admin and access-control bypasses, other users' data (exports, share links, attachments),
download sandboxing, worker and remote-access trust, cloud-session sandbox and transfer races, quota races,
and site-wide denial of service (slow readers of files and pages, JC1-1/JC1-2/IC1-1, and the Gunicorn 26
close bug, JC1-3).

**Passes.** A first audit, then repeated fresh full re-audits (findings reproduced independently and
attacked by a refuting reviewer; every fix reviewed independently). The later passes confirmed findings in
every round, almost all Low.

**Done criterion 3 is met.** The last passes were: pass 21 (5 entries), pass 22 (DC1-1; the other three areas
came back empty), pass 23 (EC1-1; the other three came back empty), pass 24 (FC1-1 and FC1-2, both in the new body
deadline written for LC1-1), pass 25 (GC1-1, same module), pass 26 (HC1-1, same module) and pass 27, a line-by-line
review of that module, which came back with no new issues. The findings of passes 22 to 26 were Low except FC1-1
(Medium: the body deadline cut worker tunnels after 2 minutes), and every one is fixed with a test. The Wiki side
(pass 22 DW2-1, then passes 23 and 24 empty) is in its own AUDIT.md. Passes 22 to 26 each concentrated on what the
previous round had changed, after two full passes (22 and 23) over every area. Further narrow follow-ups of recent
fixes remain possible, as in every earlier round.

**Tests changed.** 5 low-value test files removed and 33 added: the role x endpoint matrix
(`tests/security/test_authz_matrix.py`), the flows (`tests/security/test_flows.py`), rate limits and one
regression test per finding. Full suite (with the `age` binary on PATH): 3197 passed, 100 skipped. ruff and
bandit clean; pip-audit clean on `requirements.lock`, `requirements-build.lock` (setuptools 84.2.1, GC0-1)
and `requirements-browser.txt`.

**Residual risks.**
* **LC1-1 (mitigated):** a request body that stalls for 20 seconds or arrives slower than 32 KB/s is now cut
  off with 408 (`BC_BODY_STALL_TIMEOUT`, `BC_BODY_MIN_KBPS`). Many connections can still each hold a thread for
  the few seconds before the cut; the proxy's `read_header 10s` covers slow request headers.
* Many accounts or addresses reading slowly at once can fill the download places or the long-request places;
  each client is bounded to a share, and pages and health checks keep their own threads.
* The fixes were written and reviewed by AI agents: review by hand the download places
  (`security.hold_large_response`), the Gunicorn configuration patch, the export streaming and the agents
  sandbox changes before deploying.

**Needs a human decision (deploy-time).**
* LC1-1: the body limits are on by default; raise `BC_BODY_MIN_KBPS` or lower `BC_BODY_STALL_TIMEOUT` for a
  public site, or lower them if users upload large files over slow links. Never expose Gunicorn without the proxy.
* Keep-alive is off and `close_graceful` is replaced in `gunicorn.conf.py` (JC1-3): keep it that way until
  Gunicorn fixes the accept-loop stall.
* Defaults chosen in the audit: `BC_DOWNLOAD_THREADS` 8 (24 threads per process by default),
  `BC_CHAT_MAX_ACCOUNT_TEXT_MB` 1024, `BC_AGENTS_MAX_STORED_UPLOAD_MB` 100, `BC_SANDBOX_MAX_EXECS_PER_SANDBOX`,
  search 2 s and 10 s a minute per account; raise them where needed.
* Compatibility changes in UPGRADING.md: exports start only by POST, chat pages show at most 1 MB of text at
  a time, share links are rate-limited.
