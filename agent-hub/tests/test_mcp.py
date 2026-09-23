"""Protocol and transport checks using real stdio children and loopback HTTP."""
import io
import json
import os
from email.message import Message
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_hub.core import AGENTS, Hub
from agent_hub import mcp
from agent_hub.server import ThreadingHTTPServer, make_handler
from agent_hub.store import SQLiteStore
from agent_hub.worker import Config


def request(method, params=None, rpc_id=1):
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}


def initialize(version=mcp.PROTOCOL):
    return request("initialize", {"protocolVersion": version, "capabilities": {},
                                  "clientInfo": {"name": "test-client", "version": "1"}})


class HubFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / "mcp.sqlite3"
        self.hub = Hub(SQLiteStore(self.database))

    def tearDown(self):
        self.assertTrue(self.database.resolve().is_relative_to(self.root))
        self.temp.cleanup()

    def call_tool(self, actor, name, arguments=None):
        return mcp.handle_rpc(self.hub, actor,
                              request("tools/call", {"name": name, "arguments": arguments or {}}))


class ProtocolTests(HubFixture):
    def test_initialize_negotiates_and_validates(self):
        for version in mcp.SUPPORTED_PROTOCOLS:
            result = mcp.handle_rpc(self.hub, "manager", initialize(version))["result"]
            self.assertEqual(result["protocolVersion"], version)
            self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
        result = mcp.handle_rpc(self.hub, "manager", initialize("unsupported"))["result"]
        self.assertEqual(result["protocolVersion"], mcp.PROTOCOL)
        for params in ({}, {"protocolVersion": mcp.PROTOCOL, "capabilities": [], "clientInfo": {}}):
            reply = mcp.handle_rpc(self.hub, "manager", request("initialize", params))
            self.assertEqual(reply["error"]["code"], -32602)

    def test_tools_are_filtered_and_authorization_is_enforced(self):
        for actor, expected in (("manager", mcp.MANAGER_TOOLS), *((name, mcp.WORKER_TOOLS) for name in AGENTS)):
            tools = mcp.handle_rpc(self.hub, actor, request("tools/list"))["result"]["tools"]
            self.assertEqual({tool["name"] for tool in tools}, expected)
            self.assertTrue(all(tool["inputSchema"]["additionalProperties"] is False for tool in tools))
        for actor, tool, arguments in (("cursor", "hub_start", {"prompt": "forbidden"}),
                                       ("copilot", "hub_status", {}), ("manager", "hub_claim", {})):
            reply = self.call_tool(actor, tool, arguments)
            self.assertTrue(reply["result"]["isError"])
            self.assertNotIn("structuredContent", reply["result"])
        self.assertEqual(self.hub.list("manager"), [])

    def test_status_uses_redacted_snapshot_only_for_manager(self):
        snapshot = mock.Mock(return_value={"status": "ok", "agents": []})
        with mock.patch.dict(sys.modules, {"agent_hub.telemetry": SimpleNamespace(status_snapshot=snapshot)}):
            result = self.call_tool("manager", "hub_status")["result"]
            self.assertEqual(result["structuredContent"], {"status": "ok", "agents": []})
            snapshot.assert_called_once_with(self.hub)
            self.assertTrue(self.call_tool("claude", "hub_status")["result"]["isError"])
            snapshot.assert_called_once()

    def test_notification_cannot_start_a_room(self):
        message = request("tools/call", {"name": "hub_start", "arguments": {"prompt": "must not run"}})
        del message["id"]
        self.assertIs(mcp.handle_rpc(self.hub, "manager", message), mcp.NO_CONTENT)
        self.assertEqual(self.hub.list("manager"), [])

    def test_invalid_ids_batches_and_params_have_protocol_errors(self):
        for message in ([], [request("ping")], {}, None, {"jsonrpc": "1.0", "id": 1, "method": "ping"}):
            self.assertEqual(mcp.handle_rpc(self.hub, "manager", message)["error"]["code"], -32600)
        for invalid_id in (None, True, 1.5, "x" * 1025, "\ud800"):
            self.assertEqual(mcp.handle_rpc(self.hub, "manager", request("ping", rpc_id=invalid_id))["error"]["code"], -32600)
        for valid_id in (0, -1, "test", ""):
            self.assertEqual(mcp.handle_rpc(self.hub, "manager", request("ping", rpc_id=valid_id))["id"], valid_id)
        malformed = request("tools/list")
        malformed["params"] = []
        self.assertEqual(mcp.handle_rpc(self.hub, "manager", malformed)["error"]["code"], -32602)
        self.assertEqual(mcp.handle_rpc(self.hub, "manager", request("unknown"))["error"]["code"], -32601)

    def test_invalid_tool_arguments_do_not_mutate(self):
        examples = (("missing", {}), ("hub_start", {}), ("hub_start", {"prompt": "x", "surprise": True}),
                    ("hub_start", {"prompt": "x", "rounds": True}),
                    ("hub_start", {"prompt": "x", "agents": ["unregistered-agent"]}),
                    ("hub_get", {"room_id": "../../tokens"}), ("hub_list", []))
        for name, arguments in examples:
            with self.subTest(name=name, arguments=arguments):
                reply = mcp.handle_rpc(self.hub, "manager", request("tools/call", {"name": name, "arguments": arguments}))
                self.assertEqual(reply["error"]["code"], -32602)
        self.assertEqual(self.hub.list("manager"), [])

    def test_storage_failures_do_not_leak_diagnostics(self):
        missing = self.call_tool("manager", "hub_get", {"room_id": "0" * 32})
        self.assertTrue(missing["result"]["isError"])
        with mock.patch.object(self.hub, "list", side_effect=RuntimeError("secret credential contents")):
            result = self.call_tool("manager", "hub_list")
            self.assertEqual(result["error"], {"code": -32603, "message": "Internal error"})
            self.assertNotIn("secret", json.dumps(result))

    def test_raw_json_is_utf8_finite_and_size_bounded(self):
        for payload in (b'{bad', b'\xff', b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"n":NaN}}',
                        json.dumps(request("ping")).encode("utf-16")):
            self.assertEqual(mcp.handle_json(self.hub, "manager", payload)["error"]["code"], -32700)
        self.assertEqual(mcp.handle_json(self.hub, "manager", b" " * (mcp.MAX_INPUT_BYTES + 1))["error"]["code"], -32600)
        with mock.patch.object(self.hub, "get", return_value={"oversized": "x" * mcp.MAX_OUTPUT_BYTES}):
            result = self.call_tool("manager", "hub_get", {"room_id": "0" * 32})
            self.assertEqual(result["error"]["code"], -32001)

    def test_unicode_history_fits_wire_limit_and_lists_are_summaries(self):
        room = self.hub.create("manager", {"prompt": "Review", "rounds": 3})
        for index in range(8):
            actor = room['agents'][index % len(room['agents'])]
            task = self.hub.claim(actor)["task"]
            self.hub.complete(actor, room["id"], {"lease_token": task["lease_token"], "exit_code": 0, "output": "💡" * 4000})
        result = self.call_tool("manager", "hub_get", {"room_id": room["id"]})
        self.assertFalse(result["result"]["isError"])
        self.assertLess(len(mcp._encode(result)), mcp.MAX_OUTPUT_BYTES)
        self.assertEqual(len(result["result"]["structuredContent"]["messages"]), 8)
        summaries = self.call_tool("manager", "hub_list")["result"]["structuredContent"]["rooms"]
        self.assertNotIn("messages", summaries[0])
        self.assertNotIn("prompt", summaries[0])


class FrameTests(unittest.TestCase):
    def test_ndjson_crlf_eof_and_unicode(self):
        message = request("ping", {"text": "💡\nnext line"})
        encoded = mcp._encode(message)
        source = io.BytesIO(encoded + b"\r\n" + encoded + b"\n")
        self.assertEqual(mcp._read_stdio(source), message)
        self.assertEqual(mcp._read_stdio(source), message)
        self.assertIs(mcp._read_stdio(source), mcp.EOF)
        destination = io.BytesIO()
        mcp._write_stdio({"jsonrpc": "2.0", "id": 1, "result": {"text": "💡\nnext line"}}, destination)
        self.assertEqual(destination.getvalue().count(b"\n"), 1)
        self.assertNotIn(b"Content-Length", destination.getvalue())
        self.assertIn("💡".encode(), destination.getvalue())

    def test_oversized_unterminated_and_malformed_frames(self):
        for payload, fatal in ((b"bad\n", False), (b"\xff\n", False), (b"{}", True),
                               (b"x" * (mcp.MAX_INPUT_BYTES + 1) + b"\n", True)):
            with self.subTest(fatal=fatal, length=len(payload)):
                with self.assertRaises(mcp.FrameError) as caught:
                    mcp._read_stdio(io.BytesIO(payload))
                self.assertEqual(caught.exception.fatal, fatal)
        exact = b"{}" + b" " * (mcp.MAX_INPUT_BYTES - 2) + b"\n"
        self.assertEqual(mcp._read_stdio(io.BytesIO(exact)), {})


class HTTPAndStdioTests(HubFixture):
    def setUp(self):
        super().setUp()
        self.tokens = {name: name + "-" + "x" * 40 for name in ("manager", *AGENTS)}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.hub, self.tokens,
                                                                       allowed_mcp_origins=("https://dashboard.example",)))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def http(self, payload, *, actor="manager", headers=None, method="POST"):
        defaults = {"X-Hub-Token": self.tokens[actor], "Content-Type": "application/json"}
        defaults.update(headers or {})
        body = mcp._encode(payload) if isinstance(payload, dict) else payload
        return urlopen(Request(self.url + "/mcp", body, defaults, method=method), timeout=5)

    def run_stdio(self, payload, actor="manager"):
        environment = {**os.environ, "TEST_MCP_TOKEN": self.tokens[actor]}
        return subprocess.run([sys.executable, "-m", "agent_hub.mcp", "--url", self.url,
                               "--agent", actor, "--token-env", "TEST_MCP_TOKEN"],
                              input=payload, capture_output=True, timeout=15, env=environment,
                              cwd=Path(__file__).resolve().parents[1], shell=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_real_stdio_initialize_list_call_and_continue_after_bad_line(self):
        frames = [initialize("2025-03-26"), {"jsonrpc": "2.0", "method": "notifications/initialized"},
                  request("tools/list", rpc_id=2),
                  request("tools/call", {"name": "hub_start", "arguments": {"prompt": "Stdio 💡 review", "agents": ["codex"]}}, rpc_id=3)]
        payload = b"invalid-json\n" + b"".join(mcp._encode(frame) + b"\n" for frame in frames)
        process = self.run_stdio(payload)
        self.assertEqual(process.returncode, 0, process.stderr.decode())
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(replies), 4)
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual({item["name"] for item in replies[2]["result"]["tools"]}, mcp.MANAGER_TOOLS)
        self.assertEqual(replies[3]["result"]["structuredContent"]["prompt"], "Stdio 💡 review")
        self.assertNotIn(self.tokens["manager"].encode(), process.stdout + process.stderr)

    def test_real_stdio_worker_claim_and_complete_via_live_bridge(self):
        room = self.hub.create("manager", {"prompt": "Review", "agents": ["cursor"]})
        claimed = self.run_stdio(mcp._encode(request("tools/call", {"name": "hub_claim"})) + b"\n", actor="cursor")
        self.assertEqual(claimed.returncode, 0, claimed.stderr.decode())
        task = json.loads(claimed.stdout)["result"]["structuredContent"]["task"]
        frames = [request("tools/call", {"name": "hub_heartbeat", "arguments": {"room_id": room["id"], "lease_token": task["lease_token"]}}),
                  request("tools/call", {"name": "hub_complete", "arguments": {"room_id": room["id"], "lease_token": task["lease_token"], "output": "Review complete"}}, rpc_id=2)]
        result = self.run_stdio(b"".join(mcp._encode(frame) + b"\n" for frame in frames), actor="cursor")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(len(result.stdout.splitlines()), 2)
        self.assertEqual(self.hub.get("manager", room["id"])["status"], "completed")

    def test_real_stdio_fatal_frames_exit_after_one_bounded_error(self):
        for payload in (b"{}", b"x" * (mcp.MAX_INPUT_BYTES + 1) + b"\n"):
            process = self.run_stdio(payload)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(len(process.stdout.splitlines()), 1)
            self.assertEqual(json.loads(process.stdout)["error"]["code"], -32700)
            self.assertLess(len(process.stdout), 300)

    def test_http_notifications_parse_errors_and_version_headers(self):
        with self.http({"jsonrpc": "2.0", "method": "notifications/initialized"}) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(response.read(), b"")
        with self.http(b"invalid-json") as response:
            self.assertEqual(json.load(response)["error"]["code"], -32700)
        for version in mcp.SUPPORTED_PROTOCOLS:
            with self.http(request("ping"), headers={"MCP-Protocol-Version": version}) as response:
                self.assertEqual(json.load(response)["result"], {})
        with self.assertRaises(HTTPError) as caught:
            self.http(request("ping"), headers={"MCP-Protocol-Version": "unknown"})
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()

    def test_http_origin_identity_and_method_are_checked(self):
        for headers, status in (({"Origin": "https://attacker.example"}, 403),
                                ({"X-Hub-Token": "wrong"}, 401),
                                ({"X-Hub-Agent": "claude"}, 403)):
            with self.assertRaises(HTTPError) as caught:
                self.http(request("ping"), headers=headers)
            self.assertEqual(caught.exception.code, status)
            caught.exception.close()
        with self.http(request("ping"), headers={"Origin": "https://dashboard.example"}) as response:
            self.assertEqual(json.load(response)["result"], {})
        with self.assertRaises(HTTPError) as caught:
            self.http(None, method="GET")
        self.assertEqual(caught.exception.code, 405)
        caught.exception.close()


class Response(io.BytesIO):
    def __init__(self, value, *, content_type="application/json", status=200):
        super().__init__(value if isinstance(value, bytes) else mcp._encode(value))
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.status = status


class ClientTests(unittest.TestCase):
    def config(self, **updates):
        values = {"hub_url": "https://hub.example", "agent_id": "manager", "token": "t" * 40,
                  "token_env": "TEST_TOKEN", "workspaces": {}}
        values.update(updates)
        return Config(**values)

    def test_cloud_identity_refreshes_once_and_is_cached(self):
        for mode, provider in (("gcloud", "_gcloud_identity"), ("metadata", "_metadata_identity")):
            with self.subTest(mode=mode):
                client = mcp.MCPClient(self.config(cloud_run_auth_mode=mode))
                client.opener = mock.Mock()
                client.opener.open.side_effect = [HTTPError(client.config.hub_url, 401, "Unauthorized", {}, io.BytesIO(b"private")),
                                                 Response({"jsonrpc": "2.0", "id": 1, "result": {}}),
                                                 Response({"jsonrpc": "2.0", "id": 1, "result": {}})]
                with mock.patch.object(client, provider, side_effect=["expired-google-id", "fresh-google-id"]) as identity:
                    self.assertEqual(client.rpc(request("ping"))["result"], {})
                    self.assertEqual(client.rpc(request("ping"))["result"], {})
                    self.assertEqual(identity.call_count, 2)
                headers = [call.args[0].headers for call in client.opener.open.call_args_list]
                self.assertEqual([item["Authorization"] for item in headers],
                                 ["Bearer expired-google-id", "Bearer fresh-google-id", "Bearer fresh-google-id"])
                self.assertTrue(all(item["X-hub-token"] == "t" * 40 for item in headers))

    def test_second_auth_failure_is_bounded_and_sanitized(self):
        client = mcp.MCPClient(self.config(cloud_run_auth_mode="gcloud"))
        client.opener = mock.Mock()
        client.opener.open.side_effect = [HTTPError(client.config.hub_url, 403, "secret provider message", {}, io.BytesIO(b"secret body")) for _ in range(2)]
        with mock.patch.object(client, "_gcloud_identity", return_value="google-token"):
            reply = client.rpc(request("ping"))
        self.assertEqual(client.opener.open.call_count, 2)
        self.assertEqual(reply["error"]["code"], -32002)
        self.assertNotIn("secret", json.dumps(reply))
        self.assertNotIn("google-token", json.dumps(reply))

    def test_no_cloud_identity_without_explicit_mode(self):
        client = mcp.MCPClient(self.config())
        client.opener = mock.Mock()
        client.opener.open.return_value = Response({"jsonrpc": "2.0", "id": 1, "result": {}})
        with mock.patch.object(client, "_identity", side_effect=AssertionError("Unexpected identity lookup")):
            self.assertEqual(client.rpc(request("ping"))["result"], {})
        self.assertNotIn("Authorization", client.opener.open.call_args.args[0].headers)

    def test_invalid_and_oversized_remote_replies_fail_closed(self):
        responses = [Response(b"not-json"), Response({"jsonrpc": "2.0", "id": True, "result": {}}),
                     Response({"jsonrpc": "2.0", "id": 1, "result": [], "error": {}}),
                     Response({"jsonrpc": "2.0", "id": 1, "error": {"code": True, "message": "invalid"}}),
                     Response({}, content_type="text/html"), Response({}, status=202),
                     Response(b"x" * (mcp.MAX_OUTPUT_BYTES + 1))]
        client = mcp.MCPClient(self.config())
        client.opener = mock.Mock()
        for response in responses:
            client.opener.open.return_value = response
            reply = client.rpc(request("ping"))
            self.assertIn(reply["error"]["code"], (-32001, -32002))

    def test_origin_token_and_principal_validation(self):
        for url in ("http://remote.example", "https://user:password@example.com", "https://hub.example/path",
                    "https://hub.example?token=x", "https://hub.example#fragment"):
            with self.assertRaises(ValueError):
                mcp.MCPClient(self.config(hub_url=url))
        for updates in ({"agent_id": "unknown"}, {"token": "small"}, {"token": "x" * 32 + "\n"},
                        {"hub_url": "http://127.0.0.1:8080", "cloud_run_auth_mode": "metadata"},
                        {"hub_url": "http://127.0.0.1:8080", "cloud_run_auth": True}):
            with self.assertRaises(ValueError):
                mcp.MCPClient(self.config(**updates))


if __name__ == "__main__":
    unittest.main()
