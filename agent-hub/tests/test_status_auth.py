"""Live loopback checks for the dashboard's optional read-only principal."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_hub.core import AGENTS, Hub
from agent_hub.server import ThreadingHTTPServer, load_tokens, make_handler
from agent_hub.store import SQLiteStore


def tokens():
    return {name: name + "-" + "x" * 40 for name in ("manager", *AGENTS, "status")}


class StatusTokenTests(unittest.TestCase):
    def test_status_is_an_optional_unique_sixth_principal(self):
        configured = tokens()
        self.assertEqual(load_tokens(json.dumps(configured)), configured)
        legacy = {key: value for key, value in configured.items() if key != "status"}
        self.assertEqual(load_tokens(json.dumps(legacy)), legacy)
        for invalid in ({**configured, "status": configured["manager"]},
                        {**configured, "status": configured["cursor"]},
                        {**configured, "status": "short"},
                        {**configured, "status": "x" * 40 + " "},
                        {**configured, "other": "other-" + "x" * 40},
                        {key: value for key, value in configured.items() if key != "manager"}):
            with self.assertRaises(ValueError):
                load_tokens(json.dumps(invalid))


class StatusHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-status-auth-")
        self.root = Path(self.temp.name).resolve()
        self.hub = Hub(SQLiteStore(self.root / "hub.sqlite3"))
        self.tokens = tokens()
        self.room = self.hub.create("manager", {"prompt": "Private task contents must stay private"})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.hub, self.tokens))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertEqual(Path(self.temp.name).resolve(), self.root)
        self.temp.cleanup()

    def call(self, path, data=None, *, actor="status", claimed=None, method=None):
        headers = {"Content-Type": "application/json"}
        if actor is not None:
            headers["X-Hub-Token"] = self.tokens[actor]
        if claimed is not None:
            headers["X-Hub-Agent"] = claimed
        body = json.dumps(data).encode() if data is not None else None
        request = Request(self.base + path, body, headers, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            with response:
                return response.code, json.load(response)

    def test_status_snapshot_succeeds_without_task_text_or_credentials(self):
        lease = self.hub.claim("codex")["task"]
        status, snapshot = self.call("/v1/status", claimed="status")
        self.assertEqual(status, 200)
        self.assertIn("agents", snapshot)
        self.assertIn("rooms", snapshot)
        serialized = json.dumps(snapshot)
        self.assertNotIn(self.room["prompt"], serialized)
        self.assertNotIn(lease["lease_token"], serialized)
        for token in self.tokens.values():
            self.assertNotIn(token, serialized)
        self.assertEqual(self.call("/v1/status", actor="manager")[0], 200)
        self.assertEqual(self.call("/v1/status", actor=None)[0], 401)
        self.assertEqual(self.call("/v1/status", actor="codex")[0], 403)

    def test_status_token_cannot_read_or_mutate_rooms_and_tasks(self):
        room_id = self.room["id"]
        cases = (("/v1/rooms", None), (f"/v1/rooms/{room_id}", None),
                 ("/v1/rooms", {"prompt": "Unauthorized task", "actor": "manager"}),
                 (f"/v1/rooms/{room_id}/cancel", {}), (f"/v1/rooms/{room_id}/retry", {}),
                 ("/v1/tasks/claim", {}), ("/v1/workers/report", {"agent_id": "codex", "status": "idle"}),
                 ("/v1/status", {}), ("/future-admin-route", {}))
        for path, data in cases:
            with self.subTest(path=path, method="GET" if data is None else "POST"):
                self.assertEqual(self.call(path, data)[0], 403)
        current = self.hub.get("manager", room_id)
        self.assertEqual((current["status"], current["attempts"], current["messages"]), ("queued", 0, []))
        self.assertEqual(len(self.hub.list("manager")), 1)
        self.assertTrue(all(agent["status"] == "unconfigured" for agent in self.call("/v1/status")[1]["agents"]))

    def test_status_token_cannot_use_a_valid_agent_lease(self):
        task = self.hub.claim("codex")["task"]
        room_id = self.room["id"]
        heartbeat = {"lease_token": task["lease_token"]}
        complete = {**heartbeat, "output": "Unauthorized completion", "exit_code": 0}
        self.assertEqual(self.call(f"/v1/tasks/{room_id}/heartbeat", heartbeat)[0], 403)
        self.assertEqual(self.call(f"/v1/tasks/{room_id}/complete", complete)[0], 403)
        current = self.hub.get("manager", room_id)
        self.assertEqual((current["status"], current["step"], current["messages"]), ("running", 0, []))

    def test_status_token_cannot_spoof_manager_or_workers(self):
        for claimed in ("manager", *AGENTS):
            with self.subTest(claimed=claimed):
                self.assertEqual(self.call("/v1/status", claimed=claimed)[0], 403)
                self.assertEqual(self.call("/v1/rooms", {"prompt": "Unauthorized"}, claimed=claimed)[0], 403)
        self.assertEqual(self.call("/v1/status", claimed="status")[0], 200)

    def test_early_denials_drain_bounded_post_body_before_closing(self):
        body = json.dumps({"prompt": "x" * 16_000}).encode()
        # Exercise both actor() identity rejection and the status-only route
        # guard. A split write reproduces unread-body timing deterministically.
        for claimed in ("manager", "status"):
            with self.subTest(claimed=claimed), socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3) as client:
                headers = (f"POST /v1/rooms HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                           f"X-Hub-Token: {self.tokens['status']}\r\nX-Hub-Agent: {claimed}\r\n"
                           f"Content-Length: {len(body)}\r\nContent-Type: application/json\r\n\r\n").encode()
                client.sendall(headers + body[:1])
                client.settimeout(0.1)
                with self.assertRaises(socket.timeout):
                    client.recv(1)
                client.settimeout(3)
                client.sendall(body[1:])
                response = bytearray()
                while chunk := client.recv(4096):
                    response.extend(chunk)
                head, payload = bytes(response).split(b"\r\n\r\n", 1)
                self.assertTrue(head.startswith(b"HTTP/1.0 403"))
                self.assertIn("error", json.loads(payload))
        self.assertEqual(len(self.hub.list("manager")), 1)

    def test_oversized_unauthorized_body_is_not_drained(self):
        # No huge body is sent: waiting for it would violate the bounded drain.
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=1) as client:
            request = (f"POST /v1/rooms HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                       f"X-Hub-Token: {self.tokens['status']}\r\nContent-Length: 100001\r\n\r\n").encode()
            client.sendall(request)
            response = bytearray()
            while chunk := client.recv(4096):
                response.extend(chunk)
            self.assertTrue(response.startswith(b"HTTP/1.0 403"))

    def test_status_principal_is_excluded_from_all_mcp_dispatch(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "dashboard", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]
        for tool in ("hub_status", "hub_start", "hub_list", "hub_get", "hub_claim", "hub_heartbeat",
                     "hub_complete", "hub_cancel", "hub_retry"):
            messages.append({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": tool, "arguments": {"room_id": self.room["id"], "prompt": "Unauthorized"}}})
        for message in messages:
            with self.subTest(method=message["method"], tool=message.get("params", {}).get("name")):
                status, result = self.call("/mcp", message)
                self.assertEqual(status, 403)
                self.assertNotIn("jsonrpc", result, "Request must be rejected before MCP dispatch")
        self.assertEqual(self.call("/mcp", method="GET")[0], 403)


if __name__ == "__main__":
    unittest.main()
