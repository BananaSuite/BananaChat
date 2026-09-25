"""Isolated tests for the compute-side checkpoint agent."""

import hashlib
import io
import json
import os
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from compute.checkpoint_agent import (
    AgentConfig,
    AgentError,
    AgentHTTPServer,
    CheckpointAgent,
    HuggingFaceFetcher,
    _SafeRedirectHandler,
)


def safetensors(data=b"weights", header=None):
    if header is None:
        header = {
            "weight": {
                "dtype": "U8",
                "shape": [len(data)],
                "data_offsets": [0, len(data)],
            }
        }
    encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack("<Q", len(encoded)) + encoded + data


class FakeResponse(io.BytesIO):
    def __init__(self, payload, content_length=True):
        super().__init__(payload)
        self.headers = {}
        if content_length:
            self.headers["Content-Length"] = str(len(payload))


class FakeFetcher:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def open(self, source, token):
        self.calls.append((dict(source), token))
        payload = self.payload() if callable(self.payload) else self.payload
        return FakeResponse(payload)


class BlockingResponse:
    def __init__(self, payload, entered, release):
        self.payload = payload
        self.entered = entered
        self.release = release
        self.sent = False
        self.headers = {"Content-Length": str(len(payload))}

    def read(self, _size):
        if self.sent:
            return b""
        self.entered.set()
        self.release.wait(5)
        self.sent = True
        return self.payload

    def close(self):
        pass


class BlockingFetcher:
    def __init__(self, payload):
        self.payload = payload
        self.entered = threading.Event()
        self.release = threading.Event()

    def open(self, _source, _token):
        return BlockingResponse(self.payload, self.entered, self.release)


class CheckpointAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "checkpoints"
        self.state = base / "state"
        self.root.mkdir()
        self.state.mkdir(mode=0o700)
        self.token_file = base / "agent.token"
        self.token_file.write_text("test-bearer-token\n", encoding="utf-8")
        self.token_file.chmod(0o600)
        self.agents = []

    def tearDown(self):
        for agent in reversed(self.agents):
            agent.close()
        self.temp.cleanup()

    def config(self, **overrides):
        values = {
            "host": "127.0.0.1",
            "port": 0,
            "checkpoint_root": self.root,
            "state_dir": self.state,
            "token_file": self.token_file,
            "queue_size": 4,
            "max_download_bytes": 1024 * 1024,
            "disk_reserve_bytes": 0,
            "json_body_limit": 4096,
            "request_timeout": 1,
        }
        values.update(overrides)
        return AgentConfig(**values)

    def agent(self, payload=None, fetcher=None, start_worker=True, **config):
        if payload is None:
            payload = safetensors()
        instance = CheckpointAgent(
            self.config(**config),
            fetcher=fetcher or FakeFetcher(payload),
            start_worker=start_worker,
        )
        self.agents.append(instance)
        return instance

    @staticmethod
    def request(payload, key="request-1", **changes):
        body = {
            "source": {
                "type": "huggingface",
                "repo_id": "owner/repository",
                "filename": "weights/model.safetensors",
                "revision": "main",
            },
            "target_name": "vendor/model.safetensors",
            "expected_sha256": hashlib.sha256(payload).hexdigest(),
            "expected_size": len(payload),
            "idempotency_key": key,
        }
        body.update(changes)
        return body

    def wait(self, agent, job_id, states=("completed", "failed", "canceled")):
        deadline = time.time() + 5
        while time.time() < deadline:
            job = agent.get_job(job_id)
            if job["status"] in states:
                return job
            time.sleep(0.01)
        self.fail("job did not reach a terminal state")

    def test_token_file_must_be_mode_0600(self):
        self.token_file.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "mode-0600"):
            CheckpointAgent(self.config(), fetcher=FakeFetcher(safetensors()))

    def test_authentication_uses_exact_bearer_token(self):
        agent = self.agent(start_worker=False)
        self.assertTrue(agent.authenticate("Bearer test-bearer-token"))
        self.assertFalse(agent.authenticate(None))
        self.assertFalse(agent.authenticate("bearer test-bearer-token"))
        self.assertFalse(agent.authenticate("Bearer test-bearer-token-extra"))

    def test_rejects_malformed_source_traversal_and_non_safetensors(self):
        payload = safetensors()
        agent = self.agent(payload=payload, start_worker=False)
        cases = []
        arbitrary = self.request(payload, key="arbitrary")
        arbitrary["source"] = {"type": "url", "url": "https://example.test/a.safetensors"}
        cases.append(arbitrary)
        repo_traversal = self.request(payload, key="repo")
        repo_traversal["source"]["repo_id"] = "../repository"
        cases.append(repo_traversal)
        source_traversal = self.request(payload, key="source")
        source_traversal["source"]["filename"] = "../model.safetensors"
        cases.append(source_traversal)
        target_traversal = self.request(payload, key="target", target_name="x/../model.safetensors")
        cases.append(target_traversal)
        wrong_extension = self.request(payload, key="extension", target_name="model.ckpt")
        cases.append(wrong_extension)
        missing_hash = self.request(payload, key="hash")
        del missing_hash["expected_sha256"]
        cases.append(missing_hash)
        unknown = self.request(payload, key="unknown")
        unknown["url"] = "https://example.test"
        cases.append(unknown)
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaises(AgentError):
                    agent.submit(body)

    def test_symlink_target_directory_is_rejected(self):
        payload = safetensors()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.root / "linked").symlink_to(outside, target_is_directory=True)
        agent = self.agent(payload=payload)
        body = self.request(payload, target_name="linked/model.safetensors")
        job, _ = agent.submit(body)
        result = self.wait(agent, job["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "unsafe_target")
        self.assertFalse((outside / "model.safetensors").exists())

    def test_idempotency_returns_original_and_conflicts_on_changed_request(self):
        payload = safetensors()
        agent = self.agent(payload=payload, start_worker=False)
        first, created = agent.submit(self.request(payload))
        duplicate, duplicate_created = agent.submit(self.request(payload))
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first["id"], duplicate["id"])
        changed = self.request(payload, target_name="other.safetensors")
        with self.assertRaisesRegex(AgentError, "already used"):
            agent.submit(changed)

    def test_queue_is_bounded(self):
        payload = safetensors()
        agent = self.agent(payload=payload, start_worker=False, queue_size=1)
        agent.submit(self.request(payload, key="first"))
        with self.assertRaisesRegex(AgentError, "queue is full"):
            agent.submit(
                self.request(payload, key="second", target_name="second.safetensors")
            )

    def test_size_hash_and_safetensors_validation(self):
        valid = safetensors()
        cases = [
            (
                "size_mismatch",
                valid,
                self.request(valid, expected_size=len(valid) + 1),
            ),
            (
                "hash_mismatch",
                valid,
                self.request(valid, expected_sha256="0" * 64),
            ),
            (
                "invalid_safetensors",
                b"not-a-safetensors-file",
                self.request(
                    b"not-a-safetensors-file",
                    expected_size=len(b"not-a-safetensors-file"),
                ),
            ),
            (
                "invalid_safetensors",
                safetensors(b"x", {"x": {"dtype": "U8", "shape": [1], "data_offsets": [0, 2]}}),
                None,
            ),
        ]
        for index, (expected_code, payload, body) in enumerate(cases):
            with self.subTest(expected_code=expected_code, index=index):
                if body is None:
                    body = self.request(payload)
                body["idempotency_key"] = "validation-{}".format(index)
                body["target_name"] = "validation-{}.safetensors".format(index)
                agent = self.agent(payload=payload)
                job, _ = agent.submit(body)
                result = self.wait(agent, job["id"])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"]["code"], expected_code)
                self.assertFalse((self.root / body["target_name"]).exists())

    def test_stream_limit_and_disk_reserve_are_enforced(self):
        payload = safetensors(b"x" * 128)
        too_large = self.agent(
            payload=payload, max_download_bytes=len(payload) - 1
        )
        body = self.request(payload, expected_size=None)
        job, _ = too_large.submit(body)
        result = self.wait(too_large, job["id"])
        self.assertEqual(result["error"]["code"], "source_too_large")

        no_space = self.agent(
            payload=payload, disk_reserve_bytes=1024**5
        )
        body = self.request(
            payload, key="disk-reserve", target_name="reserve.safetensors"
        )
        job, _ = no_space.submit(body)
        result = self.wait(no_space, job["id"])
        self.assertEqual(result["error"]["code"], "insufficient_storage")

    def test_atomic_install_lists_managed_file_and_never_overwrites(self):
        payload = safetensors(b"first")
        agent = self.agent(payload=payload)
        job, _ = agent.submit(self.request(payload))
        result = self.wait(agent, job["id"])
        self.assertEqual(result["status"], "completed")
        installed = self.root / "vendor" / "model.safetensors"
        self.assertEqual(installed.read_bytes(), payload)
        listed = agent.list_checkpoints()["checkpoints"]
        self.assertEqual([item["name"] for item in listed], ["vendor/model.safetensors"])
        self.assertEqual(listed[0]["digest"], hashlib.sha256(payload).hexdigest())

        second = safetensors(b"second")
        agent.fetcher = FakeFetcher(second)
        other, _ = agent.submit(self.request(second, key="second-install"))
        failed = self.wait(agent, other["id"])
        self.assertEqual(failed["error"]["code"], "target_exists")
        self.assertEqual(installed.read_bytes(), payload)

    def test_cancellation_removes_partial_file(self):
        payload = safetensors(b"cancel-me")
        fetcher = BlockingFetcher(payload)
        agent = self.agent(fetcher=fetcher)
        job, _ = agent.submit(self.request(payload))
        self.assertTrue(fetcher.entered.wait(2))
        canceled = agent.cancel(job["id"])
        self.assertEqual(canceled["status"], "downloading")
        fetcher.release.set()
        result = self.wait(agent, job["id"])
        self.assertEqual(result["status"], "canceled")
        self.assertFalse((self.root / "vendor" / "model.safetensors").exists())
        self.assertEqual(list((self.root / "vendor").glob("*.part")), [])

    def test_delete_requires_match_and_refuses_changed_or_unmanaged_files(self):
        payload = safetensors(b"delete")
        agent = self.agent(payload=payload)
        job, _ = agent.submit(self.request(payload))
        completed = self.wait(agent, job["id"])
        digest = completed["digest"]
        with self.assertRaisesRegex(AgentError, "If-Match is required") as missing:
            agent.delete_checkpoint("vendor/model.safetensors", None)
        self.assertEqual(missing.exception.status, 428)
        with self.assertRaises(AgentError) as mismatch:
            agent.delete_checkpoint("vendor/model.safetensors", "0" * 64)
        self.assertEqual(mismatch.exception.status, 412)
        target = self.root / "vendor" / "model.safetensors"
        target.write_bytes(safetensors(b"changed"))
        with self.assertRaises(AgentError) as changed:
            agent.delete_checkpoint("vendor/model.safetensors", digest)
        self.assertEqual(changed.exception.status, 412)
        target.write_bytes(payload)
        deleted = agent.delete_checkpoint("vendor/model.safetensors", '"{}"'.format(digest))
        self.assertEqual(deleted["digest"], digest)
        self.assertFalse(target.exists())
        unmanaged = self.root / "unmanaged.safetensors"
        unmanaged.write_bytes(payload)
        with self.assertRaises(AgentError) as unknown:
            agent.delete_checkpoint("unmanaged.safetensors", digest)
        self.assertEqual(unknown.exception.status, 404)
        self.assertTrue(unmanaged.exists())

    def test_completed_metadata_survives_restart(self):
        payload = safetensors(b"persistent")
        first = self.agent(payload=payload)
        job, _ = first.submit(self.request(payload))
        completed = self.wait(first, job["id"])
        first.close()
        self.agents.remove(first)

        restarted = self.agent(payload=b"should-not-download", start_worker=False)
        restored = restarted.get_job(job["id"])
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(restored["digest"], completed["digest"])
        self.assertEqual(len(restarted.list_checkpoints()["checkpoints"]), 1)

    def test_queued_job_is_recovered_after_restart(self):
        payload = safetensors(b"recover")
        first = self.agent(payload=payload, start_worker=False)
        job, _ = first.submit(self.request(payload))
        first.close()
        self.agents.remove(first)

        restarted = self.agent(payload=payload)
        result = self.wait(restarted, job["id"])
        self.assertEqual(result["status"], "completed")

    def test_graceful_shutdown_recovers_active_job_with_full_pending_queue(self):
        payload = safetensors(b"active-and-full")
        fetcher = BlockingFetcher(payload)
        first = self.agent(fetcher=fetcher, queue_size=1)
        active, _ = first.submit(self.request(payload, key="active"))
        self.assertTrue(fetcher.entered.wait(2))
        pending, _ = first.submit(
            self.request(
                payload,
                key="pending",
                target_name="vendor/pending.safetensors",
            )
        )

        closed = threading.Event()

        def close_agent():
            first.close()
            closed.set()

        closer = threading.Thread(target=close_agent)
        closer.start()
        time.sleep(0.05)
        fetcher.release.set()
        closer.join(5)
        self.assertTrue(closed.is_set())
        self.agents.remove(first)

        restarted = self.agent(payload=payload, queue_size=1)
        self.assertEqual(self.wait(restarted, active["id"])["status"], "completed")
        self.assertEqual(self.wait(restarted, pending["id"])["status"], "completed")

    def test_restart_rejects_unsafe_persisted_target(self):
        payload = safetensors(b"state-validation")
        first = self.agent(payload=payload, start_worker=False)
        first.submit(self.request(payload))
        first.close()
        self.agents.remove(first)
        state_file = self.state / "state.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        next(iter(state["jobs"].values()))["target_name"] = "../escape.safetensors"
        state_file.write_text(json.dumps(state), encoding="utf-8")
        state_file.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "job state"):
            CheckpointAgent(self.config(), fetcher=FakeFetcher(payload), start_worker=False)

    def test_huggingface_url_is_internal_and_redirects_drop_auth(self):
        fetcher = HuggingFaceFetcher(timeout=1)
        captured = {}

        class Opener:
            def open(self, request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                return FakeResponse(b"")

        fetcher.opener = Opener()
        fetcher.open(
            {
                "repo_id": "owner/repository",
                "revision": "main",
                "filename": "folder/model.safetensors",
            },
            "hf-secret",
        )
        request = captured["request"]
        self.assertEqual(
            request.full_url,
            "https://huggingface.co/owner/repository/resolve/main/folder/model.safetensors",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer hf-secret")

        handler = _SafeRedirectHandler()
        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://cdn-lfs.huggingface.co/object",
        )
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://example.test/object",
            )


class CheckpointAgentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "checkpoints"
        self.state = base / "state"
        self.root.mkdir()
        self.state.mkdir(mode=0o700)
        self.token_file = base / "agent.token"
        self.token_file.write_text("test-bearer-token\n", encoding="utf-8")
        self.token_file.chmod(0o600)
        self.payload = safetensors(b"http")
        config = AgentConfig(
            host="127.0.0.1",
            port=0,
            checkpoint_root=self.root,
            state_dir=self.state,
            token_file=self.token_file,
            queue_size=4,
            max_download_bytes=1024 * 1024,
            disk_reserve_bytes=0,
            json_body_limit=4096,
            request_timeout=1,
        )
        self.http_agent = CheckpointAgent(config, fetcher=FakeFetcher(self.payload))
        self.server = AgentHTTPServer(("127.0.0.1", 0), self.http_agent)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:{}".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.http_agent.close()
        self.temp.cleanup()

    def wait(self, job_id):
        deadline = time.time() + 5
        while time.time() < deadline:
            job = self.http_agent.get_job(job_id)
            if job["status"] in ("completed", "failed", "canceled"):
                return job
            time.sleep(0.01)
        self.fail("job did not reach a terminal state")

    def call(self, method, path, body=None, token="test-bearer-token", headers=None):
        request_headers = {} if headers is None else dict(headers)
        if token is not None:
            request_headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=request_headers, method=method
        )
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as exc:
            response = exc
        raw = response.read()
        return response.status, json.loads(raw.decode("utf-8")), response.headers

    def test_health_is_public_but_v1_requires_authentication(self):
        status, body, _ = self.call("GET", "/healthz", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})
        status, body, headers = self.call("GET", "/v1/status", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        self.assertIn("Bearer", headers["WWW-Authenticate"])
        status, body, _ = self.call("GET", "/v1/status", token="wrong")
        self.assertEqual(status, 401)
        self.assertNotIn(str(self.root), json.dumps(body))

    def test_http_download_status_list_and_delete(self):
        body = CheckpointAgentTests.request(self.payload, key="http-request")
        status, submitted, _ = self.call("POST", "/v1/downloads", body)
        self.assertEqual(status, 202)
        completed = self.wait(submitted["id"])
        status, fetched, _ = self.call("GET", "/v1/downloads/" + submitted["id"])
        self.assertEqual(status, 200)
        self.assertEqual(fetched["status"], "completed")
        status, listed, _ = self.call("GET", "/v1/checkpoints")
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["checkpoints"]), 1)
        path = "/v1/checkpoints?" + urllib.parse.urlencode({"name": body["target_name"]})
        status, error, _ = self.call("DELETE", path)
        self.assertEqual(status, 428)
        status, deleted, _ = self.call(
            "DELETE", path, headers={"If-Match": completed["digest"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(deleted["deleted"], body["target_name"])

    def test_http_rejects_invalid_json_content_type_and_oversize_body(self):
        request = urllib.request.Request(
            self.base_url + "/v1/downloads",
            data=b"not-json",
            headers={
                "Authorization": "Bearer test-bearer-token",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as invalid:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(invalid.exception.code, 400)

        wrong_type = urllib.request.Request(
            self.base_url + "/v1/downloads",
            data=b"{}",
            headers={
                "Authorization": "Bearer test-bearer-token",
                "Content-Type": "text/plain",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as media:
            urllib.request.urlopen(wrong_type, timeout=2)
        self.assertEqual(media.exception.code, 415)
        large = b"{" + b" " * (self.http_agent.config.json_body_limit + 1) + b"}"
        too_large = urllib.request.Request(
            self.base_url + "/v1/downloads",
            data=large,
            headers={
                "Authorization": "Bearer test-bearer-token",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as oversized:
            urllib.request.urlopen(too_large, timeout=2)
        self.assertEqual(oversized.exception.code, 413)


if __name__ == "__main__":
    unittest.main()
