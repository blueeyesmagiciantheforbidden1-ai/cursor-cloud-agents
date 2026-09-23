"""Adversarial protocol/concurrency tests. Identities are synthetic fixtures."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

from agent_hub.oauth_service import (
    IAP_ISSUER, OAuthClient, OAuthConfig, OAuthError, OAuthService,
    PinnedIdentity, credential_hash,
)
from agent_hub.oauth_store import MemoryOAuthStore, _FirestoreTransaction

ISSUER = "https://gateway.example.test"
RESOURCE = ISSUER + "/mcp"
CALLBACK = "https://chatgpt.com/connector/oauth/test-fixture"
FIRST = {"iss": IAP_ISSUER, "sub": "accounts.google.com:test-subject-one"}
SECOND = {"iss": IAP_ISSUER, "sub": "accounts.google.com:test-subject-two"}
THIRD = {"iss": IAP_ISSUER, "sub": "accounts.google.com:unauthorized-fixture"}
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


def config():
    return OAuthConfig(
        ISSUER, RESOURCE,
        (OAuthClient("chatgpt-test", (CALLBACK,)),
         OAuthClient("other-client", ("https://chatgpt.com/connector/oauth/other-fixture",))),
        (PinnedIdentity(**{"issuer": FIRST["iss"], "subject": FIRST["sub"]}),
         PinnedIdentity(**{"issuer": SECOND["iss"], "subject": SECOND["sub"]})),
    )


class OAuthServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000
        self.store = MemoryOAuthStore()
        self.config = config()
        self.service = OAuthService(self.config, self.store, clock=lambda: self.now)

    def auth_params(self, **changes):
        params = {
            "response_type": "code", "client_id": "chatgpt-test",
            "redirect_uri": CALLBACK, "scope": "hub:manage", "state": "state & opaque=value",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", "resource": RESOURCE,
        }
        params.update(changes)
        return params

    def code(self, identity=FIRST, **changes):
        pending = self.service.begin_authorization(self.auth_params(**changes), identity)
        redirect = self.service.approve(pending["id"], identity, pending["csrf"])
        result = parse_qs(urlsplit(redirect).query)
        self.assertEqual(result["state"], ["state & opaque=value"])
        self.assertEqual(result["iss"], [ISSUER])
        return result["code"][0]

    def form(self, code, **changes):
        params = {"grant_type": "authorization_code", "client_id": "chatgpt-test",
                  "code": code, "redirect_uri": CALLBACK, "code_verifier": VERIFIER,
                  "resource": RESOURCE}
        params.update(changes)
        return params

    def tokens(self, identity=FIRST):
        return self.service.exchange(self.form(self.code(identity)))

    def refresh_form(self, token, **changes):
        params = {"grant_type": "refresh_token", "client_id": "chatgpt-test",
                  "resource": RESOURCE, "refresh_token": token}
        params.update(changes)
        return params

    def rejects(self, error, operation):
        with self.assertRaises(OAuthError) as caught:
            operation()
        self.assertEqual(caught.exception.error, error)
        return caught.exception

    def test_full_code_flow_and_both_pinned_users(self):
        for identity in (FIRST, SECOND):
            token = self.tokens(identity)
            self.assertEqual(token["token_type"], "Bearer")
            self.assertEqual(token["scope"], "hub:manage")
            self.assertEqual(token["expires_in"], 300)
            principal = self.service.authenticate(token["access_token"])
            self.assertEqual(principal, {**identity, "client_id": "chatgpt-test",
                                        "scope": "hub:manage", "resource": RESOURCE})

    def test_chatgpt_authorization_locale_hint_completes_flow_without_entering_grant(self):
        # Same parameter shape as ChatGPT's actual authorization request; all
        # identities, state, client and PKCE material remain synthetic fixtures.
        for locales in ("en-US", "fr-CA fr en", "zh-Hant-TW", "i-klingon x-private", ""):
            with self.subTest(locales=locales):
                query = urlencode(self.auth_params(ui_locales=locales))
                pending = self.service.begin_authorization(query, FIRST)
                callback = self.service.approve(pending["id"], FIRST, pending["csrf"])
                response = parse_qs(urlsplit(callback).query)
                self.assertEqual(set(response), {"code", "state", "iss"})
                self.assertEqual(response["state"], ["state & opaque=value"])
                tokens = self.service.exchange(self.form(response["code"][0]))
                self.assertEqual(self.service.authenticate(tokens["access_token"]), {
                    **FIRST, "client_id": "chatgpt-test", "scope": "hub:manage", "resource": RESOURCE})
                self.assertNotIn("ui_locales", repr(self.store.snapshot()))

    def test_locale_hint_does_not_replace_required_parameters_or_relax_security(self):
        for key in self.auth_params():
            params = self.auth_params(ui_locales="en-US")
            del params[key]
            self.rejects("invalid_request", lambda: self.service.begin_authorization(params, FIRST))
        cases = (("invalid_request", {"client_id": "unregistered"}),
                 ("invalid_request", {"redirect_uri": "https://attacker.example/callback"}),
                 ("invalid_target", {"resource": RESOURCE + "/other"}),
                 ("invalid_request", {"code_challenge_method": "plain"}),
                 ("invalid_request", {"code_challenge": "x" * 42}),
                 ("invalid_scope", {"scope": "hub:manage admin"}),
                 ("invalid_request", {"response_mode": "fragment"}))
        for error, changes in cases:
            with self.subTest(changes=changes):
                self.rejects(error, lambda: self.service.begin_authorization(
                    self.auth_params(ui_locales="en-US", **changes), FIRST))
        self.assertFalse(any(key[0] == "consents" for key in self.store.snapshot()))

    def test_malformed_oversize_and_duplicate_locale_hints_rejected(self):
        for locales in (" en-US", "en-US ", "en  US", "en_US", "en--US", "<script>",
                        "en-US&scope=admin", "en\nUS", "en\x7f", "é-US", "a" * 257,
                        " ".join(["en"] * 9), ["en-US"], None):
            with self.subTest(locales=locales):
                self.rejects("invalid_request", lambda: self.service.begin_authorization(
                    self.auth_params(ui_locales=locales), FIRST))
        query = urlencode(self.auth_params(ui_locales="en-US"))
        for extra in ("&ui_locales=fr-CA", "&ui%5Flocales=en-US", "&state=injected"):
            self.rejects("invalid_request", lambda: self.service.begin_authorization(query + extra, FIRST))
        for value in ("%ZZ", "%FF"):
            self.rejects("invalid_request", lambda: self.service.begin_authorization(
                urlencode(self.auth_params()) + "&ui_locales=" + value, FIRST))
        pairs = list(self.auth_params(ui_locales="en-US").items()) + [("ui_locales", "fr")]
        self.rejects("invalid_request", lambda: self.service.begin_authorization(pairs, FIRST))
        self.assertFalse(any(key[0] == "consents" for key in self.store.snapshot()))

    def test_metadata_predefined_public_clients_only(self):
        metadata = self.service.metadata(ISSUER, "https://dashboard.example.test/oauth/authorize")
        self.assertEqual(metadata["token_endpoint_auth_methods_supported"], ["none"])
        self.assertEqual(metadata["code_challenge_methods_supported"], ["S256"])
        self.assertTrue(metadata["authorization_response_iss_parameter_supported"])
        self.assertNotIn("registration_endpoint", metadata)
        self.assertNotIn("client_id_metadata_document_supported", metadata)
        self.assertEqual(self.service.protected_resource_metadata()["resource"], RESOURCE)
        with self.assertRaises(ValueError):
            self.service.metadata(ISSUER + "/", "https://dashboard.example.test/oauth/authorize")

    def test_config_requires_exactly_two_distinct_iap_subjects(self):
        for identities in ((), self.config.identities[:1],
                           self.config.identities + (PinnedIdentity(IAP_ISSUER, "third"),),
                           (self.config.identities[0], self.config.identities[0])):
            with self.assertRaises(ValueError):
                replace(self.config, identities=identities)
        with self.assertRaises(ValueError):
            PinnedIdentity("https://accounts.google.com", FIRST["sub"])
        with self.assertRaises(ValueError):
            OAuthClient("bad", ("https://chatgpt.com/callback#fragment",))
        with self.assertRaises(ValueError):
            replace(self.config, issuer="https://user:secret@gateway.example.test")
        with self.assertRaises(ValueError):
            replace(self.config, issuer="http://gateway.example.test")
        with self.assertRaises(ValueError):
            replace(self.config, issuer=ISSUER + "/")

    def test_unauthorized_and_wrong_issuer_identity_rejected(self):
        for identity in (THIRD, {"iss": "https://accounts.google.com", "sub": FIRST["sub"]},
                         {"sub": FIRST["sub"]}, FIRST["sub"], None):
            self.rejects("access_denied", lambda: self.service.begin_authorization(self.auth_params(), identity))
        self.assertFalse(any(k[0] == "consents" for k in self.store.snapshot()))

    def test_missing_plain_invalid_pkce_rejected_before_consent(self):
        for changes in ({"code_challenge_method": "plain"},
                        {"code_challenge_method": ""},
                        {"code_challenge": "x" * 42}, {"code_challenge": "!" * 43}):
            self.rejects("invalid_request", lambda: self.service.begin_authorization(
                self.auth_params(**changes), FIRST))
        params = self.auth_params()
        del params["code_challenge_method"]
        self.rejects("invalid_request", lambda: self.service.begin_authorization(params, FIRST))

    def test_unknown_client_callback_substitution_and_open_redirect_rejected(self):
        self.rejects("invalid_request", lambda: self.service.begin_authorization(
            self.auth_params(client_id="unregistered"), FIRST))
        for uri in (CALLBACK + "/", CALLBACK + "?next=evil", CALLBACK + ".evil.example",
                    "https://evil.example/redirect", CALLBACK.replace("https:", "http:")):
            with self.assertRaises(OAuthError):
                self.service.begin_authorization(self.auth_params(redirect_uri=uri), FIRST)

    def test_response_type_scope_resource_and_unknown_params_rejected(self):
        cases = (("unsupported_response_type", {"response_type": "token"}),
                 ("invalid_scope", {"scope": "hub:manage admin"}),
                 ("invalid_scope", {"scope": ""}),
                 ("invalid_target", {"resource": RESOURCE + "/"}),
                 ("invalid_target", {"resource": "https://other.example/mcp"}),
                 ("invalid_request", {"state": ""}),
                 ("invalid_request", {"response_mode": "fragment"}))
        for error, changes in cases:
            self.rejects(error, lambda: self.service.begin_authorization(self.auth_params(**changes), FIRST))

    def test_duplicate_and_malformed_parameters_rejected(self):
        query = urlencode(self.auth_params())
        for extra in ("&client_id=chatgpt-test", "&resource=" + RESOURCE,
                      "&code_challenge=" + CHALLENGE, "&state=x", "&scope=hub:manage"):
            self.rejects("invalid_request", lambda: self.service.begin_authorization(query + extra, FIRST))
        for malformed in (query + "&bad", query + "&x=%ZZ", query + "&x=%FF"):
            self.rejects("invalid_request", lambda: self.service.begin_authorization(malformed, FIRST))
        self.rejects("invalid_request", lambda: self.service.begin_authorization(
            {**self.auth_params(), "state": ["a", "b"]}, FIRST))
        code = self.code()
        form = urlencode(self.form(code)) + "&code=" + code
        self.rejects("invalid_request", lambda: self.service.exchange(form))

    def test_consent_bound_to_subject_csrf_and_single_use(self):
        pending = self.service.begin_authorization(self.auth_params(), FIRST)
        self.rejects("access_denied", lambda: self.service.approve(pending["id"], SECOND, pending["csrf"]))
        self.rejects("access_denied", lambda: self.service.approve(pending["id"], FIRST, "x" * 43))
        self.service.approve(pending["id"], FIRST, pending["csrf"])
        self.rejects("access_denied", lambda: self.service.approve(pending["id"], FIRST, pending["csrf"]))

    def test_consent_and_code_expiry_without_cleanup(self):
        pending = self.service.begin_authorization(self.auth_params(), FIRST)
        self.now += self.config.consent_ttl
        self.rejects("access_denied", lambda: self.service.approve(pending["id"], FIRST, pending["csrf"]))
        code = self.code()
        self.now += self.config.code_ttl
        self.rejects("invalid_grant", lambda: self.service.exchange(self.form(code)))

    def test_wrong_pkce_client_redirect_resource_cannot_consume_code(self):
        code = self.code()
        for changes in ({"code_verifier": "z" * 64},
                        {"client_id": "other-client"},
                        {"redirect_uri": "https://evil.example/callback"},
                        {"resource": RESOURCE + "/"}):
            with self.assertRaises(OAuthError):
                self.service.exchange(self.form(code, **changes))
        for verifier in ("short", "é" * 64, "x" * 129):
            self.rejects("invalid_request", lambda: self.service.exchange(self.form(code, code_verifier=verifier)))
        tokens = self.service.exchange(self.form(code))
        self.service.authenticate(tokens["access_token"])
        self.rejects("invalid_grant", lambda: self.service.exchange(self.form(code)))

    def test_unsupported_grants_and_missing_resource(self):
        for grant in ("password", "client_credentials", "implicit", "unknown"):
            self.rejects("unsupported_grant_type", lambda: self.service.exchange({"grant_type": grant}))
        form = self.form(self.code())
        del form["resource"]
        self.rejects("invalid_request", lambda: self.service.exchange(form))

    def test_only_one_concurrent_code_redemption(self):
        code = self.code()
        barrier = threading.Barrier(10)
        def redeem(_):
            barrier.wait()
            try:
                return self.service.exchange(self.form(code))
            except OAuthError as exc:
                return exc.error
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(redeem, range(10)))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(results.count("invalid_grant"), 9)

    def test_refresh_rotation_and_replay_revokes_entire_family(self):
        original = self.tokens()
        rotated = self.service.exchange(self.refresh_form(original["refresh_token"]))
        self.assertNotEqual(original["refresh_token"], rotated["refresh_token"])
        self.service.authenticate(rotated["access_token"])
        self.rejects("invalid_grant", lambda: self.service.exchange(self.refresh_form(original["refresh_token"])))
        for token in (original, rotated):
            self.rejects("invalid_token", lambda: self.service.authenticate(token["access_token"]))
        self.rejects("invalid_grant", lambda: self.service.exchange(self.refresh_form(rotated["refresh_token"])))

    def test_refresh_race_issues_once_then_revokes_family(self):
        original = self.tokens()
        barrier = threading.Barrier(8)
        def refresh(_):
            barrier.wait()
            try:
                return self.service.exchange(self.refresh_form(original["refresh_token"]))
            except OAuthError as exc:
                return exc.error
        with ThreadPoolExecutor(max_workers=8) as executor:
            result = list(executor.map(refresh, range(8)))
        winners = [r for r in result if isinstance(r, dict)]
        self.assertEqual(len(winners), 1)
        self.rejects("invalid_token", lambda: self.service.authenticate(winners[0]["access_token"]))

    def test_wrong_client_cannot_refresh_or_revoke_family_by_replay(self):
        original = self.tokens()
        rotated = self.service.exchange(self.refresh_form(original["refresh_token"]))
        self.rejects("invalid_grant", lambda: self.service.exchange(
            self.refresh_form(original["refresh_token"], client_id="other-client")))
        self.service.authenticate(rotated["access_token"])
        self.rejects("invalid_target", lambda: self.service.exchange(
            self.refresh_form(original["refresh_token"], resource="https://evil.example")))
        self.service.authenticate(rotated["access_token"])

    def test_refresh_scope_escalation_and_family_absolute_expiry(self):
        original = self.tokens()
        self.rejects("invalid_scope", lambda: self.service.exchange(
            self.refresh_form(original["refresh_token"], scope="admin")))
        self.now += self.config.refresh_ttl - 1
        rotated = self.service.exchange(self.refresh_form(original["refresh_token"]))
        self.assertEqual(rotated["expires_in"], 1)
        self.now += 1
        self.rejects("invalid_token", lambda: self.service.authenticate(rotated["access_token"]))
        self.rejects("invalid_grant", lambda: self.service.exchange(self.refresh_form(rotated["refresh_token"])))

    def test_access_expiry_does_not_revoke_valid_refresh(self):
        original = self.tokens()
        self.now += self.config.access_ttl
        self.rejects("invalid_token", lambda: self.service.authenticate(original["access_token"]))
        rotated = self.service.exchange(self.refresh_form(original["refresh_token"]))
        self.service.authenticate(rotated["access_token"])

    def test_subject_revocation_at_every_grant_stage_and_restart(self):
        pending = self.service.begin_authorization(self.auth_params(), FIRST)
        code = self.code()
        token = self.tokens()
        other = self.tokens(SECOND)
        self.service.set_subject_enabled(FIRST, False)
        self.rejects("access_denied", lambda: self.service.begin_authorization(self.auth_params(), FIRST))
        self.rejects("access_denied", lambda: self.service.approve(pending["id"], FIRST, pending["csrf"]))
        self.rejects("invalid_grant", lambda: self.service.exchange(self.form(code)))
        self.rejects("invalid_grant", lambda: self.service.exchange(self.refresh_form(token["refresh_token"])))
        self.rejects("invalid_token", lambda: self.service.authenticate(token["access_token"]))
        restarted = OAuthService(self.config, self.store, clock=lambda: self.now)
        self.rejects("invalid_token", lambda: restarted.authenticate(token["access_token"]))
        restarted.authenticate(other["access_token"])
        self.rejects("access_denied", lambda: self.service.set_subject_enabled(THIRD, True))

    def test_reenabling_subject_does_not_resurrect_prior_grants(self):
        pending = self.service.begin_authorization(self.auth_params(), FIRST)
        code = self.code()
        token = self.tokens()
        self.service.set_subject_enabled(FIRST, False)
        self.service.set_subject_enabled(FIRST, True)
        self.rejects("access_denied", lambda: self.service.approve(pending["id"], FIRST, pending["csrf"]))
        self.rejects("invalid_grant", lambda: self.service.exchange(self.form(code)))
        self.rejects("invalid_grant", lambda: self.service.exchange(self.refresh_form(token["refresh_token"])))
        self.rejects("invalid_token", lambda: self.service.authenticate(token["access_token"]))
        fresh = self.tokens()
        self.service.authenticate(fresh["access_token"])

    def test_revocation_invalidates_family_and_unknown_token_is_silent(self):
        token = self.tokens()
        self.service.revoke("x" * 43)
        self.service.revoke(None)
        self.service.revoke(token["refresh_token"])
        self.rejects("invalid_token", lambda: self.service.authenticate(token["access_token"]))
        self.rejects("invalid_grant", lambda: self.service.exchange(self.refresh_form(token["refresh_token"])))
        token = self.tokens()
        self.service.revoke(token["access_token"])
        self.rejects("invalid_grant", lambda: self.service.exchange(self.refresh_form(token["refresh_token"])))


    def test_revocation_is_bound_to_expected_client_for_access_and_refresh(self):
        for token_name in ("access_token", "refresh_token"):
            with self.subTest(token_name=token_name):
                token = self.tokens()
                for wrong_client in ("other-client", "unregistered", ""):
                    before = self.store.snapshot()
                    self.assertIsNone(self.service.revoke(
                        token[token_name], expected_client_id=wrong_client))
                    self.assertEqual(self.store.snapshot(), before)
                    self.service.authenticate(token["access_token"])
                self.service.revoke(token[token_name], expected_client_id="chatgpt-test")
                self.rejects("invalid_token", lambda: self.service.authenticate(token["access_token"]))
                self.rejects("invalid_grant", lambda: self.service.exchange(
                    self.refresh_form(token["refresh_token"])))

    def test_persistence_has_no_raw_codes_tokens_csrf_or_client_secret(self):
        pending = self.service.begin_authorization(self.auth_params(), FIRST)
        callback = self.service.approve(pending["id"], FIRST, pending["csrf"])
        code = parse_qs(urlsplit(callback).query)["code"][0]
        tokens = self.service.exchange(self.form(code))
        stored = repr(self.store.snapshot())
        for secret in (pending["id"], pending["csrf"], code, tokens["access_token"], tokens["refresh_token"]):
            self.assertNotIn(secret, stored)

    def test_confidential_post_client_and_public_secret_injection(self):
        secret = "synthetic-high-entropy-client-secret-" + "a" * 40
        confidential = replace(self.config, clients=(OAuthClient("chatgpt-test", (CALLBACK,), credential_hash(secret)),))
        self.service = OAuthService(confidential, self.store, clock=lambda: self.now)
        code = self.code()
        self.rejects("invalid_client", lambda: self.service.exchange(self.form(code)))
        self.rejects("invalid_client", lambda: self.service.exchange(self.form(code, client_secret="wrong")))
        token = self.service.exchange(self.form(code, client_secret=secret))
        self.service.authenticate(token["access_token"])
        self.assertNotIn(secret, repr(self.store.snapshot()))
        self.service = OAuthService(self.config, self.store, clock=lambda: self.now)
        code = self.code()
        self.rejects("invalid_client", lambda: self.service.exchange(self.form(code, client_secret="injected")))

    def test_restart_retains_consent_and_tokens_and_rejects_changed_resource(self):
        pending = self.service.begin_authorization(self.auth_params(), FIRST)
        self.service = OAuthService(self.config, self.store, clock=lambda: self.now)
        callback = self.service.approve(pending["id"], FIRST, pending["csrf"])
        token = self.service.exchange(self.form(parse_qs(urlsplit(callback).query)["code"][0]))
        changed = OAuthService(replace(self.config, resource=ISSUER + "/other"), self.store, clock=lambda: self.now)
        self.rejects("invalid_token", lambda: changed.authenticate(token["access_token"]))
        restarted = OAuthService(self.config, self.store, clock=lambda: self.now)
        restarted.authenticate(token["access_token"])

    def test_insufficient_scope_and_malformed_bearer(self):
        tokens = self.tokens()
        self.rejects("insufficient_scope", lambda: self.service.authenticate(tokens["access_token"], "admin"))
        for bad in ("", None, "Bearer " + tokens["access_token"], "x" * 257):
            self.rejects("invalid_token", lambda: self.service.authenticate(bad))


class StoreTests(unittest.TestCase):
    def test_memory_transaction_rolls_back_on_exception(self):
        store = MemoryOAuthStore()
        key = "a" * 64
        def fail(tx):
            tx.put("families", key, {"revoked": True})
            raise RuntimeError("rollback")
        with self.assertRaises(RuntimeError):
            store.run(fail)
        self.assertEqual(store.snapshot(), {})

    def test_firestore_adapter_defers_all_writes_until_after_reads(self):
        events = []
        saved = {}
        class Snapshot:
            exists = True
            def __init__(self, key):
                self.key = key
            def to_dict(self):
                return {"value": self.key}
        class Reference:
            def __init__(self, key):
                self.key = key
            def get(self, *, transaction):
                events.append(("read", self.key))
                return Snapshot(self.key)
        class Collection:
            def document(self, key):
                return Reference(key)
        class Client:
            def collection(self, collection):
                self.last_collection = collection
                return Collection()
        class Transaction:
            def set(self, reference, value):
                events.append(("write", reference.key))
                saved[reference.key] = value
        client = Client()
        view = _FirestoreTransaction(client, Transaction(), "runcrew_oauth")
        a, b = "a" * 64, "b" * 64
        view.get("families", a)
        view.put("families", a, {"revoked": True})
        self.assertEqual(view.get("families", a), {"revoked": True})
        view.get("families", b)
        self.assertEqual([e[0] for e in events], ["read", "read"])
        view.flush()
        self.assertEqual([e[0] for e in events], ["read", "read", "write"])
        self.assertEqual(saved[a], {"revoked": True})
        self.assertEqual(client.last_collection, "runcrew_oauth_families")


if __name__ == "__main__":
    unittest.main()
