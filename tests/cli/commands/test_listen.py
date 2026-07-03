from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from hooky.cli.commands.listen import make_handler, verify_signature


class VerifySignatureTests(unittest.TestCase):
    def test_accepts_matching_signature(self) -> None:
        body = b'{"hello":"world"}'
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()

        self.assertTrue(verify_signature("secret", body, signature))

    def test_rejects_wrong_secret(self) -> None:
        body = b'{"hello":"world"}'
        signature = "sha256=" + hmac.new(b"other-secret", body, hashlib.sha256).hexdigest()

        self.assertFalse(verify_signature("secret", body, signature))

    def test_rejects_missing_prefix(self) -> None:
        self.assertFalse(verify_signature("secret", b"body", "deadbeef"))


class ListenServerTests(unittest.TestCase):
    def start_server(self, *, secret: str | None = None) -> tuple[ThreadingHTTPServer, int]:
        self.tmp = tempfile.TemporaryDirectory()
        workspace = Path(self.tmp.name)
        handler = make_handler(workspace, None, secret, Path(self.tmp.name) / "last-run")
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(self.tmp.cleanup)
        return server, server.server_address[1]

    def post(self, port: int, body: bytes, *, event: str, signature: str | None = None) -> int:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            data=body,
            method="POST",
            headers={"X-GitHub-Event": event, **({"X-Hub-Signature-256": signature} if signature else {})},
        )
        with urllib.request.urlopen(request) as response:
            return response.status

    def test_healthz_returns_ok(self) -> None:
        _server, port = self.start_server()

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz") as response:
            self.assertEqual(response.status, 200)

    def test_non_triggering_event_is_ignored_and_does_not_spawn_subprocess(self) -> None:
        _server, port = self.start_server()
        body = json.dumps({"label": {"name": "bug"}, "issue": {"number": 1}}).encode()

        with mock.patch.object(subprocess, "run") as run_mock:
            status = self.post(port, body, event="issues")
            time.sleep(0.2)

        self.assertEqual(status, 202)
        run_mock.assert_not_called()

    def test_missing_signature_is_rejected_when_secret_configured(self) -> None:
        _server, port = self.start_server(secret="topsecret")
        body = json.dumps({"label": {"name": "hooky:run"}, "issue": {"number": 1}}).encode()

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post(port, body, event="issues")
        self.assertEqual(ctx.exception.code, 401)

    def test_triggering_event_spawns_hooky_run_subprocess(self) -> None:
        _server, port = self.start_server()
        body = json.dumps({"label": {"name": "hooky:run"}, "issue": {"number": 12, "title": "Broken link"}}).encode()

        with mock.patch.object(subprocess, "run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess([], 0)
            status = self.post(port, body, event="issues")
            deadline = time.monotonic() + 2
            while run_mock.call_count == 0 and time.monotonic() < deadline:
                time.sleep(0.02)

        self.assertEqual(status, 202)
        run_mock.assert_called_once()
        (command,), _kwargs = run_mock.call_args
        self.assertIn("run", command)
        self.assertIn("--run-key", command)
        self.assertIn("issue-12", command)
        self.assertNotIn("--light", command)

    def test_light_implement_comment_on_pr_spawns_run_with_light_flag(self) -> None:
        _server, port = self.start_server()
        body = json.dumps(
            {
                "issue": {"number": 7, "title": "Add feature", "pull_request": {"url": "..."}},
                "comment": {"body": "/hooky also handle nulls"},
            }
        ).encode()

        with mock.patch.object(subprocess, "run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess([], 0)
            status = self.post(port, body, event="issue_comment")
            deadline = time.monotonic() + 2
            while run_mock.call_count == 0 and time.monotonic() < deadline:
                time.sleep(0.02)

        self.assertEqual(status, 202)
        run_mock.assert_called_once()
        (command,), _kwargs = run_mock.call_args
        self.assertIn("run", command)
        self.assertIn("--light", command)
        self.assertIn("pr-7", command)

    def test_review_comment_on_pr_spawns_review_subprocess(self) -> None:
        _server, port = self.start_server()
        body = json.dumps(
            {
                "issue": {"number": 7, "title": "Add feature", "pull_request": {"url": "..."}},
                "comment": {"body": "/hooky review"},
            }
        ).encode()

        with mock.patch.object(subprocess, "run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess([], 0)
            status = self.post(port, body, event="issue_comment")
            deadline = time.monotonic() + 2
            while run_mock.call_count == 0 and time.monotonic() < deadline:
                time.sleep(0.02)

        self.assertEqual(status, 202)
        run_mock.assert_called_once()
        (command,), _kwargs = run_mock.call_args
        self.assertIn("review", command)
        self.assertNotIn("run", command)

    def test_refine_only_comment_on_issue_does_not_spawn_subprocess(self) -> None:
        _server, port = self.start_server()
        body = json.dumps({"issue": {"number": 1, "title": "Broken link"}, "comment": {"body": "/hooky also check the header"}}).encode()

        with mock.patch.object(subprocess, "run") as run_mock:
            status = self.post(port, body, event="issue_comment")
            time.sleep(0.2)

        self.assertEqual(status, 202)
        run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
