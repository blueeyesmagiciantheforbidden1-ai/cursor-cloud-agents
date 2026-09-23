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
ROOM_FIELDS = {"prompt", "agents", "timeout_seconds", "workspace", "purpose"}


def tokens_in(prompt):
    found = {}
    if not isinstance(prompt, str):
        return found
    for agent in vf.AGENTS:
        marker = agent + " replies "
        start = prompt.find(marker)
        if start < 0:
            continue
        found[agent] = prompt[start + len(marker):].split(".", 1)[0].strip()
    return found


def nonce_in(prompt):
    marker = "Room nonce: "
    if not isinstance(prompt, str) or marker not in prompt:
        return ""
    return prompt.split(marker, 1)[1].split(".", 1)[0].strip()


def messages_for(body, outcome):
    if "messages" in outcome:
        return outcome["messages"]
    prompt = body.get("prompt") if isinstance(body, dict) else ""
    tokens = tokens_in(prompt)
    mode = outcome.get("mode", "match")
    wrong = outcome.get("wrong_agent", "claude")
    duplicate = outcome.get("duplicate_agent", "claude")
    drop = outcome.get("drop_agent")
    codes = outcome.get("codes") or {}
    messages = []
    for agent, token in tokens.items():
        if agent == drop:
            continue
        text = "wrong-token" if mode == "wrong" and agent == wrong else token
        code = codes.get(agent, 0)
        messages.append({"agent": agent, "text": text, "exit_code": code, "step": len(messages)})
        if mode == "duplicate" and agent == duplicate:
            messages.append({"agent": agent, "text": token, "exit_code": code, "step": len(messages)})
    return messages


def ready_status(**extra):
    agents = []
    for agent in vf.AGENTS:
        record = {"id": agent, "status": "ready"}
        record.update(extra.get(agent, {}))
        agents.append(record)
    payload = {"agents": agents}
    payload.update(extra.get("_root", {}))
    return payload


class FakeHub:
    def __init__(self, outcomes, status=None):
        self.outcomes = list(outcomes)
        self.status_payload = status
        self.posts = []
        self.gets = []
        self.headers = []
        self.events = []
        self._next = 1
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
                if outcome.get("echo_token_error"):
                    token = next(iter(tokens_in(body.get("prompt", "")).values()), "missing")
                    parent.posts.append({"body": body, "room": None})
                    parent.events.append("post")
                    self._send(500, {"error": "rejected " + token})
                    return
                room_id = f"{parent._next:032x}"
                parent._next += 1
                created_at = 1_700_000_000 + len(parent.posts)
                record = {
                    "id": room_id,
                    "created_at": created_at,
                    "gets": 0,
                    "prompt": body.get("prompt") if isinstance(body, dict) else "",
                    "requested_agents": list(body.get("agents") or []),
                    "timeout_seconds": body.get("timeout_seconds"),
                    "messages": messages_for(body, outcome),
                    "status": outcome.get("status", "completed"),
                    "queued_polls": outcome.get("queued_polls", 0),
                }
                if "effective_roster" in outcome:
                    record["effective_roster"] = outcome["effective_roster"]
                if "queue_deadline" in outcome:
                    record["queue_deadline"] = outcome["queue_deadline"]
                parent.posts.append({"body": body, "room": record})
                parent.events.append("post")
                self._send(200, _room_view(record, "queued", []))

            def do_GET(self):
                if not self._authorized():
                    self._send(401, {"error": "Authentication required"})
                    return
                if self.path == "/v1/status":
                    parent.events.append("status")
                    self._send(200, parent.status_payload if parent.status_payload is not None else {"agents": []})
                    return
                prefix = "/v1/rooms/"
                if not self.path.startswith(prefix):
                    self._send(404, {"error": "Not found"})
                    return
                room_id = self.path[len(prefix):]
                room = next((item["room"] for item in parent.posts
                             if item["room"] and item["room"]["id"] == room_id), None)
                if room is None:
                    self._send(404, {"error": "Collaboration not found"})
                    return
                room["gets"] += 1
                parent.gets.append(room_id)
                parent.events.append("get")
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
    view = {
        "id": room["id"],
        "created_at": room["created_at"],
        "status": status,
        "agents": list(room.get("requested_agents") or []),
        "messages": messages,
        "prompt": room.get("prompt", ""),
        "workspace": "default",
        "purpose": "project",
        "timeout_seconds": room.get("timeout_seconds"),
    }
    if "effective_roster" in room:
        view["effective_roster"] = room["effective_roster"]
    if "queue_deadline" in room:
        view["queue_deadline"] = room["queue_deadline"]
    return view


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _match(status="completed", **extra):
    return {"status": status, "mode": "match", **extra}


class VerifyFleetTests(unittest.TestCase):
    def _run(self, outcomes, args, *, status=None, gate=None, poll_seconds=vf.POLL_SECONDS,
             deadline_seconds=vf.DEADLINE_SECONDS, env=None, argv=None):
        clock = Clock()
        sleeps = []

        def default_sleep(seconds):
            sleeps.append(seconds)
            clock.advance(seconds)

        stdout, stderr = StringIO(), StringIO()
        with tempfile.TemporaryDirectory() as directory, FakeHub(outcomes, status=status) as hub:
            ledger = str(Path(directory) / "ledger.json")
            command = list(argv) if argv is not None else []
            if argv is None:
                if gate:
                    command.append(gate)
                command.extend(["--hub", hub.url, "--ledger", ledger, *args])
            else:
                command = [hub.url if part == "{hub}" else ledger if part == "{ledger}" else part
                           for part in argv]
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = vf.main(command, env=env or {"HUB_MANAGER_TOKEN": MANAGER, "HUB_ID_TOKEN": IDENTITY},
                               sleep=default_sleep, clock=clock, poll_seconds=poll_seconds,
                               deadline_seconds=deadline_seconds)
            text = Path(ledger).read_text(encoding="utf-8") if Path(ledger).exists() else ""
            payload = json.loads(text) if text else None
            posts = list(hub.posts)
            gets = list(hub.gets)
            headers = list(hub.headers)
            events = list(hub.events)
        self._assert_hidden(stdout.getvalue(), stderr.getvalue(), text, posts)
        return {"code": code, "stdout": stdout.getvalue(), "stderr": stderr.getvalue(),
                "ledger": payload, "posts": posts, "gets": gets, "headers": headers,
                "sleeps": sleeps, "events": events, "ledger_text": text}

    def _assert_hidden(self, stdout, stderr, ledger_text, posts):
        combined = stdout + stderr + ledger_text
        self.assertNotIn(MANAGER, combined)
        self.assertNotIn(IDENTITY, combined)
        self.assertNotIn("Bearer ", combined)
        for post in posts:
            body = post.get("body") or {}
            prompt = body.get("prompt", "")
            if prompt:
                self.assertNotIn(prompt, combined)
            nonce = nonce_in(prompt)
            if nonce:
                self.assertNotIn(nonce, combined)
            for token in tokens_in(prompt).values():
                self.assertNotIn(token, combined)
                self.assertRegex(token, r"^[0-9a-f]{16}-[A-Z]+$")
                self.assertTrue(token.startswith(nonce + "-"))

    def _gate(self, result, name):
        gates = result["ledger"]["gates"]
        found = [item for item in gates if item["gate"] == name]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_readme_lists_gate_order(self):
        readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
        self.assertIn("capability -> roster -> fleet -> duplicate -> load -> expiry", readme)
        self.assertEqual(vf.GATE_ORDER, ("capability", "roster", "fleet", "duplicate", "load", "expiry"))

    def test_fleet_pass_uses_coordinator_token_not_worker_ok(self):
        result = self._run([_match()], ["--consecutive", "1", "--max-rooms", "1"])
        self.assertEqual(result["code"], 0)
        body = result["posts"][0]["body"]
        self.assertEqual(set(body), ROOM_FIELDS)
        self.assertEqual(body["agents"], list(vf.AGENTS))
        self.assertEqual(body["timeout_seconds"], 300)
        self.assertEqual(body["workspace"], "default")
        self.assertEqual(body["purpose"], "project")
        self.assertNotIn("queue_deadline", body)
        tokens = tokens_in(body["prompt"])
        self.assertEqual(set(tokens), set(vf.AGENTS))
        self.assertEqual(result["headers"][0]["X-Hub-Token"], MANAGER)
        self.assertEqual(result["headers"][0]["Authorization"], "Bearer " + IDENTITY)
        gate = self._gate(result, "fleet")
        self.assertTrue(gate["pass"])
        self.assertEqual(gate["result"], "pass")
        self.assertEqual(result["ledger"]["result"], "pass")
        room = gate["evidence"]["rooms"][0]
        self.assertEqual(room["room_id"], f"{1:032x}")
        self.assertEqual(room["created_at"], 1_700_000_000)
        self.assertEqual(room["room_status"], "completed")
        self.assertEqual(room["status"], "completed")
        self.assertTrue(room["pass"])
        self.assertEqual(room["exit_codes"], {agent: 0 for agent in vf.AGENTS})
        for agent in vf.AGENTS:
            record = room["agents"][agent]
            self.assertEqual(record["validation_status"], "token_matched")
            self.assertEqual(record["execution_status"], {"exit_code": 0, "delivered": True, "ok": True})
            self.assertEqual(record["message_count"], 1)
            self.assertIn(f"{agent} exit_code=0 delivered=yes validation=token_matched", result["stdout"])
        self.assertIn("consecutive=1/1", result["stdout"])
        self.assertNotIn(" | OK", result["stdout"])

    def test_worker_ok_text_with_exit_zero_fails_validation(self):
        messages = [{"agent": agent, "text": agent.upper() + " | OK", "exit_code": 0} for agent in vf.AGENTS]
        result = self._run([{"status": "completed", "messages": messages}],
                           ["--consecutive", "1", "--max-rooms", "1"])
        self.assertEqual(result["code"], 1)
        room = self._gate(result, "fleet")["evidence"]["rooms"][0]
        self.assertFalse(room["pass"])
        self.assertEqual(room["room_status"], "completed")
        for agent in vf.AGENTS:
            record = room["agents"][agent]
            self.assertTrue(record["execution_status"]["ok"])
            self.assertEqual(record["execution_status"]["exit_code"], 0)
            self.assertTrue(record["execution_status"]["delivered"])
            self.assertEqual(record["validation_status"], "wrong_token")
        self.assertEqual(result["ledger"]["result"], "fail")

    def test_wrong_token_is_execution_ok_and_validation_failed(self):
        result = self._run([{"status": "completed", "mode": "wrong", "wrong_agent": "claude"}],
                           ["--consecutive", "1", "--max-rooms", "1"])
        self.assertEqual(result["code"], 1)
        room = self._gate(result, "fleet")["evidence"]["rooms"][0]
        claude = room["agents"]["claude"]
        self.assertEqual(claude["execution_status"], {"exit_code": 0, "delivered": True, "ok": True})
        self.assertEqual(claude["validation_status"], "wrong_token")
        self.assertEqual(room["agents"]["codex"]["validation_status"], "token_matched")
        self.assertFalse(room["pass"])
        self.assertFalse(self._gate(result, "fleet")["pass"])

    def test_room_timeout_bounds_and_option(self):
        result = self._run([_match()], ["--consecutive", "1", "--max-rooms", "1", "--room-timeout", "180"])
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["posts"][0]["body"]["timeout_seconds"], 180)
        accepted = self._run([_match()], ["--consecutive", "1", "--max-rooms", "1", "--room-timeout", "120"])
        self.assertEqual(accepted["code"], 0)
        self.assertEqual(accepted["posts"][0]["body"]["timeout_seconds"], 120)
        ceiling = self._run([_match()], ["--consecutive", "1", "--max-rooms", "1", "--room-timeout", "900"])
        self.assertEqual(ceiling["code"], 0)
        self.assertEqual(ceiling["posts"][0]["body"]["timeout_seconds"], 900)
        for bad in ("90", "119", "901"):
            with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(StringIO()) as stderr:
                code = vf.main(["--hub", "http://127.0.0.1:9", "--ledger", str(Path(directory) / "l.json"),
                                "--room-timeout", bad],
                               env={"HUB_MANAGER_TOKEN": MANAGER, "HUB_ID_TOKEN": IDENTITY})
            self.assertEqual(code, 1)
            self.assertIn("120 to 900", stderr.getvalue())

    def test_stalled_room_fails_and_polls_every_ten_seconds(self):
        result = self._run([_match("stalled", queued_polls=2)], ["--consecutive", "1", "--max-rooms", "1"])
        self.assertEqual(result["code"], 1)
        self.assertEqual(result["sleeps"], [10, 10])
        self.assertEqual(result["gets"], [f"{1:032x}"] * 3)
        room = self._gate(result, "fleet")["evidence"]["rooms"][0]
        self.assertEqual(room["room_status"], "stalled")
        self.assertFalse(room["pass"])
        self.assertEqual(room["agents"]["codex"]["validation_status"], "token_matched")
        self.assertIn("room_status=stalled result=fail", result["stdout"])

    def test_blocked_on_provider_is_recorded_and_not_a_pass(self):
        result = self._run([_match("blocked_on_provider")], ["--consecutive", "1", "--max-rooms", "1"])
        self.assertEqual(result["code"], 1)
        self.assertEqual(result["sleeps"], [])
        room = self._gate(result, "fleet")["evidence"]["rooms"][0]
        self.assertEqual(room["room_status"], "blocked_on_provider")
        self.assertEqual(room["status"], "blocked_on_provider")
        self.assertFalse(room["pass"])
        self.assertTrue(all(item["validation_status"] == "token_matched" for item in room["agents"].values()))
        self.assertIn("room_status=blocked_on_provider result=fail", result["stdout"])

    def test_new_room_statuses_are_recorded(self):
        for status in ("retry_scheduled", "needs_reconciliation", "expired"):
            with self.subTest(status=status):
                result = self._run([_match(status)], ["--consecutive", "1", "--max-rooms", "1"])
                self.assertEqual(result["code"], 1)
                self.assertEqual(result["sleeps"], [])
                room = self._gate(result, "fleet")["evidence"]["rooms"][0]
                self.assertEqual(room["room_status"], status)
                self.assertFalse(room["pass"])

    def test_missing_room_fields_do_not_crash(self):
        result = self._run([{"status": None, "messages": []}], ["--consecutive", "1", "--max-rooms", "1"])
        self.assertEqual(result["code"], 1)
        room = self._gate(result, "fleet")["evidence"]["rooms"][0]
        self.assertEqual(room["room_status"], "unknown")
        self.assertFalse(room["pass"])
        for agent in vf.AGENTS:
            record = room["agents"][agent]
            self.assertEqual(record["validation_status"], "missing")
            self.assertEqual(record["execution_status"], {"exit_code": None, "delivered": False, "ok": False})
            self.assertIsNone(room["exit_codes"][agent])

    def test_consecutive_counter_resets_after_a_failed_room(self):
        outcomes = [_match(), {"status": "completed", "mode": "wrong"}, _match(), _match()]
        result = self._run(outcomes, ["--consecutive", "2", "--max-rooms", "8"])
        self.assertEqual(result["code"], 0)
        rooms = self._gate(result, "fleet")["evidence"]["rooms"]
        self.assertEqual([room["pass"] for room in rooms], [True, False, True, True])
        self.assertEqual(self._gate(result, "fleet")["evidence"]["streak"], 2)
        self.assertEqual(len(result["posts"]), 4)
        self.assertEqual(
            [line for line in result["stdout"].splitlines() if line.startswith("consecutive=")],
            ["consecutive=1/2", "consecutive=0/2", "consecutive=1/2", "consecutive=2/2"])

    def test_fleet_defaults_to_two_consecutive_passes(self):
        result = self._run([_match(), _match(), _match()], [])
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(result["posts"]), 2)
        self.assertEqual(self._gate(result, "fleet")["evidence"]["consecutive_required"], 2)
        self.assertEqual(self._gate(result, "fleet")["evidence"]["streak"], 2)

    def test_legacy_flags_without_subcommand_run_fleet(self):
        result = self._run([_match()], [], argv=["--hub", "{hub}", "--ledger", "{ledger}",
                                                 "--consecutive", "1", "--max-rooms", "1"])
        self.assertEqual(result["code"], 0)
        self.assertTrue(self._gate(result, "fleet")["pass"])

    def test_queued_room_fails_when_fifteen_minutes_elapse(self):
        self.assertEqual(vf.POLL_SECONDS, 10)
        self.assertEqual(vf.DEADLINE_SECONDS, 900)
        result = self._run([{"status": "queued", "queued_polls": 10_000, "messages": []}],
                           ["--consecutive", "1", "--max-rooms", "1"])
        self.assertEqual(result["code"], 1)
        self.assertEqual(result["sleeps"], [10] * 90)
        self.assertEqual(sum(result["sleeps"]), 900)
        room = self._gate(result, "fleet")["evidence"]["rooms"][0]
        self.assertFalse(room["pass"])
        self.assertEqual(room["room_status"], "queued")
        self.assertTrue(all(code is None for code in room["exit_codes"].values()))

    def test_hub_error_redacts_fixture_token(self):
        result = self._run([{"echo_token_error": True}], ["--consecutive", "1", "--max-rooms", "1"])
        self.assertEqual(result["code"], 1)
        self.assertIn("[redacted]", result["stderr"])
        self.assertIn("HTTP 500", result["stderr"])
        gate = self._gate(result, "fleet")
        self.assertFalse(gate["pass"])
        self.assertIn("[redacted]", gate["reason"])

    def test_capability_accepts_ready_or_restarting_and_records_manifest(self):
        status = ready_status(
            codex={"capability_manifest": {"model": "fixture-model", "text": True}},
            claude={"status": "restarting", "capabilities": ["text"]},
            copilot={"manifest": {"tools": False}},
        )
        result = self._run([], [], gate="capability", status=status)
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["posts"], [])
        self.assertEqual(result["events"], ["status"])
        gate = self._gate(result, "capability")
        self.assertTrue(gate["pass"])
        agents = gate["evidence"]["agents"]
        self.assertEqual(agents["codex"]["status"], "ready")
        self.assertEqual(agents["claude"]["status"], "restarting")
        self.assertTrue(agents["codex"]["capability_manifest_present"])
        self.assertIn("capability_manifest.model", agents["codex"]["capability_manifest_fields"])
        self.assertEqual(agents["codex"]["capability_manifest"]["capability_manifest"]["model"], "fixture-model")
        self.assertIn("text", agents["claude"]["capability_manifest_fields"])
        self.assertIn("manifest.tools", agents["copilot"]["capability_manifest_fields"])
        self.assertFalse(agents["cursor"]["capability_manifest_present"])
        self.assertEqual(agents["cursor"]["capability_manifest_fields"], [])
        self.assertEqual(gate["evidence"]["offline"], [])

    def test_capability_fails_when_any_agent_is_offline(self):
        status = ready_status(grok={"status": "offline"})
        result = self._run([], [], gate="capability", status=status)
        self.assertEqual(result["code"], 1)
        gate = self._gate(result, "capability")
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["evidence"]["offline"], ["grok"])
        self.assertEqual(gate["evidence"]["agents"]["grok"]["status"], "offline")

    def test_capability_old_hub_without_new_fields_fails_cleanly(self):
        status = {"agents": [{"id": agent, "status": "idle"} for agent in vf.AGENTS]}
        result = self._run([], [], gate="capability", status=status)
        self.assertEqual(result["code"], 1)
        gate = self._gate(result, "capability")
        self.assertFalse(gate["pass"])
        for agent in vf.AGENTS:
            info = gate["evidence"]["agents"][agent]
            self.assertEqual(info["status"], "idle")
            self.assertFalse(info["capability_manifest_present"])
            self.assertEqual(info["capability_manifest"], {})
            self.assertEqual(info["capability_manifest_fields"], [])
        self.assertEqual(gate["evidence"]["offline"], [])

    def test_capability_ready_without_manifest_still_passes(self):
        status = {"agents": {agent: {"status": "ready"} for agent in vf.AGENTS}}
        result = self._run([], [], gate="capability", status=status)
        self.assertEqual(result["code"], 0)
        gate = self._gate(result, "capability")
        self.assertTrue(gate["pass"])
        self.assertTrue(all(not info["capability_manifest_present"]
                            for info in gate["evidence"]["agents"].values()))

    def test_roster_one_agent_rooms_use_agents_when_effective_roster_is_absent(self):
        result = self._run([_match() for _ in vf.AGENTS], [], gate="roster")
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(result["posts"]), 5)
        gate = self._gate(result, "roster")
        self.assertTrue(gate["pass"])
        for agent, room, post in zip(vf.AGENTS, gate["evidence"]["rooms"], result["posts"]):
            self.assertEqual(post["body"]["agents"], [agent])
            self.assertEqual(set(tokens_in(post["body"]["prompt"])), {agent})
            self.assertFalse(room["effective_roster_present"])
            self.assertEqual(room["roster_field"], "agents")
            self.assertEqual(room["effective_roster"], [agent])
            self.assertEqual(room["message_count"], 1)
            self.assertEqual(room["validation_status"], "token_matched")
            self.assertTrue(room["execution_status"]["ok"])
            self.assertTrue(room["pass"])
            self.assertEqual(room["room_status"], "completed")

    def test_roster_requires_exactly_that_agent_as_effective_roster(self):
        outcomes = [_match(effective_roster=["codex", "claude"])] + [_match() for _ in vf.AGENTS[1:]]
        result = self._run(outcomes, [], gate="roster")
        self.assertEqual(result["code"], 1)
        rooms = self._gate(result, "roster")["evidence"]["rooms"]
        self.assertEqual(rooms[0]["roster_field"], "effective_roster")
        self.assertTrue(rooms[0]["effective_roster_present"])
        self.assertEqual(rooms[0]["effective_roster"], ["codex", "claude"])
        self.assertFalse(rooms[0]["pass"])
        self.assertEqual(len(result["posts"]), 5)

    def test_duplicate_message_fails_the_duplicate_gate(self):
        result = self._run([{"status": "completed", "mode": "duplicate", "duplicate_agent": "claude"}],
                           [], gate="duplicate")
        self.assertEqual(result["code"], 1)
        gate = self._gate(result, "duplicate")
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["evidence"]["duplicates"], ["claude"])
        room = gate["evidence"]["rooms"][0]
        claude = room["agents"]["claude"]
        self.assertEqual(claude["validation_status"], "duplicate")
        self.assertEqual(claude["message_count"], 2)
        self.assertTrue(claude["execution_status"]["delivered"])
        self.assertEqual(claude["execution_status"]["exit_code"], 0)
        self.assertTrue(claude["execution_status"]["ok"])
        self.assertEqual(room["agents"]["codex"]["message_count"], 1)
        self.assertFalse(room["pass"])

    def test_duplicate_gate_passes_when_each_agent_appears_once(self):
        result = self._run([_match()], [], gate="duplicate")
        self.assertEqual(result["code"], 0)
        gate = self._gate(result, "duplicate")
        self.assertTrue(gate["pass"])
        self.assertEqual(gate["evidence"]["duplicates"], [])

    def test_load_creates_default_three_rooms_before_polling(self):
        result = self._run([_match(), _match(), _match()], [], gate="load")
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["events"][:3], ["post", "post", "post"])
        self.assertNotIn("get", result["events"][:3])
        gate = self._gate(result, "load")
        self.assertTrue(gate["pass"])
        self.assertEqual(gate["evidence"]["requested_rooms"], 3)
        self.assertEqual(gate["evidence"]["room_timeout"], 300)
        self.assertLessEqual(gate["evidence"]["elapsed_seconds"], 300)
        self.assertTrue(gate["evidence"]["within_room_timeout"])
        self.assertEqual(len(gate["evidence"]["rooms"]), 3)
        self.assertTrue(all(room["pass"] for room in gate["evidence"]["rooms"]))

    def test_load_count_is_bounded_and_one_room_can_pass(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(StringIO()) as stderr:
            code = vf.main(["load", "--hub", "http://127.0.0.1:9", "--ledger", str(Path(directory) / "l.json"),
                            "--load-rooms", "6"],
                           env={"HUB_MANAGER_TOKEN": MANAGER, "HUB_ID_TOKEN": IDENTITY})
        self.assertEqual(code, 1)
        self.assertIn("1 to 5", stderr.getvalue())
        result = self._run([_match()], ["--load-rooms", "1"], gate="load")
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(result["posts"]), 1)
        self.assertEqual(self._gate(result, "load")["evidence"]["requested_rooms"], 1)

    def test_load_fails_when_a_room_does_not_finish_within_room_timeout(self):
        result = self._run(
            [{"status": "queued", "queued_polls": 10_000, "messages": []}],
            ["--load-rooms", "1", "--room-timeout", "120"],
            gate="load",
        )
        self.assertEqual(result["code"], 1)
        self.assertEqual(sum(result["sleeps"]), 120)
        gate = self._gate(result, "load")
        self.assertFalse(gate["pass"])
        self.assertFalse(gate["evidence"]["within_room_timeout"])
        self.assertEqual(gate["evidence"]["elapsed_seconds"], 120)
        self.assertEqual(gate["evidence"]["rooms"][0]["room_status"], "queued")

    def test_expiry_skips_when_hub_does_not_report_queue_deadline(self):
        status = ready_status()
        result = self._run([], [], gate="expiry", status=status)
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["posts"], [])
        gate = self._gate(result, "expiry")
        self.assertEqual(gate["result"], "skipped")
        self.assertIsNone(gate["pass"])
        self.assertEqual(gate["reason"], vf.SKIPPED_EXPIRY_REASON)
        self.assertFalse(gate["evidence"]["queue_deadline_supported"])
        self.assertEqual(result["ledger"]["result"], "skipped")
        self.assertIn("expiry result=skipped", result["stdout"])

    def test_expiry_passes_only_when_the_room_expires(self):
        supported = ready_status(_root={"queue_deadline_supported": True})
        expired = self._run([_match("expired", queue_deadline=1_700_000_120)], [], gate="expiry", status=supported)
        self.assertEqual(expired["code"], 0)
        gate = self._gate(expired, "expiry")
        self.assertTrue(gate["pass"])
        self.assertEqual(gate["evidence"]["support_field"], "queue_deadline_supported")
        self.assertTrue(gate["evidence"]["queue_deadline_present"])
        self.assertEqual(gate["evidence"]["queue_deadline"], 1_700_000_120)
        self.assertEqual(gate["evidence"]["rooms"][0]["room_status"], "expired")
        completed = self._run([_match("completed", queue_deadline=1_700_000_120)], [], gate="expiry",
                              status={"features": ["queue_deadline"], "agents": supported["agents"]})
        self.assertEqual(completed["code"], 1)
        failed = self._gate(completed, "expiry")
        self.assertFalse(failed["pass"])
        self.assertEqual(failed["evidence"]["support_field"], "features")
        self.assertEqual(failed["evidence"]["rooms"][0]["room_status"], "completed")

    def test_all_runs_gates_in_order_and_skips_expiry_without_new_fields(self):
        outcomes = [_match() for _ in range(5 + 2 + 1 + 3)]
        result = self._run(outcomes, [], gate="all", status=ready_status())
        self.assertEqual(result["code"], 0)
        self.assertEqual([item["gate"] for item in result["ledger"]["gates"]], list(vf.GATE_ORDER))
        self.assertEqual(result["ledger"]["result"], "pass")
        self.assertTrue(self._gate(result, "capability")["pass"])
        self.assertFalse(self._gate(result, "capability")["evidence"]["agents"]["codex"]["capability_manifest_present"])
        self.assertTrue(self._gate(result, "roster")["pass"])
        self.assertTrue(self._gate(result, "fleet")["pass"])
        self.assertEqual(self._gate(result, "fleet")["evidence"]["streak"], 2)
        self.assertTrue(self._gate(result, "duplicate")["pass"])
        self.assertTrue(self._gate(result, "load")["pass"])
        expiry = self._gate(result, "expiry")
        self.assertEqual(expiry["result"], "skipped")
        self.assertEqual(len(result["posts"]), 11)
        self.assertEqual(result["events"][0], "status")

    def test_all_stops_after_a_failing_capability_gate(self):
        result = self._run([_match()], [], gate="all", status=ready_status(codex={"status": "offline"}))
        self.assertEqual(result["code"], 1)
        self.assertEqual([item["gate"] for item in result["ledger"]["gates"]], ["capability"])
        self.assertEqual(result["posts"], [])

    def test_all_includes_expiry_when_the_hub_expires_the_room(self):
        outcomes = [_match() for _ in range(5 + 2 + 1 + 3)] + [_match("expired")]
        status = ready_status(_root={"capabilities": {"queue_deadline": True}})
        result = self._run(outcomes, [], gate="all", status=status)
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(result["posts"]), 12)
        expiry = self._gate(result, "expiry")
        self.assertTrue(expiry["pass"])
        self.assertEqual(expiry["evidence"]["support_field"], "capabilities.queue_deadline")
        self.assertEqual(expiry["evidence"]["rooms"][0]["room_status"], "expired")

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

    def test_unauthorized_hub_does_not_print_tokens(self):
        result = self._run([_match()], ["--consecutive", "1", "--max-rooms", "1"],
                           env={"HUB_MANAGER_TOKEN": "other-manager-token", "HUB_ID_TOKEN": IDENTITY})
        self.assertEqual(result["code"], 1)
        self.assertIn("HTTP 401", result["stderr"])
        self.assertNotIn("other-manager-token", result["stdout"] + result["stderr"] + result["ledger_text"])

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
                    code = vf.main(["fleet", "--hub", f"http://127.0.0.1:{server.server_address[1]}",
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

    def test_queue_deadline_detector_ignores_missing_and_false_flags(self):
        self.assertEqual(vf.queue_deadline_supported({}), (False, None))
        self.assertEqual(vf.queue_deadline_supported({"queue_deadline": False}), (False, None))
        self.assertEqual(vf.queue_deadline_supported({"queue_deadline": {"supported": False}}), (False, None))
        self.assertEqual(vf.queue_deadline_supported({"supports": ["queue_deadline"]}), (True, "supports"))


if __name__ == "__main__":
    unittest.main()
