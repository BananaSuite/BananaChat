"""Official Codex CLI subscription transport; ChatGPT login only, no API keys.

An operator-owned manifest binds private CODEX_HOME profiles to account labels.
Web administrators select those profiles, never supply executable paths,
credentials, configuration or CLI arguments. Answers come from one-shot
``codex exec`` runs with the model's tools switched off; logins, models and
allowances from the CLI's stdio app server. ``exec`` reports each message only
once it is complete, so an answer arrives in one piece. See docs/codex-cli.md.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager

from bananachat.db import settings
from bananachat.db import subscription_pool as accounts
from bananachat.services import claude_sandbox
from bananachat.services import codex_control
from bananachat.services import subscription_pool
from bananachat.services.subscription_cli import MAX_LINE, ConnectorError, json_file, private
from bananachat.services.subscription_profiles import ManagedProfiles
from bananachat.services.upstream import CancelToken

log = logging.getLogger("bananachat.codex_cli")

PROVIDER = "codex"
BINDINGS_KEY = "codex_cli_profiles"
MAX_BYTES = 8 * 1024 * 1024
METADATA_WORKERS = 4
METADATA_SECONDS = 27  # Leaves eight seconds for process-group cleanup within a 35-second budget.
METADATA_CACHE_SECONDS = 5
AUTHENTICATION_FAILED = "native_authentication_failed"
# Every tool source that configuration can remove. --disable and --strict-config reject
# an unknown name, so a renamed setting stops the CLI instead of handing the model its tool back.
DISABLED_FEATURES = ("shell_tool", "unified_exec", "shell_snapshot", "view_image", "image_generation", "apps",
                     "plugins", "tool_suggest", "multi_agent", "multi_agent_v2", "goals", "memories", "hooks",
                     "skill_mcp_dependency_install", "sleep_tool")
OVERRIDES = ('model_provider="openai"', 'forced_login_method="chatgpt"', 'approval_policy="never"',
            'web_search="disabled"', "agents.enabled=false", "tools.update_plan.enabled=false",
            "tools.experimental_request_user_input.enabled=false", "skills.include_instructions=false",
            "skills.bundled.enabled=false", "cloud.skills.enabled=false", 'shell_environment_policy.inherit="none"',
            "include_permissions_instructions=false", "include_apps_instructions=false",
            "include_collaboration_mode_instructions=false", "include_environment_context=false",
            "project_doc_max_bytes=0", "analytics.enabled=false")
# The model catalogue can still add apply_patch, a clock, tool search, reads from MCP servers
# (none is configured) and the code-mode wrapper, whose script runtime has no file or network
# access and reports no item. The read-only sandbox refuses every patch; any item other than
# an answer ends the run.
ANSWER_ITEMS = ("agent_message", "reasoning", "error")
QUOTA_MESSAGES = ("hit your usage limit", "out of credits", "hit your spend cap")


class ProcessCleanupError(ConnectorError):
    """A descendant still runs; the pool must quarantine this account."""

    requires_quarantine = True


class NativeAuthenticationError(ConnectorError):
    """The CLI positively reports logged-out or non-subscription authentication."""


def _number(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= accounts.TOKENS_MAX:
        raise ConnectorError("Codex returned invalid token usage.")
    return value


def _exhausted(message):
    if not isinstance(message, str):
        return False
    message = message.replace("\u2019", "'").casefold()
    return any(marker in message for marker in QUOTA_MESSAGES)


def _stop(process):
    # Even after the parent exits, descendants must not survive its request.
    _signal(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    _signal(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        raise ProcessCleanupError("Codex did not stop. Review the account before routing more requests.") from None
    if Path("/proc").is_dir():
        # wait() reaps only the leader. A killed grandchild may still be running
        # until the kernel processes SIGKILL; do not release its account yet.
        deadline = time.monotonic() + 2
        while _running(process.pid, deadline=deadline):
            if time.monotonic() >= deadline:
                raise ProcessCleanupError("Codex descendants did not stop. Review the account before routing more requests.")
            _signal(process.pid, signal.SIGKILL)
            time.sleep(0.01)


def _signal(session, number):
    # Codex starts its code-mode host in a process group of its own, so the group
    # alone misses it; it still belongs to the session the CLI leads. A process that
    # cannot be signalled is left to the verification in _stop, which fails closed.
    try:
        os.killpg(session, number)
    except (ProcessLookupError, PermissionError):
        pass
    for pid in _running(session) if Path("/proc").is_dir() else ():
        try:
            os.kill(pid, number)
        except (ProcessLookupError, PermissionError):
            pass


def _running(session, *, deadline=None):
    """Process IDs still running in the session that a transport's CLI leads."""
    found = []
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            if deadline is not None and time.monotonic() >= deadline:
                raise ProcessCleanupError("Codex process cleanup could not be verified. Review the account before routing more requests.")
            try:
                # A process name may contain spaces or ')'; split after its
                # final delimiter. Bounded raw reads keep large /proc scans fast.
                with open(entry.path + "/stat", "rb") as source:
                    fields = source.read(4096).rsplit(b")", 1)[1].split()
                if int(fields[3]) == session and fields[0] not in (b"Z", b"X"):
                    found.append(int(entry.name))
            except (OSError, ValueError, IndexError):
                continue  # The process can disappear while its stat file is read.
    return found


class Adapter(ManagedProfiles):
    provider = PROVIDER
    bindings_key = BINDINGS_KEY

    def __init__(self, config):
        if os.name != "posix":
            raise ConnectorError("The Codex CLI connector requires a Linux/POSIX server.")
        manifest = json_file(config.codex_cli_config)
        if not isinstance(manifest, dict):
            raise ConnectorError("The connector configuration must be an object.")
        binary = Path(manifest.get("binary", ""))
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise ConnectorError("Configure an absolute path to the official Codex CLI executable.")
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
            home = private(profile.get("codex_home", ""), directory=True)
            models = profile.get("models", [])
            discovery = profile.get("discovery", "manual" if models else "automatic")
            usage_source = profile.get("usage_source", "automatic")
            if discovery not in ("automatic", "manual") or usage_source not in ("automatic", "local"):
                raise ConnectorError("Use automatic/manual model discovery and automatic/local usage reporting.")
            if not isinstance(models, list) or len(models) > 200 or (discovery == "manual" and not models):
                raise ConnectorError("Manual discovery needs a verified model list (maximum 200 models).")
            try:
                # Reuse the provider's capability/name validation, without inventing models.
                checked = [{**subscription_pool._model_descriptor(PROVIDER, model),
                            "effort_restricted": "reasoning" in model} for model in models]
            except ValueError:
                raise ConnectorError("Invalid model configuration.") from None
            if any(m["name"].startswith("REPLACE_") for m in checked):
                raise ConnectorError("Replace the example model ID with a verified subscription model.")
            if any(m["capabilities"] != ["completion"] or any(level not in codex_control.LEVELS for level in m["reasoning"])
                   for m in checked):
                raise ConnectorError("Invalid model configuration.")
            self.profiles[name] = {"home": str(home), "models": checked, "discovery": discovery,
                                   "usage_source": usage_source}
            self._profile_locks[name] = threading.RLock()
        if len({p["home"] for p in self.profiles.values()}) != len(self.profiles):
            raise ConnectorError("Each subscription needs a separate authentication directory.")
        self.sandboxed = getattr(config, "environment", "production") not in ("development", "testing")
        forbidden = [Path(__file__).resolve().parents[2], Path(config.codex_cli_config).resolve()]
        # The Claude manifest names that provider's logins; a Codex profile must not expose it.
        forbidden += [getattr(config, name) for name in ("instance_dir", "database_path", "log_file",
                                                         "maintenance_file", "claude_code_config")
                      if getattr(config, name, None)]
        if self.sandboxed:
            try:
                if os.geteuid() == 0:
                    raise claude_sandbox.SandboxError("Production native transport needs an unprivileged identity.")
                self.binary = claude_sandbox.trusted_executable(self.binary)
                claude_sandbox.trusted_executable("/usr/bin/bwrap")
                claude_sandbox.private_roots(forbidden)
                if self.profiles:
                    claude_sandbox.profile_paths({name: self._mounts(profile)
                                                  for name, profile in self.profiles.items()},
                                                 uid=os.getuid(), forbidden=forbidden)
            except FileNotFoundError:
                raise ConnectorError("The production Codex sandbox is not configured: Bubblewrap (/usr/bin/bwrap) "
                                     "or a configured path is missing. Install Bubblewrap (apt install bubblewrap) "
                                     "and run scripts/setup_subscriptions.py.") from None
            except (OSError, claude_sandbox.SandboxError) as error:
                raise ConnectorError("The production Codex sandbox is not configured. Install Bubblewrap and use a "
                                     "root-owned native executable with separate private profiles "
                                     f"({error}).") from None
        self._init_managed(manifest, sandboxed=self.sandboxed, forbidden=forbidden)

    def _managed_profile(self, path):
        return {"home": str(path), "models": [], "discovery": "automatic", "usage_source": "automatic",
                "managed": True}

    @staticmethod
    def _prepare_managed(path):
        return None  # Codex keeps everything in CODEX_HOME itself

    def _command(self, name, arguments, environment):
        profile = self.profiles[name]
        if self.sandboxed:
            return claude_sandbox.command(self.binary, self._mounts(profile), arguments, cwd=profile["home"],
                                          environment=environment)
        return [self.binary, *arguments]

    def login_command(self, name):
        """``(argv, environment, cwd)`` of the official device-code sign-in for a profile."""
        if name not in self.profiles:
            raise ConnectorError("Unknown Codex profile.")
        profile = self.profiles[name]
        environment = self._env(profile)
        return self._command(name, ["login", "--device-auth"], environment), environment, profile["home"]

    def logout_command(self, name):
        if name not in self.profiles:
            raise ConnectorError("Unknown Codex profile.")
        profile = self.profiles[name]
        environment = self._env(profile)
        return self._command(name, ["logout"], environment), environment, profile["home"]

    @staticmethod
    def _mounts(profile):
        """The shared sandbox's view of a profile; Codex keeps its configuration inside its home."""
        return {"home": profile["home"], "config_dir": profile["home"]}

    def _env(self, profile):
        # Allowlist prevents inherited API keys, access tokens, base URLs or CODEX_* overrides.
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE",
                                              "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                                              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                                              "http_proxy", "https_proxy", "all_proxy", "no_proxy") if key in os.environ}
        env.update(HOME=profile["home"], CODEX_HOME=profile["home"], NO_COLOR="1")
        return env

    def _records(self, profile, arguments, data=b"", *, cancel=None, timeout=None, cwd=None, conversation=False):
        """Bounded nonblocking input/output; closes the whole process group on every exit.

        A conversation keeps standard input open: the caller appends its next
        request to ``data`` (a bytearray) after reading the previous reply.
        """
        if cancel is not None:
            cancel.check()
        environment = self._env(profile)
        command = [self.binary, *arguments]
        if self.sandboxed:
            try:
                command = claude_sandbox.command(self.binary, self._mounts(profile), arguments,
                                                cwd=cwd or profile["home"], environment=environment)
            except (OSError, claude_sandbox.SandboxError):
                raise ConnectorError("The Codex sandbox could not be configured safely.") from None
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=cwd or profile["home"], env=environment,
                                   start_new_session=True, close_fds=True)
        deadline = time.monotonic() + (timeout or self.timeout)
        output = bytearray()
        total = 0
        offset = 0
        writing = False
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            if not data and not conversation:
                process.stdin.close()
            try:
                while selector.get_map():
                    if cancel is not None:
                        cancel.check()
                    if time.monotonic() >= deadline:
                        raise ConnectorError("Codex exceeded the request timeout.")
                    if not writing and not process.stdin.closed and offset < len(data):
                        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                        writing = True
                    for key, _ in selector.select(0.1):
                        pipe = key.fileobj
                        if key.data == "stdin":
                            try:
                                offset += os.write(pipe.fileno(), data[offset:offset + 65536])
                            except BrokenPipeError:
                                offset = len(data)
                                conversation = False
                            if offset == len(data):
                                selector.unregister(pipe)
                                writing = False
                                if not conversation:
                                    pipe.close()
                            continue
                        chunk = os.read(pipe.fileno(), 65536)
                        if not chunk:
                            selector.unregister(pipe)
                            continue
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise ConnectorError("Codex exceeded the response size limit.")
                        if key.data == "stderr":
                            continue  # Never forward or log provider stderr/credentials.
                        output.extend(chunk)
                        while b"\n" in output:
                            line, _, rest = output.partition(b"\n")
                            output[:] = rest
                            if len(line) > MAX_LINE:
                                raise ConnectorError("Codex returned an oversized record.")
                            if line.strip():
                                yield self._decode(line)
                        if len(output) > MAX_LINE:
                            raise ConnectorError("Codex returned an oversized record.")
                if output.strip():
                    yield self._decode(output)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConnectorError("Codex exceeded the request timeout.")
                try:
                    code = process.wait(timeout=min(remaining, 3))
                except subprocess.TimeoutExpired:
                    raise ConnectorError("Codex did not stop after completing its output.") from None
                if code:
                    raise ConnectorError("Codex failed. Check the account authentication and model configuration.")
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
            raise ConnectorError("Codex returned invalid structured output.") from None
        if not isinstance(value, dict):
            raise ConnectorError("Codex returned an invalid record.")
        return value

    def bindings(self):
        value = settings.state_get(BINDINGS_KEY, {})
        return self._managed_bindings(value if isinstance(value, dict) else {})

    def profile_for(self, account):
        if account.get("provider", PROVIDER) != PROVIDER:
            raise ConnectorError("This account belongs to another subscription provider.")
        name = self.bindings().get(str(account["id"]), account["label"])
        if name not in self.profiles:
            raise ConnectorError("Select a configured Codex profile for this account.")
        return name, self.profiles[name]

    def check_profile(self, name, *, cancel=None):
        if name not in self.profiles:
            raise ConnectorError("Unknown Codex profile.")
        try:
            account = codex_control.account(self, self.profiles[name], cancel=cancel)
        except codex_control.ControlError:
            raise ConnectorError("Codex could not report this profile's login. Check the CLI version and subscription login.") from None
        if account is None:
            raise NativeAuthenticationError("This profile is not signed in to a ChatGPT subscription. Sign it in "
                                            "from Administration → Models → Codex.")
        if account["type"] != "chatgpt":
            raise NativeAuthenticationError("This profile needs a native ChatGPT subscription login; API-key authentication is not accepted.")
        email = account["email"]
        identity = hashlib.sha256(email.strip().casefold().encode()).hexdigest() \
            if isinstance(email, str) and 0 < len(email) <= 320 else None
        previous = self._identities.get(name)
        if previous and previous[1] != identity:
            from bananachat.services import codex_usage
            with self._model_lock:
                self._models.pop(name, None)
                self._model_epoch += 1
            with self._metadata_cache_lock:
                self._metadata_cache.clear()
            self.profiles[name]["identity_changed_at"] = time.time()
            self.profiles[name]["identity_change_pending"] = True
            codex_usage.invalidate(self, self.profiles[name])
            subscription_pool.invalidate(PROVIDER)
        self._identities[name] = (time.monotonic(), identity)
        plan = account.get("planType")
        return {"authenticated": True, "plan": plan[:40] if isinstance(plan, str) and plan else None}

    def _identity(self, name, *, cancel=None):
        with self._profile_guard(name, cancel):
            cached = self._identities.get(name)
            if not cached or time.monotonic() - cached[0] >= 60:
                self.check_profile(name, cancel=cancel)
            return self._identities.get(name, (0, None))[1]

    @contextmanager
    def _profile_guard(self, name, cancel=None):
        """Serialize a profile's metadata reads without blocking other profiles."""
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
        with self._profile_guard(name, cancel):
            with self._model_lock:
                cached = self._models.get(name)
                epoch = self._model_epoch
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            try:
                self.check_profile(name, cancel=cancel)
                try:
                    detected = codex_control.models(self, profile, cancel=cancel)
                except codex_control.ControlError:
                    raise ConnectorError("Codex model discovery is unavailable. Check the CLI version and subscription login.") from None
                if profile["models"]:
                    # An explicit allowlist is a restriction, never proof that a model still exists.
                    selected = []
                    for wanted in profile["models"]:
                        actual = next((m for m in detected if m["name"] == wanted["name"]), None)
                        if actual:
                            selected.append({**actual, "display": wanted["display"],
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
        from bananachat.services import codex_usage

        with self._profile_guard(name, cancel):
            if kind == "identity":
                self.check_profile(name, cancel=cancel)
                identity, value = self._identities.get(name, (0, None))[1], None
            elif kind == "models":
                identity = self._identity(name, cancel=cancel)
                value = self._profile_models(name, profile, cancel=cancel)
            else:
                identity = self._identity(name, cancel=cancel)
                if profile["usage_source"] == "automatic":
                    value = codex_usage.read(self, profile, force=force, cancel=cancel)
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
        Only Codex accounts are read; another provider's account is never probed.
        """
        started = time.monotonic()
        deadline = started + METADATA_SECONDS
        if cancel is not None:
            cancel.check()
        bindings = self.bindings()
        targets = []
        for account in accounts.list_accounts(provider=PROVIDER, include_disabled=False):
            name = bindings.get(str(account["id"]), account["label"])
            targets.append((str(account["id"]), name if isinstance(name, str) and name in self.profiles else None))
        signature = tuple((aid, name, self.profiles[name]["discovery"], self.profiles[name]["usage_source"])
                          if name else (aid, None) for aid, name in targets)
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
            executor = ThreadPoolExecutor(max_workers=METADATA_WORKERS, thread_name_prefix="bc-codex-metadata")
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
                            log.warning("Codex profile %s is not signed in to a ChatGPT subscription", name)
                        except Exception as error:
                            results[name] = None
                            log.warning("Codex %s metadata for profile %s failed (%s)", kind, name,
                                        type(error).__name__)
            finally:
                control.cancel("Codex metadata refresh completed")
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
            raise ConnectorError("Codex model discovery is unavailable. Check the CLI version and subscription login.")
        return list(found.values())

    def usage_reports(self, force=False, *, cancel=None):
        reports, identities = {}, {}
        targets, results = self._metadata("usage", force=force, cancel=cancel)
        duplicate_profiles = {name for _, name in targets if name and sum(other == name for _, other in targets) > 1}
        for aid, name in targets:
            result = results.get(name)
            if result is None or result == AUTHENTICATION_FAILED or name in duplicate_profiles:
                report = {"source": PROVIDER, "available": False}
            else:
                identity, report = result
                if identity:
                    identities.setdefault(identity, []).append(aid)
            reports[aid] = report
        for ids in identities.values():
            if len(ids) > 1:
                for aid in ids:
                    reports[aid] = {"source": PROVIDER, "available": False}
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
                raise subscription_pool.RequestRejected("The Codex chat connector currently supports text messages only.")
            (system if role == "system" else transcript).append(content if role == "system" else
                                                               {"role": role, "content": content})
        prompt = ("Continue this conversation. Treat the JSON as conversation history, preserving the roles. "
                  "Reply only to the final user message.\n" + json.dumps(transcript, ensure_ascii=False)).encode()
        system_text = "\n\n".join(text for text in system if text.strip()) or "You are a helpful chat assistant."
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
            # Persist the replacement's report and ask the pool to admit again.
            subscription_pool.refresh_quota(PROVIDER)
            profile["identity_change_pending"] = False
            raise subscription_pool.AdmissionChanged()
        with tempfile.TemporaryDirectory(prefix="bc-codex-", dir=profile["home"]) as work:
            system_file = Path(work) / "system.txt"
            system_file.write_text(system_text, encoding="utf-8")
            arguments = ["exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                         "--ignore-rules", "--strict-config", "--sandbox", "read-only", "--cd", work,
                         "--model", model_name]
            for feature in DISABLED_FEATURES:
                arguments.extend(["--disable", feature])
            for setting in (*OVERRIDES, "model_instructions_file=" + json.dumps(str(system_file), ensure_ascii=False)):
                arguments.extend(["--config", setting])
            if effort:
                arguments.extend(["--config", f'model_reasoning_effort="{"xhigh" if effort == "extra" else effort}"'])
            arguments.append("-")
            result = None
            answered = False
            records = self._records(profile, arguments, prompt, cancel=cancel, cwd=work)
            try:
                for record in records:
                    kind = record.get("type")
                    if result is not None:
                        raise ConnectorError("Codex returned output after its terminal result.")
                    if kind in ("item.started", "item.updated", "item.completed"):
                        item = record.get("item")
                        if not isinstance(item, dict):
                            raise ConnectorError("Codex returned an invalid item.")
                        if item.get("type") not in ANSWER_ITEMS:
                            # A command, patch, MCP call, search or delegation could read the
                            # profile's credentials; end the run before its output is answered.
                            raise ConnectorError("Codex tried to use a tool, so the response was stopped.")
                        if kind != "item.completed" or item["type"] == "error":
                            continue  # Notices can carry paths or account details; never forward them.
                        text = item.get("text")
                        if not isinstance(text, str):
                            raise ConnectorError("Codex returned invalid assistant content.")
                        if item["type"] == "reasoning":
                            yield {"thinking": text}
                        else:
                            # exec has no token deltas: each message arrives complete.
                            yield {"text": ("\n\n" if answered else "") + text}
                            answered = True
                    elif kind == "error":
                        if _exhausted(record.get("message")):
                            raise subscription_pool.QuotaExhausted()
                        # Other stream errors may be retried by the CLI; only turn.failed ends a turn.
                    elif kind == "turn.failed":
                        error = record.get("error")
                        if _exhausted(error.get("message") if isinstance(error, dict) else None):
                            raise subscription_pool.QuotaExhausted()
                        raise ConnectorError("Codex could not complete the response.")
                    elif kind == "turn.completed":
                        usage = record.get("usage")
                        if not isinstance(usage, dict) or not {"input_tokens", "output_tokens"} <= usage.keys():
                            raise ConnectorError("Codex omitted terminal token usage.")
                        # Cached input is part of input_tokens and reasoning part of output_tokens.
                        result = {"tokens_in": _number(usage["input_tokens"]),
                                  "tokens_out": _number(usage["output_tokens"]), "done": True, "finish_reason": "stop"}
                    elif kind not in ("thread.started", "turn.started"):
                        raise ConnectorError("Codex returned an unexpected record.")
            finally:
                # A tool attempt must end its process group now, not when the generator is collected.
                records.close()
            if result is None:
                raise ConnectorError("Codex stopped without a terminal result.")
            yield result


def create_adapter(config):
    return Adapter(config)
