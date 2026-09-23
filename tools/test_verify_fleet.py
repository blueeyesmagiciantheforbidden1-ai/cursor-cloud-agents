"""Local fake-hub tests for tools/verify_fleet.py. No cloud or live hub calls."""
import contextlib
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_fleet as vf

MANAGER = "manager-token-for-test"
IDENTITY = "identity-token-for-test"


def _messages(texts=None, codes=None):
    messages = []
    for agent in vf.AGENTS:
        if texts is not None and agent not in texts:
            continue
        text = vf.expected_reply(agent) if texts is None else texts[agent]
        code = 0 if codes is None else codes.get(agent, 0)
        messages.append({"agent": agent, "text": text, "exit_code": code, "step": len(messages)})
    return messages


def ok_outcome():
    return {"status": "completed", "messages": _messages(), "queued_polls": 0}


class FakeHub:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.posts = []
        self.gets = []
        self.headers = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format_string, *args):
                return

            def _authorized(self):
                token = self.headers.get("X-Hub-Token")
                authorization = self.headers.get("Authorization")
                parent.headers.append({"X-Hub-Token": token, "Authorization": authorization})
                return token == MANAGER and authorization == "Bearer " + IDENTITY

            def _read_body(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))

            def _send(self, status, payload):
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self):
                body = self._read_body()
                if not self._authorized():
                    self._send(401, {"error": "Authentication required"})
                    return
                if self.path != "/v1/rooms":
                    self._send(404, {"error": "Not found"})
                    return
                if not parent.outcomes:
                    self._send(500, {"error": "No scripted rooms left"})
                    return
                outcome = parent.outcomes.pop(0)
                room_id = f"{len(parent.posts) + 1:032x}"
                created_at = 1_700_000_000 + len(parent.posts)
                record = {"id": room_id, "created_at": created_at, "gets": 0, **outcome}
                parent.posts.append({"body": body, "room": record})
                self._send(200, _room_view(record, "queued", []))

            def do_GET(self):
                if not self._authorized():
                    self._send(401, {"error": "Authentication required"})
                    return
                prefix = "/v1/rooms/"
                if not self.path.startswith(prefix):
                    self._send(404, {"error": "Not found"})
                    return
                room_id = self.path[len(prefix):]
                room = next((item["room"] for item in parent.posts if item["room"]["id"] == room_id), None)
                if room is None:
                    self._send(404, {"error": "Collaboration not found"})
                    return
                room["gets"] += 1
                parent.gets.append(room_id)
                if room["gets"] <= room.get("queued_polls", 0):
                    self._send(200, _room_view(room, "queued", []))
                    return
                self._send(200, _room_view(room, room["status"], room["messages"]))

        ThreadingHTTPServer.allow_reuse_address = True
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.server.server_address[1]), 0.2):
                    return self
            except OSError:
                time.sleep(0.01)
        raise RuntimeError("fake hub did not accept connections")

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def _room_view(room, status, messages):
    return {
        "id": room["id"],
        "created_at": room["created_at"],
        "status": status,
        "agents": list(vf.AGENTS),
        "messages": messages,
        "workspace": "default",
        "purpose": "project",
        "timeout_seconds": 300,
    }


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class VerifyFleetTests(unittest.TestCase):
    def _run(self, outcomes, args, *, poll_seconds=vf.POLL_SECONDS, deadline_seconds=vf.DEADLINE_SECONDS,
             sleep=None, clock=None):
        clock = Clock() if clock is None else clock
        sleeps = []

        def default_sleep(seconds):
            sleeps.append(seconds)
            clock.advance(seconds)

        stdout, stderr = StringIO(), StringIO()
        with tempfile.TemporaryDirectory() as directory, FakeHub(outcomes) as hub:
            ledger = str(Path(directory) / "ledger.json")
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = vf.main(["--hub", hub.url, "--ledger", ledger, *args],
                               env={"HUB_MANAGER_TOKEN": MANAGER, "HUB_ID_TOKEN": IDENTITY},
                               sleep=default_sleep if sleep is None else sleep,
                               clock=clock, poll_seconds=poll_seconds,
                               deadline_seconds=deadline_seconds)
            text = Path(ledger).read_text(encoding="utf-8")
            payload = json.loads(text)
            posts = list(hub.posts)
            gets = list(hub.gets)
            headers = list(hub.headers)
        combined = stdout.getvalue() + stderr.getvalue() + text
        self.assertNotIn(MANAGER, combined)
        self.assertNotIn(IDENTITY, combined)
        self.assertNotIn("Bearer ", combined)
        return {"code": code, "stdout": stdout.getvalue(), "stderr": stderr.getvalue(),
                "ledger": payload, "posts": posts, "gets": gets, "headers": headers, "sleeps": sleeps}

    def test_pass_prints_exit_codes_and_writes_ledger(self):
        result = self._run([ok_outcome()], ["--consecutive", "1", "--max-rooms", "1"])
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(result["posts"]), 1)
        self.assertEqual(result["posts"][0]["body"], {
            "prompt": vf.PROMPT,
            "agents": ["codex", "claude", "cursor", "copilot", "grok"],
            "timeout_seconds": 300,
            "workspace": "default",
            "purpose": "project",
        })
        self.assertEqual(result["headers"][0], {
            "X-Hub-Token": MANAGER,
            "Authorization": "Bearer " + IDENTITY,
        })
        self.assertEqual(result["gets"], ["0" * 31 + "1"])
        room = result["ledger"]["rooms"][0]
        self.assertEqual(room["room_id"], "0" * 31 + "1")
        self.assertEqual(room["created_at"], 1_700_000_000)
        self.assertEqual(room["exit_codes"], {agent: 0 for agent in vf.AGENTS})
        self.assertTrue(room["pass"])
        self.assertEqual(result["ledger"]["result"], "pass")
        for agent in vf.AGENTS:
            self.assertIn(f"{agent} exit_code=0 text={vf.expected_reply(agent)}", result["stdout"])
        self.assertIn("consecutive=1/1", result["stdout"])

    def test_failed_agent_is_not_five_for_five(self):
        outcome = {"status": "failed", "queued_polls": 0, "messages": _messages(
            {"codex": "CODEX | OK", "claude": "provider exploded"},
            {"codex": 0, "claude": 1})}
        result = self._run([outcome], ["--consecutive", "1", "--max-rooms", "1"])
        self.assertEqual(result["code"], 1)
        room = result["ledger"]["rooms"][0]
        self.assertFalse(room["pass"])
        self.assertEqual(room["status"], "failed")
        self.assertEqual(room["exit_codes"]["codex"], 0)
        self.assertEqual(room["exit_codes"]["claude"], 1)
        self.assertIsNone(room["exit_codes"]["cursor"])
        self.assertIn("claude exit_code=1 text=provider exploded", result["stdout"])
        self.assertIn("cursor exit_code=missing text=", result["stdout"])
        self.assertEqual(result["ledger"]["result"], "fail")

    def test_stalled_room_fails_and_polls_every_ten_seconds(self):
        outcome = {"status": "stalled", "queued_polls": 2, "messages": [
            {"agent": "codex", "text": "CODEX | OK", "exit_code": 0}]}
        result = self._run([outcome], ["--consecutive", "1", "--max-rooms", "1"])
        self.assertEqual(result["code"], 1)
        self.assertEqual(result["sleeps"], [10, 10])
        self.assertEqual(result["gets"], ["0" * 31 + "1"] * 3)
        room = result["ledger"]["rooms"][0]
        self.assertEqual(room["status"], "stalled")
        self.assertFalse(room["pass"])
        self.assertEqual(room["exit_codes"]["codex"], 0)
        self.assertIsNone(room["exit_codes"]["grok"])
        self.assertIn("status=stalled result=fail", result["stdout"])

    def test_consecutive_counter_resets_after_a_failed_room(self):
        failed = {"status": "failed", "queued_polls": 0, "messages": [
            {"agent": "codex", "text": "CODEX | OK", "exit_code": 0},
            {"agent": "cursor", "text": "CURSOR | NO", "exit_code": 7},
        ]}
        result = self._run([ok_outcome(), failed, ok_outcome(), ok_outcome(), ok_outcome()],
                           ["--consecutive", "3", "--max-rooms", "8"])
        self.assertEqual(result["code"], 0)
        self.assertEqual([room["pass"] for room in result["ledger"]["rooms"]],
                         [True, False, True, True, True])
        self.assertEqual(result["ledger"]["rooms"][1]["exit_codes"]["cursor"], 7)
        self.assertEqual(result["ledger"]["streak"], 3)
        self.assertEqual(len(result["posts"]), 5)
        self.assertEqual(
            [line for line in result["stdout"].splitlines() if line.startswith("consecutive=")],
            ["consecutive=1/3", "consecutive=0/3", "consecutive=1/3", "consecutive=2/3", "consecutive=3/3"])

    def test_reply_text_cannot_echo_tokens(self):
        outcome = {"status": "failed", "queued_polls": 0, "messages": [
            {"agent": "codex", "text": "leak " + MANAGER + " and " + IDENTITY, "exit_code": 1}]}
        result = self._run([outcome], ["--consecutive", "1", "--max-rooms", "1"])
        self.assertEqual(result["code"], 1)
        self.assertIn("codex exit_code=1 text=leak [redacted] and [redacted]", result["stdout"])

    def test_exact_reply_and_completed_status_are_required(self):
        good = _room_view({"id": "a" * 32, "created_at": 1}, "completed", _messages())
        self.assertTrue(vf.five_for_five(good))
        wrong = _room_view({"id": "b" * 32, "created_at": 1}, "completed", _messages(
            {**{agent: vf.expected_reply(agent) for agent in vf.AGENTS}, "grok": "GROK | NO"}))
        self.assertFalse(vf.five_for_five(wrong))
        padded = _room_view({"id": "d" * 32, "created_at": 1}, "completed", _messages(
            {**{agent: vf.expected_reply(agent) for agent in vf.AGENTS}, "grok": "GROK | OK\n"}))
        self.assertTrue(vf.five_for_five(padded))
        stalled = _room_view({"id": "c" * 32, "created_at": 1}, "stalled", _messages())
        self.assertFalse(vf.five_for_five(stalled))

    def test_queued_room_fails_when_fifteen_minutes_elapse(self):
        self.assertEqual(vf.POLL_SECONDS, 10)
        self.assertEqual(vf.DEADLINE_SECONDS, 900)
        outcome = {"status": "queued", "queued_polls": 10_000, "messages": []}
        result = self._run([outcome], ["--consecutive", "1", "--max-rooms", "1"])
        self.assertEqual(result["code"], 1)
        self.assertEqual(result["sleeps"], [10] * 90)
        self.assertEqual(sum(result["sleeps"]), 900)
        room = result["ledger"]["rooms"][0]
        self.assertFalse(room["pass"])
        self.assertEqual(room["status"], "queued")
        self.assertTrue(all(code is None for code in room["exit_codes"].values()))

    def test_missing_token_and_non_loopback_http_do_not_call_out(self):
        stdout, stderr = StringIO(), StringIO()
        with tempfile.TemporaryDirectory() as directory:
            ledger = str(Path(directory) / "ledger.json")
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = vf.main(["--hub", "http://example.com", "--ledger", ledger, "--consecutive", "1"],
                               env={"HUB_MANAGER_TOKEN": MANAGER, "HUB_ID_TOKEN": IDENTITY})
            self.assertEqual(code, 1)
            self.assertFalse(Path(ledger).exists())
            self.assertIn("HTTPS", stderr.getvalue())
            self.assertNotIn(MANAGER, stderr.getvalue())
            with contextlib.redirect_stderr(stderr):
                missing = vf.main(["--hub", "http://127.0.0.1:9", "--ledger", ledger], env={})
            self.assertEqual(missing, 1)
            self.assertIn("Set HUB_MANAGER_TOKEN", stderr.getvalue())
            self.assertFalse(Path(ledger).exists())

    def test_redirect_is_not_followed(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format_string, *args):
                return

            def _reject(self):
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length:
                    self.rfile.read(length)
                seen.append(self.path)
                self.send_response(302)
                host, port = self.server.server_address
                self.send_header("Location", f"http://{host}:{port}/stolen")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            do_POST = _reject
            do_GET = _reject

        ThreadingHTTPServer.allow_reuse_address = True
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stdout, stderr = StringIO(), StringIO()
        try:
            with tempfile.TemporaryDirectory() as directory:
                ledger = str(Path(directory) / "ledger.json")
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = vf.main(["--hub", f"http://127.0.0.1:{server.server_address[1]}",
                                    "--ledger", ledger, "--consecutive", "1", "--max-rooms", "1"],
                                   env={"HUB_MANAGER_TOKEN": MANAGER, "HUB_ID_TOKEN": IDENTITY},
                                   sleep=lambda seconds: None, clock=lambda: 0.0,
                                   poll_seconds=10, deadline_seconds=900)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.assertEqual(code, 1)
        self.assertEqual(seen, ["/v1/rooms"])
        self.assertNotIn(MANAGER, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(IDENTITY, stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
