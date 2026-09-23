import base64
from contextlib import contextmanager
from copy import deepcopy
import io
import json
import os
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent_hub.dashboard import (
    IapVerifier, MAX_RESPONSE, Monitor, StatusError, StatusGateway,
    main, make_server, public_snapshot, validated_origin,
)


def snapshot():
    return {"schema_version": 1, "generated_at": "2026-09-20T12:00:00Z",
            "hub": {"status": "ok", "service": "agent-hub", "deployment": "cloud", "store": "firestore"},
            "agents": [{"id": "codex", "configured": True, "status": "idle", "auth_status": "unknown"}],
            "alerts": [], "usage": [{"provider": "codex", "scope": "account", "metric": "credits",
                                      "used": None, "limit": None, "remaining": None, "status": "unavailable"}],
            "rooms": [], "queue": {"counts": {"queued": 0}, "observed_rooms": 0, "window": "latest 50 rooms"}}


@contextmanager
def running_server(monitor, authorize=None):
    server = make_server("127.0.0.1", 0, monitor, authorize=authorize)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class DashboardTests(unittest.TestCase):
    def test_display_projection_removes_prompt_result_and_credentials(self):
        value = snapshot()
        value["credential"] = "TOP_SECRET"
        value["hub"]["token"] = "TOP_SECRET"
        value["agents"][0]["worker_config"] = {"token": "TOP_SECRET"}
        value["rooms"] = [{"id": "room1", "prompt": "TOP_SECRET", "messages": [{"result": "TOP_SECRET"}]}]
        result = public_snapshot(value)
        self.assertNotIn("TOP_SECRET", json.dumps(result))
        self.assertEqual(result["rooms"], [{"id": "room1"}])
        self.assertIsNone(result["usage"][0]["remaining"])

    def test_projection_bounds_lists_and_drops_nonfinite(self):
        value = snapshot()
        value["usage"] = [{"used": float("nan"), "limit": float("inf"), "source": "x" * 2000}] * 1000
        result = public_snapshot(value)
        self.assertEqual(len(result["usage"]), 100)
        self.assertIsNone(result["usage"][0]["used"])
        self.assertIsNone(result["usage"][0]["limit"])
        self.assertEqual(len(result["usage"][0]["source"]), 1000)

    def test_cached_response_isolated_from_callers(self):
        calls = []
        monitor = Monitor(lambda: calls.append(True) or snapshot(), clock=lambda: 0)
        first = monitor.snapshot()
        first["agents"][0]["status"] = "tampered"
        self.assertEqual(monitor.snapshot()["agents"][0]["status"], "idle")
        self.assertEqual(len(calls), 1)

    def test_failure_retains_last_snapshot_with_explicit_stale_marker(self):
        calls = []
        def fetch():
            calls.append(True)
            if len(calls) > 1:
                raise StatusError("auth_failed")
            return snapshot()
        monitor = Monitor(fetch, cache_seconds=0)
        good = monitor.snapshot()
        bad = monitor.snapshot()
        self.assertTrue(good["dashboard"]["hub_reachable"])
        self.assertFalse(bad["dashboard"]["hub_reachable"])
        self.assertTrue(bad["dashboard"]["cached"])
        self.assertEqual(bad["dashboard"]["error"], "auth_failed")
        self.assertEqual(good["generated_at"], bad["generated_at"])

    def test_initial_failure_never_invents_a_healthy_hub(self):
        def fetch():
            raise RuntimeError("Do not display secret exception details")
        result = Monitor(fetch).snapshot()
        self.assertFalse(result["dashboard"]["hub_reachable"])
        self.assertFalse(result["dashboard"]["cached"])
        self.assertEqual(result["hub"], {})
        self.assertEqual(result["agents"], [])
        self.assertNotIn("secret", json.dumps(result))

    def test_personal_machine_fields_are_never_projected_as_cloud_data(self):
        value = snapshot()
        for name in ("inventory", "activity", "milestones"):
            value[name] = [{"label": "PERSONAL_PC_OBSERVATION", "title": "PERSONAL_PC_OBSERVATION"}]
        value["machine"] = {"platform": "PERSONAL_PC_OBSERVATION", "ram_percent": 88}
        value["collector"] = {"source": "local-machine", "status": "ok"}
        result = public_snapshot(value)
        for name in ("inventory", "activity", "milestones", "machine", "collector"):
            self.assertNotIn(name, result)
        self.assertNotIn("PERSONAL_PC_OBSERVATION", json.dumps(result))

    def test_local_hub_is_not_accepted_as_cloud_telemetry(self):
        local = snapshot()
        local["hub"]["deployment"] = "local"
        result = Monitor(lambda: local).snapshot()
        self.assertFalse(result["dashboard"]["hub_reachable"])
        self.assertEqual(result["dashboard"]["error"], "not_cloud")
        self.assertEqual(result["cloud"]["hub_connection"], "not_connected")
        self.assertEqual(result["agents"], [])
        self.assertEqual(result["usage"], [])
        self.assertEqual(result["rooms"], [])

    def test_legacy_local_enrichment_is_not_executed(self):
        from unittest.mock import Mock
        collector = Mock(side_effect=AssertionError("Do not inspect personal PC"))
        result = Monitor(snapshot, enrich=collector).snapshot()
        collector.assert_not_called()
        self.assertEqual(result["cloud"]["hub_connection"], "connected")

    def test_absent_cloud_metrics_remain_unknown_even_when_hub_is_connected(self):
        result = Monitor(snapshot).snapshot()
        self.assertEqual(result["cloud"]["metrics_status"], "not_connected")
        for field in ("cpu_percent", "memory_percent", "request_count", "instance_count"):
            self.assertIsNone(result["cloud"][field])

    def test_cloud_run_dashboard_runtime_does_not_claim_cloud_hub_connection(self):
        def missing():
            raise StatusError("unconfigured")
        with patch.dict(os.environ, {"K_SERVICE": "agent-dashboard"}, clear=True):
            result = Monitor(missing).snapshot()
        self.assertEqual(result["cloud"]["dashboard_runtime"], "cloud_run")
        self.assertEqual(result["cloud"]["hub_connection"], "not_connected")

    def test_origin_and_mode_restrictions(self):
        self.assertEqual(validated_origin("http://127.0.0.1:8080/"), "http://127.0.0.1:8080")
        for origin in ("http://remote.example", "https://a.example/path", "https://x:y@a.example", "https://a.example?token=x", "file:///etc/passwd"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                validated_origin(origin)
        with self.assertRaises(ValueError):
            StatusGateway("http://localhost:8080", "a" * 32, "metadata")

    def test_http_assets_and_status_are_authenticated_without_header_leak(self):
        monitor = Monitor(snapshot)
        with running_server(monitor, lambda token: token == "valid-test-assertion") as origin:
            with urlopen(origin + "/healthz") as response:
                self.assertEqual(response.status, 200)
            for path in ("/", "/app.js", "/app.css", "/api/status"):
                with self.subTest(path=path):
                    with self.assertRaises(HTTPError) as cm:
                        urlopen(origin + path)
                    self.assertEqual(cm.exception.code, 401)
                    cm.exception.close()
                    request = Request(origin + path, headers={"X-Goog-IAP-JWT-Assertion": "valid-test-assertion"})
                    with urlopen(request) as response:
                        body = response.read()
                        self.assertEqual(response.status, 200)
                        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                        self.assertEqual(response.headers["Cache-Control"], "no-store")
                        self.assertNotIn(b"valid-test-assertion", body)
            request = Request(origin + "/api/status", headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:someone@example.com"})
            with self.assertRaises(HTTPError) as cm:
                urlopen(request)
            self.assertEqual(cm.exception.code, 401)
            cm.exception.close()

    def test_dashboard_has_no_mutation_or_arbitrary_file_routes(self):
        with running_server(Monitor(snapshot)) as origin:
            for path, method, status in (("/../dashboard.py", "GET", 404), ("/api/start", "POST", 405), ("/api/status", "DELETE", 405)):
                with self.subTest(path=path), self.assertRaises(HTTPError) as cm:
                    urlopen(Request(origin + path, method=method))
                self.assertEqual(cm.exception.code, status)
                cm.exception.close()

    def test_cloud_run_refuses_missing_iap_or_monitoring_configuration(self):
        with patch.dict(os.environ, {"K_SERVICE": "test-dashboard"}, clear=True), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                main([])
        env = {"K_SERVICE": "test-dashboard", "DASHBOARD_IAP_AUDIENCE": "/projects/123/locations/us-central1/services/test-dashboard"}
        with patch.dict(os.environ, env, clear=True), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                main([])


class GatewayTests(unittest.TestCase):
    def gateway(self):
        return StatusGateway("https://private-hub.example", "s" * 32, principal="status")

    def test_gateway_uses_only_fixed_read_path_and_monitor_identity(self):
        requests = []
        gateway = self.gateway()
        def opened(request, timeout):
            requests.append(request)
            return io.BytesIO(json.dumps(snapshot()).encode())
        gateway.client.opener.open = opened
        self.assertEqual(gateway()["schema_version"], 1)
        self.assertEqual(requests[0].full_url, "https://private-hub.example/v1/status")
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[0].get_header("X-hub-agent"), "status")

    def test_gateway_rejects_large_or_wrong_schema_response(self):
        for content in (b"x" * (MAX_RESPONSE + 1), b'{"schema_version":2}', b'[]', b'not-json'):
            with self.subTest(length=len(content)):
                gateway = self.gateway()
                gateway.client.opener.open = lambda *a, **k: io.BytesIO(content)
                with self.assertRaises(StatusError) as cm:
                    gateway()
                self.assertEqual(cm.exception.kind, "invalid_response")

    def test_gateway_classifies_auth_and_network_errors_without_details(self):
        for error, kind in ((HTTPError("https://private-hub.example", 403, "secret", {}, None), "auth_failed"),
                            (URLError("secret"), "unreachable")):
            gateway = self.gateway()
            def opened(*args, **kwargs):
                raise error
            gateway.client.opener.open = opened
            with self.assertRaises(StatusError) as cm:
                gateway()
            self.assertEqual(str(cm.exception), kind)

    def test_google_identity_refreshes_once_on_unauthorized(self):
        gateway = StatusGateway("https://private-hub.example", "s" * 32, "gcloud", "status")
        identities = []
        def identity():
            identities.append(True)
            return "identity-test-token"
        gateway.client._identity = identity
        calls = []
        def opened(request, **kwargs):
            calls.append(request)
            if len(calls) == 1:
                raise HTTPError(request.full_url, 401, "unauthorized", {}, None)
            return io.BytesIO(json.dumps(snapshot()).encode())
        gateway.client.opener.open = opened
        gateway()
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(identities), 2)
        self.assertEqual(calls[1].get_header("Authorization"), "Bearer identity-test-token")


class SignedIapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives import serialization
            from google.auth import jwt
            from google.auth.crypt import es256
        except ImportError as exc:
            raise unittest.SkipTest("Install project requirements for signed IAP tests") from exc
        cls.jwt = jwt
        cls.key = ec.generate_private_key(ec.SECP256R1())
        private_pem = cls.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        cls.signer = es256.ES256Signer.from_string(private_pem, key_id="test-key")
        cls.public_pem = cls.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        cls.audience = "/projects/123/locations/us-central1/services/test-dashboard"

    def token(self, **overrides):
        claims = {"iss": "https://cloud.google.com/iap", "aud": self.audience,
                  "sub": "accounts.google.com:test", "email": "operator@example.com", "iat": int(time.time()), "exp": int(time.time()) + 600}
        claims.update(overrides)
        return self.jwt.encode(self.signer, claims).decode()

    def verifier(self):
        verifier = IapVerifier(self.audience)
        verifier.keys = {"test-key": self.public_pem}
        verifier.expires = time.monotonic() + 100
        return verifier

    def test_valid_signed_assertion_and_claim_rejection(self):
        verifier = self.verifier()
        self.assertTrue(verifier(self.token()))
        for override in ({"aud": "/wrong"}, {"iss": "https://example.com"}, {"email": ""}, {"sub": ""},
                         {"iat": int(time.time()) - 1000, "exp": int(time.time()) - 400}, {"exp": int(time.time()) + 3600}):
            with self.subTest(override=override):
                self.assertFalse(verifier(self.token(**override)))

    def test_unsigned_or_tampered_assertion_is_rejected(self):
        verifier = self.verifier()
        token = self.token()
        parts = token.split(".")
        parts[1] = base64.urlsafe_b64encode(b'{"email":"attacker@example.com"}').decode().rstrip("=")
        self.assertFalse(verifier(".".join(parts)))
        self.assertFalse(verifier("not-a-jwt"))
        self.assertFalse(verifier(""))


if __name__ == "__main__":
    unittest.main()
