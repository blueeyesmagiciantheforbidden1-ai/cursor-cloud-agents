"""Signed local JWT fixtures verify the enrollment display without Google calls."""
import base64
from contextlib import contextmanager
import json
import os
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_hub.dashboard import IapVerifier, Monitor, make_server


@contextmanager
def identity_server(authorize, allowlist):
    data = {"schema_version": 1, "hub": {"deployment": "cloud", "status": "ok"}}
    with patch.dict(os.environ, {"HUB_OWNER_EMAILS_JSON": allowlist}):
        server = make_server("127.0.0.1", 0, Monitor(lambda: data), authorize=authorize)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class DashboardIdentityTests(unittest.TestCase):
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
        key = ec.generate_private_key(ec.SECP256R1())
        private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
        cls.signer = es256.ES256Signer.from_string(private, key_id="identity-fixture")
        cls.public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        cls.audience = "/projects/123/locations/us-central1/services/test-dashboard"

    def token(self, **overrides):
        claims = {"iss": "https://cloud.google.com/iap", "aud": self.audience,
                  "sub": "accounts.google.com:owner-one", "email": "one@example.com",
                  "iat": int(time.time()), "exp": int(time.time()) + 600,
                  "private_extra_claim": "never-display-this-fixture"}
        claims.update(overrides)
        return self.jwt.encode(self.signer, claims).decode()

    def verifier(self):
        verifier = IapVerifier(self.audience)
        verifier.keys = {"identity-fixture": self.public}
        verifier.expires = time.monotonic() + 100
        return verifier

    def request(self, origin, token, path="/identity", **headers):
        return Request(origin + path, headers={"X-Goog-IAP-JWT-Assertion": token, **headers})

    def test_claims_are_returned_only_after_signature_and_claim_verification(self):
        verifier = self.verifier()
        token = self.token()
        identity = verifier.identity(token)
        self.assertEqual(set(identity), {"iss", "sub", "email", "iat", "exp"})
        self.assertEqual(identity["email"], "one@example.com")
        self.assertIs(verifier(token), True)
        self.assertNotIn(token, json.dumps(identity))
        self.assertNotIn("never-display-this-fixture", json.dumps(identity))
        invalid = [self.token(**override) for override in (
            {"aud": "/wrong-service"}, {"iss": "https://attacker.example"},
            {"iat": int(time.time()) - 1000, "exp": int(time.time()) - 400},
            {"iat": int(time.time()) + 100, "exp": int(time.time()) + 600},
            {"exp": int(time.time()) + 3600}, {"email": ""}, {"sub": ""},
            {"iat": True}, {"exp": "soon"},
        )]
        parts = token.split(".")
        parts[1] = base64.urlsafe_b64encode(b'{"email":"one@example.com"}').decode().rstrip("=")
        invalid.extend([".".join(parts), "not-a-jwt", ""])
        for assertion in invalid:
            with self.subTest(kind=assertion[:10]):
                self.assertIsNone(verifier.identity(assertion))
                self.assertIs(verifier(assertion), False)

    def test_each_allowlisted_owner_sees_only_their_own_identity_without_assertion(self):
        verifier = self.verifier()
        with identity_server(verifier, '["one@example.com","two@example.com"]') as origin:
            for email, subject, other in (
                ("one@example.com", "accounts.google.com:owner-one", "two@example.com"),
                ("two@example.com", "accounts.google.com:owner-two", "one@example.com"),
            ):
                token = self.token(email=email, sub=subject)
                with urlopen(self.request(origin, token)) as response:
                    body = response.read().decode()
                    self.assertEqual(response.status, 200)
                    self.assertIn(email, body)
                    self.assertIn(subject, body)
                    self.assertNotIn(other, body)
                    self.assertNotIn(token, body)
                    self.assertNotIn("never-display-this-fixture", body)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                    self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                    self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
                    self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_third_identity_denied_even_with_forged_allowlisted_email_header(self):
        token = self.token(email="third@example.com", sub="accounts.google.com:third")
        with identity_server(self.verifier(), '["one@example.com","two@example.com"]') as origin:
            request = self.request(origin, token, **{
                "X-Goog-Authenticated-User-Email": "accounts.google.com:one@example.com"})
            with self.assertRaises(HTTPError) as raised:
                urlopen(request)
            with raised.exception as response:
                self.assertEqual(response.code, 403)
                body = response.read().decode()
                self.assertNotIn("third@example.com", body)
                self.assertNotIn(token, body)
            # Identity-display allowlisting does not change existing status access.
            with urlopen(self.request(origin, token, path="/api/status")) as response:
                self.assertEqual(response.status, 200)

    def test_forged_wrong_audience_issuer_and_expired_tokens_cannot_reach_identity_page(self):
        tokens = ["not-a-jwt", self.token(aud="/wrong"), self.token(iss="https://wrong.example"),
                  self.token(iat=int(time.time()) - 1000, exp=int(time.time()) - 400)]
        with identity_server(self.verifier(), '["one@example.com"]') as origin:
            for token in tokens:
                with self.assertRaises(HTTPError) as raised:
                    urlopen(self.request(origin, token))
                with raised.exception as response:
                    self.assertEqual(response.code, 401)
                    body = response.read().decode()
                    self.assertNotIn(token, body)
                    self.assertNotIn("one@example.com", body)

    def test_missing_or_malformed_allowlist_fails_closed_without_disabling_status(self):
        values = ["", "invalid-json", "{}", '"one@example.com"', "[]",
                  '["one@example.com",42]', '["one@example.com "]',
                  '["one@example.com","two@example.com","three@example.com"]']
        token = self.token()
        for value in values:
            with self.subTest(value=value), identity_server(self.verifier(), value) as origin:
                with self.assertRaises(HTTPError) as raised:
                    urlopen(self.request(origin, token))
                with raised.exception as response:
                    self.assertEqual(response.code, 403)
                with urlopen(self.request(origin, token, path="/api/status")) as response:
                    self.assertEqual(response.status, 200)

    def test_legacy_boolean_authorizer_and_preview_cannot_supply_verified_identity(self):
        for authorize in (None, lambda token: True):
            with identity_server(authorize, '["one@example.com"]') as origin:
                with self.assertRaises(HTTPError) as raised:
                    urlopen(self.request(origin, self.token()))
                with raised.exception as response:
                    self.assertEqual(response.code, 401)
                with urlopen(origin + "/api/status") as response:
                    self.assertEqual(response.status, 200)

    def test_verified_identity_values_are_html_escaped(self):
        email = "<script>alert(1)</script>@example.com"
        subject = 'accounts.google.com:<svg/onload="alert(1)">&'
        token = self.token(email=email, sub=subject)
        with identity_server(self.verifier(), json.dumps([email])) as origin:
            with urlopen(self.request(origin, token)) as response:
                body = response.read().decode()
                self.assertNotIn("<script>", body)
                self.assertNotIn("<svg/", body)
                self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;@example.com", body)
                self.assertIn("&lt;svg/onload=&quot;alert(1)&quot;&gt;&amp;", body)
                self.assertNotIn(token, body)


if __name__ == "__main__":
    unittest.main()
