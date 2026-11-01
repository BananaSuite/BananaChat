"""Official Claude Code subscription transport; no website cookies or API fallback.

An operator-owned manifest binds private authentication profiles to account
labels. Web administrators select those profiles, never supply executable
paths, credentials, hooks or CLI arguments. See docs/claude-code.md.
"""
from __future__ import annotations

import json
import hashlib
import copy
import logging
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager

from bananachat.db import settings
from bananachat.db import subscription_pool as accounts
from bananachat.services import claude_sandbox
from bananachat.services import subscription_pool
from bananachat.services.subscription_cli import MAX_LINE, ConnectorError, ConnectorTimeout, json_file, private
from bananachat.services.subscription_profiles import ManagedProfiles
from bananachat.services.upstream import CancelToken

log = logging.getLogger("bananachat.claude_code")

PROVIDER = "claude"
BINDINGS_KEY = "claude_code_profiles"
MAX_BYTES = 8 * 1024 * 1024
METADATA_WORKERS = 4
METADATA_SECONDS = 27  # Leaves eight seconds for process-group cleanup within a 35-second budget.
METADATA_CACHE_SECONDS = 5
AUTHENTICATION_FAILED = "native_authentication_failed"


class ProcessCleanupError(ConnectorError):
    """A descendant still runs; the pool must quarantine this account."""

    requires_quarantine = True


class NativeAuthenticationError(ConnectorError):
    """The CLI positively reports logged-out or non-subscription authentication."""


def _number(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= accounts.TOKENS_MAX:
        raise ConnectorError("Claude Code returned invalid token usage.")
    return value


def _stop(process):
    # Even after the parent exits, descendants must not survive its request.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        raise ProcessCleanupError("Claude Code did not stop. Review the account before routing more requests.") from None
    if Path("/proc").is_dir():
        # wait() reaps only the leader. A killed grandchild may still be running
        # until the kernel processes SIGKILL; do not release its account yet.
        deadline = time.monotonic() + 2
        while _group_running(process.pid, deadline=deadline):
            if time.monotonic() >= deadline:
                raise ProcessCleanupError("Claude Code descendants did not stop. Review the account before routing more requests.")
            time.sleep(0.01)


def _group_running(group, *, deadline=None):
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            if deadline is not None and time.monotonic() >= deadline:
                raise ProcessCleanupError("Claude process cleanup could not be verified. Review the account before routing more requests.")
            try:
                # A process name may contain spaces or ')'; split after its
                # final delimiter. Bounded raw reads keep large /proc scans fast.
                with open(entry.path + "/stat", "rb") as source:
                    fields = source.read(4096).rsplit(b")", 1)[1].split()
                if int(fields[2]) == group and fields[0] not in (b"Z", b"X"):
                    return True
            except (OSError, ValueError, IndexError):
                continue  # The process can disappear while its stat file is read.
    return False


class Adapter(ManagedProfiles):
    provider = PROVIDER
    bindings_key = BINDINGS_KEY
    reports_cli_start = True  # chat() calls subscription_pool.cli_started() (see there)

    def __init__(self, config):
        if os.name != "posix":
            raise ConnectorError("The Claude Code connector requires a Linux/POSIX server.")
        manifest = json_file(config.claude_code_config)
        if not isinstance(manifest, dict):
            raise ConnectorError("The connector configuration must be an object.")
        binary = Path(manifest.get("binary", ""))
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise ConnectorError("Configure an absolute path to the official Claude Code executable.")
        self.binary = str(binary)
        self.timeout = manifest.get("timeout_seconds", 180)
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, int) or not 10 <= self.timeout <= 900:
            raise ConnectorError("The request timeout must be between 10 and 900 seconds.")
        profiles = manifest.get("profiles", {})
        signed_in = manifest.get("managed_root") not in (None, "")
        if not isinstance(profiles, dict) or not (0 if signed_in else 1) <= len(profiles) <= 100:
            raise ConnectorError("Configure between one and 100 private account profiles, or a managed_root for "
                                 "profiles signed in from the administration pages.")
        self.profiles = {}
        self._models = {}
        self._model_epoch = 0
        self._identities = {}
        self._model_lock = threading.RLock()
        self._profile_locks = {}
        self._metadata_lock = threading.Lock()
        self._metadata_cache_lock = threading.Lock()
        self._metadata_cache = {}
        for name, profile in profiles.items():
            if not isinstance(name, str) or not name or len(name) > 80 or not isinstance(profile, dict):
                raise ConnectorError("Invalid account profile.")
            home = private(profile.get("home", ""), directory=True)
            config_dir = private(profile.get("config_dir", ""), directory=True)
            models = profile.get("models", [])
            discovery = profile.get("discovery", "manual" if models else "automatic")
            usage_source = profile.get("usage_source", "automatic")
            if discovery not in ("automatic", "manual") or usage_source not in ("automatic", "local"):
                raise ConnectorError("Use automatic/manual model discovery and automatic/local usage reporting.")
            if not isinstance(models, list) or len(models) > 200 or (discovery == "manual" and not models):
                raise ConnectorError("Manual discovery needs a verified model list (maximum 200 models).")
            # Reuse the provider's capability/name validation, without inventing models.
            checked = [{**subscription_pool._model_descriptor(PROVIDER, model),
                        "effort_restricted": "reasoning" in model} for model in models]
            if any(m["name"].startswith("REPLACE_") for m in checked):
                raise ConnectorError("Replace the example model ID with a verified subscription model.")
            if any(m["capabilities"] != ["completion"] or any(level not in ("low", "medium", "high", "extra", "max") for level in m["reasoning"]) for m in checked):
                raise ConnectorError("Invalid model configuration.")
            self.profiles[name] = {"home": str(home), "config_dir": str(config_dir), "models": checked,
                                   "telemetry": profile.get("telemetry_file"), "discovery": discovery, "usage_source": usage_source}
            self._profile_locks[name] = threading.RLock()
        if len({p["config_dir"] for p in self.profiles.values()}) != len(self.profiles) \
                or len({p["home"] for p in self.profiles.values()}) != len(self.profiles):
            raise ConnectorError("Each subscription needs a separate authentication directory.")
        self.sandboxed = getattr(config, "environment", "production") not in ("development", "testing")
        forbidden = [Path(__file__).resolve().parents[2], Path(config.claude_code_config).resolve()]
        # The Codex manifest names that provider's logins; a Claude profile must not expose it.
        forbidden += [getattr(config, name) for name in ("instance_dir", "database_path", "log_file",
                                                         "maintenance_file", "codex_cli_config")
                      if getattr(config, name, None)]
        if self.sandboxed:
            try:
                if os.geteuid() == 0:
                    raise claude_sandbox.SandboxError("Production native transport needs an unprivileged identity.")
                self.binary = claude_sandbox.trusted_executable(self.binary)
                claude_sandbox.trusted_executable("/usr/bin/bwrap")
                claude_sandbox.private_roots(forbidden)
                if self.profiles:
                    claude_sandbox.profile_paths(self.profiles, uid=os.getuid(), forbidden=forbidden)
            except FileNotFoundError:
                raise ConnectorError("The production Claude sandbox is not configured: Bubblewrap (/usr/bin/bwrap) "
                                     "or a configured path is missing. Install Bubblewrap (apt install bubblewrap) "
                                     "and run scripts/setup_subscriptions.py.") from None
            except (OSError, claude_sandbox.SandboxError) as error:
                raise ConnectorError("The production Claude sandbox is not configured. Install Bubblewrap and use "
                                     "root-owned native executables with separate private profiles "
                                     f"({error}).") from None
        self._init_managed(manifest, sandboxed=self.sandboxed, forbidden=forbidden)

    def _managed_profile(self, path):
        return {"home": str(path), "config_dir": str(Path(path) / ".claude"), "models": [], "telemetry": None,
                "discovery": "automatic", "usage_source": "automatic", "managed": True}

    @staticmethod
    def _prepare_managed(path):
        config_dir = Path(path) / ".claude"
        try:
            os.mkdir(config_dir, 0o700)
        except FileExistsError:
            pass
        claude_sandbox.private_path(config_dir, uid=os.getuid(), directory=True)

    def _command(self, name, arguments, environment):
        profile = self.profiles[name]
        if self.sandboxed:
            return claude_sandbox.command(self.binary, profile, arguments, cwd=profile["home"],
                                          environment=environment)
        return [self.binary, *arguments]

    def login_command(self, name):
        """``(argv, environment, cwd)`` of the official interactive sign-in for a profile (run on a terminal)."""
        if name not in self.profiles:
            raise ConnectorError("Unknown Claude Code profile.")
        profile = self.profiles[name]
        environment = self._env(profile, usage_metadata=True)
        environment.update(TERM="xterm-256color", COLUMNS="4000", LINES="40")
        return self._command(name, ["auth", "login", "--claudeai"], environment), environment, profile["home"]

    def logout_command(self, name):
        if name not in self.profiles:
            raise ConnectorError("Unknown Claude Code profile.")
        profile = self.profiles[name]
        environment = self._env(profile, usage_metadata=True)
        return self._command(name, ["auth", "logout"], environment), environment, profile["home"]

    def _env(self, profile, *, usage_metadata=False):
        # Allowlist prevents inherited API keys, token overrides, hooks or provider switches.
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE",
                                              "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                                              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                                              "http_proxy", "https_proxy", "all_proxy", "no_proxy") if key in os.environ}
        env.update(HOME=profile["home"], CLAUDE_CONFIG_DIR=profile["config_dir"], DISABLE_AUTOUPDATER="1",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", CLAUDE_CODE_SKIP_PROMPT_HISTORY="1")
        if usage_metadata:
            # This switch suppresses the official usage endpoint too; metadata calls
            # contain no user prompt and retain telemetry opt-outs independently.
            env.pop("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", None)
            env.update(DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
        return env

    def _records(self, profile, arguments, data=b"", *, cancel=None, timeout=None, cwd=None, single_json=False,
                 usage_metadata=False, exit_codes=(0,), started=None):
        """Bounded nonblocking input/output; closes the whole process group on every exit."""
        if cancel is not None:
            cancel.check()
        environment = self._env(profile, usage_metadata=usage_metadata)
        command = [self.binary, *arguments]
        if self.sandboxed:
            try:
                command = claude_sandbox.command(self.binary, profile, arguments,
                                                cwd=cwd or profile["home"], environment=environment)
            except (OSError, claude_sandbox.SandboxError):
                raise ConnectorError("The Claude sandbox could not be configured safely.") from None
        if started is not None:
            started()  # from here on a failure may be the account's
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=cwd or profile["home"], env=environment,
                                   start_new_session=True, close_fds=True)
        deadline = time.monotonic() + (timeout or self.timeout)
        output = bytearray()
        total = 0
        offset = 0
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            if data:
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
            try:
                while selector.get_map():
                    if cancel is not None:
                        cancel.check()
                    if time.monotonic() >= deadline:
                        raise ConnectorTimeout("Claude Code exceeded the request timeout.")
                    for key, _ in selector.select(0.1):
                        pipe = key.fileobj
                        if key.data == "stdin":
                            try:
                                offset += os.write(pipe.fileno(), data[offset:offset + 65536])
                            except BrokenPipeError:
                                offset = len(data)
                            if offset == len(data):
                                selector.unregister(pipe)
                                pipe.close()
                            continue
                        chunk = os.read(pipe.fileno(), 65536)
                        if not chunk:
                            selector.unregister(pipe)
                            continue
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise ConnectorError("Claude Code exceeded the response size limit.")
                        if key.data == "stderr":
                            continue  # Never forward or log provider stderr/credentials.
                        output.extend(chunk)
                        while not single_json and b"\n" in output:
                            line, _, rest = output.partition(b"\n")
                            output[:] = rest
                            if len(line) > MAX_LINE:
                                raise ConnectorError("Claude Code returned an oversized record.")
                            if line.strip():
                                yield self._decode(line)
                        if len(output) > MAX_LINE:
                            raise ConnectorError("Claude Code returned an oversized record.")
                if output.strip():
                    yield self._decode(output)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConnectorTimeout("Claude Code exceeded the request timeout.")
                try:
                    code = process.wait(timeout=min(remaining, 3))
                except subprocess.TimeoutExpired:
                    raise ConnectorError("Claude Code did not stop after completing its output.") from None
                if code not in exit_codes:
                    raise ConnectorError("Claude Code failed. Check the account authentication and model configuration.")
            finally:
                try:
                    _stop(process)
                finally:
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        pipe.close()

    @staticmethod
    def _decode(line):
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            raise ConnectorError("Claude Code returned invalid structured output.") from None
        if not isinstance(value, dict):
            raise ConnectorError("Claude Code returned an invalid record.")
        return value

    def bindings(self):
        value = settings.state_get(BINDINGS_KEY, {})
        return self._managed_bindings(value if isinstance(value, dict) else {})

    def profile_for(self, account):
        if account.get("provider", PROVIDER) != PROVIDER:
            raise ConnectorError("This account belongs to another subscription provider.")
        name = self.bindings().get(str(account["id"]), account["label"])
        if name not in self.profiles:
            raise ConnectorError("Select a configured Claude Code profile for this account.")
        return name, self.profiles[name]

    def check_profile(self, name, *, cancel=None):
        """Positively confirm a native subscription login; a changed account invalidates every observation."""
        if name not in self.profiles:
            raise ConnectorError("Unknown Claude Code profile.")
        # A logged-out CLI still prints its status, then exits with status 1.
        records = list(self._records(self.profiles[name], ["auth", "status", "--json"], timeout=15, cancel=cancel,
                                     single_json=True, exit_codes=(0, 1)))
        if len(records) != 1 or not isinstance(records[0].get("loggedIn"), bool):
            raise ConnectorError("Claude returned an invalid authentication status.")
        status = records[0]
        # A null plan means no subscription. An API key is listed even beside a
        # claude.ai login, and chat refuses any session that could fall back to it.
        if status["loggedIn"] is False:
            raise NativeAuthenticationError("This profile is not signed in to a Claude subscription. Sign it in "
                                            "from Administration → Models → Claude.")
        if (isinstance(status.get("authMethod"), str) and status["authMethod"] != "claude.ai") \
                or (isinstance(status.get("apiProvider"), str) and status["apiProvider"] != "firstParty") \
                or ("subscriptionType" in status and status["subscriptionType"] is None) or "apiKeySource" in status:
            raise NativeAuthenticationError("This profile needs native Claude subscription login; API-key authentication is not accepted.")
        if status.get("authMethod") != "claude.ai" or status.get("apiProvider") != "firstParty" \
                or not isinstance(status.get("subscriptionType"), str):
            raise ConnectorError("Claude returned an invalid authentication status.")
        email = status.get("email")
        identity = hashlib.sha256(email.strip().casefold().encode()).hexdigest() \
            if isinstance(email, str) and 0 < len(email) <= 320 else None
        previous = self._identities.get(name)
        if previous and previous[1] != identity:
            from bananachat.services import claude_usage
            with self._model_lock:
                self._models.pop(name, None)
                self._model_epoch += 1
            with self._metadata_cache_lock:
                self._metadata_cache.clear()
            self.profiles[name]["identity_changed_at"] = time.time()
            self.profiles[name]["identity_change_pending"] = True
            claude_usage.invalidate(self, self.profiles[name])
            subscription_pool.invalidate(PROVIDER)
        self._identities[name] = (time.monotonic(), identity)
        plan = status.get("subscriptionType")
        return {"authenticated": True,
                "subscription_type": plan if plan in ("pro", "max", "team", "enterprise") else None}

    def _identity(self, name, *, cancel=None):
        with self._profile_guard(name, cancel):
            cached = self._identities.get(name)
            if not cached or time.monotonic() - cached[0] >= 60:
                self.check_profile(name, cancel=cancel)
            return self._identities.get(name, (0, None))[1]

    @contextmanager
    def _profile_guard(self, name, cancel=None):
        """Serialize a profile's native config writes without blocking other profiles."""
        lock = self._profile_locks[name]
        while not lock.acquire(timeout=0.1):
            if cancel is not None:
                cancel.check()
        try:
            if cancel is not None:
                cancel.check()
            yield
        finally:
            lock.release()

    def _require_unique_identity(self, name, *, cancel=None):
        identity = self._identities.get(name, (0, None))[1]
        if identity is None:
            return
        targets, results = self._metadata("identity", cancel=cancel)
        current = results.get(name)
        if not isinstance(current, tuple) or current[0] is None:
            raise subscription_pool.AdmissionChanged()
        identity = current[0]
        for _, other_name in targets:
            if other_name is None:
                continue  # A removed profile cannot serve requests.
            if other_name == name:
                continue
            if other_name not in results:
                raise subscription_pool.AdmissionChanged()  # Unknown identity after a deadline cannot admit safely.
            result = results[other_name]
            if result == AUTHENTICATION_FAILED:
                continue  # Failed authentication cannot serve; it must not disable a healthy profile.
            if result is None:
                raise subscription_pool.AdmissionChanged()  # A timeout/error is not proof of being logged out.
            if result[0] == identity:
                raise ConnectorError("Two profiles use the same subscription. Authenticate distinct accounts.")

    def _profile_models(self, name, profile, *, cancel=None):
        if profile["discovery"] == "manual":
            return profile["models"]
        from bananachat.services import claude_discovery

        with self._profile_guard(name, cancel):
            with self._model_lock:
                cached = self._models.get(name)
                epoch = self._model_epoch
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            try:
                self.check_profile(name, cancel=cancel)
                detected = claude_discovery.discover(self, profile, cancel=cancel)
                if profile["models"]:
                    # An explicit allowlist is a restriction, never proof that a model still exists.
                    selected = []
                    for wanted in profile["models"]:
                        actual = next((m for m in detected if wanted["name"] in
                                       (m["name"], *m.get("aliases", []))), None)
                        if actual:
                            selected.append({**actual, "name": wanted["name"], "display": wanted["display"],
                                             "reasoning": [v for v in wanted["reasoning"] if v in actual["reasoning"]]
                                             if wanted["effort_restricted"] else actual["reasoning"]})
                            if actual["reasoning"] and not selected[-1]["reasoning"]:
                                raise ConnectorError("Automatic model restrictions must retain a supported effort level.")
                    detected = selected
                with self._model_lock:
                    if epoch == self._model_epoch:
                        self._models[name] = (time.monotonic(), detected)
                return detected
            except Exception:
                with self._model_lock:
                    self._models.pop(name, None)
                raise

    def invalidate_models(self):
        with self._model_lock:
            self._models.clear()
            self._model_epoch += 1
        with self._metadata_cache_lock:
            self._metadata_cache.pop("models", None)

    def _metadata_profile(self, kind, name, profile, force, cancel):
        """Worker uses only the preloaded profile; Flask/database work stays outside."""
        from bananachat.services import claude_usage

        with self._profile_guard(name, cancel):
            if kind == "identity":
                self.check_profile(name, cancel=cancel)
                identity, value = self._identities.get(name, (0, None))[1], None
            elif kind == "models":
                identity = self._identity(name, cancel=cancel)
                value = self._profile_models(name, profile, cancel=cancel)
            else:
                identity = self._identity(name, cancel=cancel)
                if profile["telemetry"]:
                    value = self._file_usage(profile)
                elif profile["usage_source"] == "automatic":
                    value = claude_usage.read(self, profile, force=force, cancel=cancel)
                else:
                    value = {"source": "local", "available": True, "window_left": None,
                             "weekly_left": None, "model_limits": {}}
            cancel.check()
            return identity, value

    def _metadata(self, kind, *, force=False, cancel=None):
        """One bounded pool per adapter, with four workers and coalesced refreshes.

        Authentication, discovery and usage share the same limit. Queued work
        is cancelled at the overall deadline and running CLI groups are reaped
        before the refresh releases its lock. A failed profile cannot prevent
        healthy profiles from reporting. The five-second snapshot also covers
        failures, preventing repeated callers from spawning an outage storm.
        """
        started = time.monotonic()
        deadline = started + METADATA_SECONDS
        if cancel is not None:
            cancel.check()
        bindings = self.bindings()
        targets = []
        # A Codex account labelled like a profile must never start the Claude CLI.
        for account in accounts.list_accounts(provider=PROVIDER, include_disabled=False):
            name = bindings.get(str(account["id"]), account["label"])
            targets.append((str(account["id"]), name if isinstance(name, str) and name in self.profiles else None))
        telemetry_stamps = {}
        if kind == "usage":
            for _, name in targets:
                path = self.profiles[name]["telemetry"] if name else None
                if path:
                    try:
                        info = os.stat(path, follow_symlinks=False)
                        telemetry_stamps[name] = (info.st_mtime_ns, info.st_size)
                    except OSError:
                        telemetry_stamps[name] = None
        signature = tuple((aid, name, self.profiles[name]["discovery"], self.profiles[name]["usage_source"],
                           self.profiles[name]["telemetry"], telemetry_stamps.get(name)) if name else (aid, None)
                          for aid, name in targets)
        if kind == "models":
            with self._model_lock:
                signature = (self._model_epoch, signature)
        while not self._metadata_lock.acquire(timeout=min(0.1, max(0, deadline - time.monotonic()))):
            if cancel is not None:
                cancel.check()
            if time.monotonic() >= deadline:
                return targets, {}
        try:
            with self._metadata_cache_lock:
                cached = self._metadata_cache.get(kind)
                if cached and cached[1] == signature and time.monotonic() - cached[0] < METADATA_CACHE_SECONDS \
                        and (not force or cached[0] >= started):
                    if cancel is not None:
                        cancel.check()
                    return targets, copy.deepcopy(cached[2])
            control = CancelToken()
            relay = lambda: control.cancel(cancel.reason or "cancelled")
            if cancel is not None:
                cancel.on_cancel(relay)
            results = {}
            executor = ThreadPoolExecutor(max_workers=METADATA_WORKERS, thread_name_prefix="bc-claude-metadata")
            futures = {}
            try:
                for name in dict.fromkeys(name for _, name in targets if name):
                    futures[executor.submit(self._metadata_profile, kind, name, self.profiles[name], force, control)] = name
                while futures:
                    if cancel is not None:
                        cancel.check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    done, _ = wait(futures, timeout=min(0.1, remaining), return_when=FIRST_COMPLETED)
                    for future in done:
                        name = futures.pop(future)
                        try:
                            results[name] = future.result()
                        except NativeAuthenticationError:
                            results[name] = AUTHENTICATION_FAILED
                            log.warning("Claude profile %s is not signed in to a subscription", name)
                        except Exception as error:
                            results[name] = None
                            log.warning("Claude %s metadata for profile %s failed (%s)", kind, name,
                                        type(error).__name__)
            finally:
                control.cancel("Claude metadata refresh completed")
                for future in futures:
                    future.cancel()
                executor.shutdown(wait=True, cancel_futures=True)
                if cancel is not None:
                    cancel.remove(relay)
            if cancel is not None:
                cancel.check()
            with self._metadata_cache_lock:
                self._metadata_cache[kind] = (time.monotonic(), signature, copy.deepcopy(results))
            return targets, results
        finally:
            self._metadata_lock.release()

    def discover(self, *, cancel=None):
        found, failed, identities = {}, False, {}
        targets, results = self._metadata("models", cancel=cancel)
        duplicates = {name for _, name in targets if name and sum(other == name for _, other in targets) > 1}
        for aid, name in targets:
            result = results.get(name)
            if result is None or result == AUTHENTICATION_FAILED or name in duplicates:
                failed = True
                continue
            identity, models = result
            account_id = int(aid)
            if identity:
                identities.setdefault(identity, []).append(account_id)
            for model in models:
                descriptor = found.setdefault(model["name"], {**model, "account_ids": []})
                descriptor["reasoning"] = [v for v in descriptor["reasoning"] if v in model["reasoning"]]
                descriptor["account_ids"].append(account_id)
        duplicates = {aid for ids in identities.values() if len(ids) > 1 for aid in ids}
        if duplicates:
            failed = True
            for descriptor in found.values():
                descriptor["account_ids"] = [aid for aid in descriptor["account_ids"] if aid not in duplicates]
            found = {name: value for name, value in found.items() if value["account_ids"]}
        if failed and not found:
            raise ConnectorError("Claude model discovery is unavailable. Check the CLI version and subscription login.")
        return list(found.values())

    def _file_usage(self, profile):
        report = json_file(profile["telemetry"])
        if not isinstance(report, dict):
            raise ConnectorError("Invalid Claude usage observation.")
        stamp = report.get("observed_at")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) \
                or not 0 <= time.time() - stamp <= 900:
            raise ConnectorError("The Claude usage observation is stale.")
        result = {"source": "file", "available": True, "observed_at": stamp, "model_limits": {}}
        for key in ("window_left", "weekly_left"):
            value = report.get(key)
            reset_key = key.replace("_left", "_resets_at")
            reset = report.get(reset_key)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                        or not 0 <= value <= 1:
                    raise ConnectorError("Invalid Claude usage percentage.")
                if reset is not None and (isinstance(reset, bool) or not isinstance(reset, (int, float))
                                          or not math.isfinite(reset) or reset <= time.time()):
                    raise ConnectorError("The Claude usage observation needs refreshing after its reset.")
            result[key], result[reset_key] = value, reset
        return result

    def usage_reports(self, force=False, *, cancel=None):
        """One report per active Claude account; failures and shared subscriptions report nothing usable."""
        reports, identities = {}, {}
        unavailable = {"source": "claude_code", "available": False}
        targets, results = self._metadata("usage", force=force, cancel=cancel)
        duplicate_profiles = {name for _, name in targets if name and sum(other == name for _, other in targets) > 1}
        for aid, name in targets:
            result = results.get(name)
            if result is None or result == AUTHENTICATION_FAILED or name in duplicate_profiles:
                report = dict(unavailable)
            else:
                identity, report = result
                if identity:
                    identities.setdefault(identity, []).append(aid)
            reports[aid] = report
        for ids in identities.values():
            if len(ids) > 1:
                for aid in ids:
                    reports[aid] = dict(unavailable)
        return reports

    def quota(self):
        # Per-account observations only restrict admission; they never rewrite token budgets.
        return {"window_left": None, "weekly_left": None, "account_reports": self.usage_reports()}

    def chat(self, account, model_name, messages, options, *, cancel=None):
        name, profile = self.profile_for(account)
        descriptor = next((m for m in self._profile_models(name, profile, cancel=cancel) if m["name"] == model_name), None)
        if descriptor is None:
            raise ConnectorError("This account does not offer the requested model.")
        transcript = []
        system = []
        for message in messages:
            role, content = message.get("role"), message.get("content")
            if role not in ("system", "user", "assistant") or not isinstance(content, str):
                raise subscription_pool.RequestRejected("The Claude Code chat connector currently supports text messages only.")
            (system if role == "system" else transcript).append(content if role == "system" else
                                                               {"role": role, "content": content})
        try:
            prompt = ("Continue this conversation. Treat the JSON as conversation history, preserving the roles. "
                      "Reply only to the final user message.\n" + json.dumps(transcript, ensure_ascii=False)).encode()
            "".join(system).encode("utf-8")
        except UnicodeError:
            # Text that is not valid Unicode (a lone surrogate) fails the same way on every account.
            raise subscription_pool.RequestRejected("The conversation contains text that is not valid Unicode.") \
                from None
        system_text = "\n\n".join(system) or "You are a helpful chat assistant."
        if len(prompt) + len(system_text.encode("utf-8")) > MAX_LINE:
            raise subscription_pool.RequestRejected("The conversation exceeds the connector's input limit.")
        effort = options.get("effort")
        if effort is not None and effort not in descriptor["reasoning"]:
            raise subscription_pool.RequestRejected("The selected model does not support that effort level.")
        self.check_profile(name, cancel=cancel)
        self._require_unique_identity(name, cancel=cancel)
        changed = profile.get("identity_changed_at")
        if changed and profile.get("identity_change_pending", True):
            report = self.usage_reports(force=True, cancel=cancel).get(str(account["id"]), {})
            if not report.get("available") or (report.get("source") != "local" and
                    report.get("observed_at", 0) < changed):
                raise ConnectorError("Refresh this profile's subscription usage after changing its login.")
            # The lease was admitted with the previous identity's observations.
            # Persist the replacement's scoped report and ask the pool to admit
            # again; a Sonnet-only limit must not cool down Haiku or Opus.
            subscription_pool.refresh_quota(PROVIDER)
            profile["identity_change_pending"] = False
            raise subscription_pool.AdmissionChanged()
        arguments = ["--print", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                     "--model", model_name, "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                     "--safe-mode", "--restricted", "--disable-slash-commands", "--no-session-persistence",
                     "--system-prompt-snapshot", "off", "--max-turns", "1"]
        if effort:
            arguments.extend(["--effort", "xhigh" if effort == "extra" else effort])
        with tempfile.TemporaryDirectory(prefix="bc-claude-", dir=profile["home"]) as work:
            system_file = Path(work) / "system.txt"
            system_file.write_text(system_text, encoding="utf-8")
            arguments.extend(["--system-prompt-file", str(system_file)])
            result = None
            confined = False
            streamed = set()
            for record in self._records(profile, arguments, prompt, cancel=cancel, cwd=work,
                                      started=subscription_pool.cli_started):
                kind = record.get("type")
                if result is not None:
                    raise ConnectorError("Claude Code returned output after its terminal result.")
                if kind == "system" and record.get("subtype") == "init":
                    # The session announces what the model may run; flags alone are not proof.
                    if record.get("tools") != [] or record.get("mcp_servers") != [] \
                            or record.get("apiKeySource") != "none":
                        raise ConnectorError("Claude Code exposed tools or a non-subscription credential. Review the profile.")
                    confined = True
                elif kind in ("stream_event", "assistant", "result") and not confined:
                    raise ConnectorError("Claude Code did not confirm that its tools are disabled.")
                if kind == "stream_event":
                    event = record.get("event", {})
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") in ("text_delta", "thinking_delta"):
                            key = "text" if delta["type"] == "text_delta" else "thinking"
                            text = delta.get(key)
                            if not isinstance(text, str):
                                raise ConnectorError("Claude Code returned an invalid text delta.")
                            streamed.add(key)
                            yield {key: text}
                elif kind == "assistant":
                    # Non-streaming clients may emit complete blocks. Do not duplicate partial text.
                    for block in record.get("message", {}).get("content", []):
                        key = block.get("type")
                        if key in ("text", "thinking") and key not in streamed:
                            text = block.get(key)
                            if not isinstance(text, str):
                                raise ConnectorError("Claude Code returned invalid assistant content.")
                            yield {key: text}
                elif kind == "rate_limit_event":
                    info = record.get("rate_limit_info", {})
                    # Extra usage is billed beyond the subscription: rest the account rather than spend it.
                    if info.get("status") == "rejected" or info.get("isUsingOverage") is True:
                        reset = info.get("resetsAt")
                        when = None
                        if isinstance(reset, (int, float)) and not isinstance(reset, bool) and math.isfinite(reset):
                            try:
                                when = datetime.fromtimestamp(reset, timezone.utc)
                            except (ValueError, OverflowError, OSError):
                                pass
                        raise subscription_pool.QuotaExhausted(resets_at=when)
                elif kind == "result":
                    if record.get("is_error") or record.get("subtype") != "success":
                        if subscription_pool.request_too_long(record.get("result")):
                            raise subscription_pool.RequestRejected("The conversation is too long for this model.")
                        raise ConnectorError("Claude Code could not complete the response.")
                    usage = record.get("usage")
                    if not isinstance(usage, dict) or not {"input_tokens", "output_tokens"} <= usage.keys():
                        raise ConnectorError("Claude Code omitted terminal token usage.")
                    tokens_in = sum(_number(usage.get(k, 0)) for k in
                                    ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                    tokens_out = _number(usage.get("output_tokens", 0))
                    if tokens_in > accounts.TOKENS_MAX:
                        raise ConnectorError("Claude Code token usage exceeded the accounting limit.")
                    finish = "length" if record.get("stop_reason") == "max_tokens" else "stop"
                    result = {"tokens_in": tokens_in, "tokens_out": tokens_out, "done": True, "finish_reason": finish}
            if result is None:
                raise ConnectorError("Claude Code stopped without a terminal result.")
            yield result


def create_adapter(config):
    return Adapter(config)
