"""Read-only CLOUD monitoring website; run with ``python -m agent_hub.dashboard``.

Browser clients never receive the hub credential. Production requires direct
Cloud Run IAP plus independently verified signed IAP headers.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from html import escape
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request, build_opener

from .worker import Config, HubClient, NoRedirect, WorkerError

WEB_ROOT = Path(__file__).with_name("web")
MAX_RESPONSE = 512_000
AGENTS = ("codex", "claude", "cursor", "copilot", "grok")
IDENTITY_CSP = "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
CONSENT_CSP = "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
CONSENT_COOKIE = "__Host-myhero-consent"
MAX_CONSENT_INPUT = 8192


def _consent_parameters(encoded):
    if len(encoded.encode('utf-8')) > MAX_CONSENT_INPUT or re.search(r'%(?![0-9a-fA-F]{2})', encoded):
        raise ValueError('Invalid consent parameters')
    pairs = parse_qsl(encoded, keep_blank_values=True, strict_parsing=True,
                      encoding='utf-8', errors='strict', max_num_fields=20)
    result = {}
    for key, value in pairs:
        if (key in result or not 1 <= len(key) <= 64 or len(value) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in key + value)):
            raise ValueError('Invalid consent parameters')
        result[key] = value
    return result


def _consent_origin():
    value = os.environ.get('HUB_OAUTH_AUTHORIZATION_ORIGIN', '')
    parsed = urlsplit(value)
    if (not value or value != validated_origin(value) or parsed.scheme != 'https'
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
        raise ValueError('HUB_OAUTH_AUTHORIZATION_ORIGIN must be a canonical HTTPS origin')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('HUB_OAUTH_AUTHORIZATION_ORIGIN has an invalid port')
    return value


def _consent_page_csp(consent, oauth_service):
    """Permit the registered callback after the same-origin form's redirect.

    Chromium checks the initiating document's form-action on redirects. CSP
    ignores callback paths after a redirect, so the core's exact URI check is
    still the security boundary. Never turn raw query input into CSP sources.
    """
    callback = consent['redirect_uri']
    if not any(client.client_id == consent['client_id'] and callback in client.redirect_uris
               for client in oauth_service.config.clients):
        raise ValueError('Consent callback is not registered for this client')
    # A single canonical HTTPS CSP source: no directive delimiters, whitespace,
    # wildcard host/port, credentials, query, fragment, or header injection.
    if not re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~!$&()+,=:@%/-]*)?', callback):
        raise ValueError('Consent callback cannot be represented as a CSP source')
    parsed = urlsplit(callback)
    if not parsed.hostname or parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('Invalid consent callback')
    return CONSENT_CSP + ' ' + callback


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StatusError(RuntimeError):
    def __init__(self, kind):
        self.kind = kind
        super().__init__(kind)


def validated_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise ValueError("HUB_URL must be an origin URL")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Remote hubs require HTTPS")
    return value.rstrip("/")


class StatusGateway:
    """Fixed-path GET adapter reusing the worker's Cloud Run identity provider."""
    def __init__(self, origin, token, auth_mode=None, principal="manager"):
        origin = validated_origin(origin)
        if auth_mode not in (None, "metadata", "gcloud"):
            raise ValueError("HUB_CLOUD_RUN_AUTH_MODE must be metadata or gcloud")
        if auth_mode == "metadata" and not origin.startswith("https://"):
            raise ValueError("Metadata identity requires HTTPS")
        if token and (len(token) < 32 or len(token) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise ValueError("Hub monitoring token must be 32..256 printable characters")
        if principal not in ("manager", "status"):
            raise ValueError("Monitoring principal must be manager or status")
        self.client = HubClient(Config(origin, principal, token, "HUB_STATUS_TOKEN", {},
                                       cloud_run_auth_mode=auth_mode))

    def __call__(self):
        client = self.client
        if not client.config.token:
            raise StatusError("unconfigured")
        google_auth = client.config.cloud_run_auth_mode is not None
        for attempt in range(2):
            headers = {"Accept": "application/json", "X-Hub-Token": client.config.token,
                       "X-Hub-Agent": client.config.agent_id}
            if google_auth:
                try:
                    headers["Authorization"] = "Bearer " + client._identity()
                except WorkerError as exc:
                    raise StatusError("auth_failed") from exc
            request = Request(client.config.hub_url + "/v1/status", headers=headers, method="GET")
            try:
                with client.opener.open(request, timeout=10) as response:
                    encoded = response.read(MAX_RESPONSE + 1)
                break
            except HTTPError as exc:
                code = exc.code
                exc.close()
                if google_auth and code in (401, 403) and attempt == 0:
                    client.identity_token = None
                    client.identity_expires = 0
                    continue
                raise StatusError("auth_failed" if code in (401, 403) else "hub_error") from exc
            except (URLError, OSError) as exc:
                raise StatusError("unreachable") from exc
        if len(encoded) > MAX_RESPONSE:
            raise StatusError("invalid_response")
        try:
            result = json.loads(encoded)
            if not isinstance(result, dict) or result.get("schema_version") != 1:
                raise ValueError()
        except (ValueError, UnicodeError) as exc:
            raise StatusError("invalid_response") from exc
        return result


def _safe(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:1000]
    return None


def _fields(value, keys):
    return {key: _safe(value[key]) for key in keys if key in value} if isinstance(value, dict) else {}


def public_snapshot(data):
    """Cloud hub display fields only; exclude personal-machine enrichment."""
    result = {"schema_version": 1, "generated_at": _safe(data.get("generated_at")),
              "hub": _fields(data.get("hub"), ("status", "service", "store", "deployment"))}
    definitions = {
        "agents": ("id", "configured", "status", "last_seen", "current_room_id", "error", "auth_status", "last_exit_code"),
        "usage": ("provider", "scope", "metric", "label", "used", "limit", "remaining", "unit", "source", "observed_at", "status", "error", "window_minutes", "resets_at"),
        "alerts": ("id", "severity", "kind", "title", "message", "agent_id", "observed_at"),
        "rooms": ("id", "room_id", "status", "created_at", "updated_at", "next_agent", "current_agent", "completed_steps", "total_steps", "rounds"),
    }
    for name, keys in definitions.items():
        value = data.get(name, [])
        result[name] = [_fields(item, keys) for item in value[:100]] if isinstance(value, list) else []
    queue = data.get("queue", {})
    result["queue"] = _fields(queue, ("observed_rooms", "window"))
    if isinstance(queue, dict) and isinstance(queue.get("counts"), dict):
        result["queue"]["counts"] = {str(k)[:40]: _safe(v) for k, v in list(queue["counts"].items())[:20]}
    return result


class Monitor:
    def __init__(self, fetch, *, cache_seconds=3, clock=time.monotonic, enrich=None):
        self.fetch = fetch
        # Legacy preview callers may still pass enrich. Never invoke it: local
        # process/filesystem observations are outside this cloud-only monitor.
        self.cache_seconds = cache_seconds
        self.clock = clock
        self.lock = threading.Lock()
        self.last_success = None
        self.response = None
        self.next_fetch = 0

    def snapshot(self):
        with self.lock:
            if self.response is not None and self.clock() < self.next_fetch:
                return deepcopy(self.response)
            attempted = utc_now()
            error = None
            try:
                received = self.fetch()
                if (not isinstance(received, dict) or not isinstance(received.get("hub"), dict)
                        or received["hub"].get("deployment") != "cloud"):
                    raise StatusError("not_cloud")
                result = public_snapshot(received)
                self.last_success = deepcopy(result)
                self.last_success_at = attempted
            except StatusError as exc:
                error = exc.kind
            except Exception:
                error = "hub_error"
            if error:
                result = deepcopy(self.last_success) if self.last_success else public_snapshot({})
            result["cloud"] = {
                "provider": "Google Cloud", "platform": "Cloud Run",
                "hub_connection": "connected" if error is None else "not_connected" if error in ("unconfigured", "not_cloud") else "unavailable",
                "dashboard_runtime": "cloud_run" if os.environ.get("K_SERVICE") else "preview",
                "hub_last_seen": getattr(self, "last_success_at", None),
                "metrics_status": "not_connected", "cpu_percent": None,
                "memory_percent": None, "request_count": None, "instance_count": None,
            }
            result["dashboard"] = {"status": "ok", "fetched_at": attempted, "hub_reachable": error is None,
                                   "last_success_at": getattr(self, "last_success_at", None),
                                   "cached": error is not None and self.last_success is not None, "error": error}
            self.response = result
            self.next_fetch = self.clock() + self.cache_seconds
            return deepcopy(result)


class IapVerifier:
    """Verify ES256, audience, issuer, expiry and identity using cached IAP keys."""
    def __init__(self, audience):
        if not re.fullmatch(r"/projects/[0-9]+/locations/[a-z0-9-]+/services/[a-z0-9-]+", audience):
            raise ValueError("DASHBOARD_IAP_AUDIENCE must name the Cloud Run service")
        from google.auth import jwt
        from google.auth.crypt import es256
        self.jwt = jwt
        self.audience = audience
        self.lock = threading.Lock()
        self.keys = None
        self.expires = 0

    def identity(self, token):
        """Return only verified identity claims, never the assertion or extras."""
        if not isinstance(token, str) or not token or len(token) > 16_384:
            return None
        try:
            header = self.jwt.decode_header(token)
            if header.get("alg") != "ES256":
                return None
            with self.lock:
                if self.keys is None or time.monotonic() >= self.expires:
                    opener = build_opener(NoRedirect())
                    with opener.open("https://www.gstatic.com/iap/verify/public_key", timeout=5) as response:
                        encoded = response.read(100_001)
                    if len(encoded) > 100_000:
                        return None
                    self.keys = json.loads(encoded)
                    self.expires = time.monotonic() + 300
                keys = self.keys
            claims = self.jwt.decode(token, certs=keys, audience=self.audience, clock_skew_in_seconds=30)
            if (claims.get("iss") != "https://cloud.google.com/iap"
                    or not isinstance(claims.get("sub"), str) or not claims["sub"]
                    or not isinstance(claims.get("email"), str) or not claims["email"]
                    or any(type(claims.get(key)) not in (int, float)
                           or not math.isfinite(claims[key]) for key in ("iat", "exp"))
                    or not 0 < claims["exp"] - claims["iat"] <= 660):
                return None
            return {key: claims[key] for key in ("iss", "sub", "email", "iat", "exp")}
        except Exception:
            return None

    def __call__(self, token):
        # Existing dashboard callers expect a boolean authorization function.
        return self.identity(token) is not None


def owner_emails_from_environment():
    """Read a local allowlist for identity display; never enroll an OAuth owner.

    Missing or malformed configuration disables only /identity. Values are
    literal email addresses, not domains, patterns, or asserted identities.
    """
    raw = os.environ.get("HUB_OWNER_EMAILS_JSON", "")
    if not raw or len(raw) > 4096:
        return frozenset()
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        return frozenset()
    if (not isinstance(entries, list) or not entries or len(entries) > 2
            or any(not isinstance(email, str) or not 3 <= len(email) <= 320
                   or email != email.strip() or email.count("@") != 1
                   or any(character.isspace() or ord(character) < 32 for character in email)
                   or any(not part for part in email.split("@")) for email in entries)):
        return frozenset()
    return frozenset(email.casefold() for email in entries)


def make_server(host, port, monitor, *, authorize=None, oauth_service=None):
    owner_emails = owner_emails_from_environment()
    authorization_origin = _consent_origin() if oauth_service is not None else None
    if oauth_service is not None:
        from .oauth_service import OAuthError
    assets = {"/": ("index.html", "text/html; charset=utf-8"),
              "/app.css": ("app.css", "text/css; charset=utf-8"),
              "/app.js": ("cloud.js", "text/javascript; charset=utf-8")}

    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentMonitor"
        sys_version = ""

        def log_message(self, format, *args):
            return  # Request URLs and auth headers never enter console logs.

        def reply(self, code, data, content_type="application/json; charset=utf-8", *, csp=None,
                  referrer_policy="no-referrer", extra_headers=()):
            encoded = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", referrer_policy)
            self.send_header("Content-Security-Policy", csp or "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            for name, value in extra_headers:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(encoded)

        def verified_identity(self):
            verify_identity = getattr(authorize, 'identity', None)
            assertions = self.headers.get_all('X-Goog-IAP-JWT-Assertion', [])
            return verify_identity(assertions[0]) if callable(verify_identity) and len(assertions) == 1 else None

        def consent_error(self, status=400, error='invalid_request'):
            return self.reply(status, {'error': error, 'error_description': 'Authorization request rejected'},
                              csp=CONSENT_CSP)

        def begin_consent(self):
            claims = self.verified_identity()
            if claims is None:
                return self.consent_error(401, 'access_denied')
            subject = {key: claims[key] for key in ('iss', 'sub')}
            try:
                query = _consent_parameters(urlsplit(self.path).query)
            except (ValueError, UnicodeError):
                return self.consent_error()
            try:
                consent = oauth_service.begin_authorization(query, subject)
                # These values become HTML attributes and a cookie. The core's
                # opaque nonce contract is narrower than arbitrary strings.
                if (not isinstance(consent, dict) or consent.get('subject') != subject
                        or any(not isinstance(consent.get(key), str)
                               or not re.fullmatch(r'[A-Za-z0-9_-]{20,256}', consent[key]) for key in ('id', 'csrf'))
                        or any(not isinstance(consent.get(key), str) or len(consent[key]) > 4096
                               for key in ('client_id', 'scope', 'redirect_uri'))):
                    return self.consent_error(503, 'temporarily_unavailable')
                page_csp = _consent_page_csp(consent, oauth_service)
                page = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                        '<meta name="viewport" content="width=device-width, initial-scale=1">'
                        '<title>Approve MyHero connection</title></head><body>'
                        '<h1>Approve MyHero connection</h1><p>Client: <code>'
                        + escape(consent['client_id'], quote=True) + '</code></p><p>Requested scopes: <code>'
                        + escape(consent['scope'], quote=True) + '</code></p><p>Return address: <code>'
                        + escape(consent['redirect_uri'], quote=True) + '</code></p>'
                        '<form method="post" action="/oauth/approve">'
                        '<input type="hidden" name="id" value="' + escape(consent['id'], quote=True) + '">'
                        '<input type="hidden" name="csrf" value="' + escape(consent['csrf'], quote=True) + '">'
                        '<button type="submit">Approve connection</button></form>'
                        '<p>Close this page to decline.</p></body></html>')
                cookie = (CONSENT_COOKIE + '=' + consent['csrf']
                          + '; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=600')
                # no-referrer also nulls Origin for a navigation POST. Preserve
                # the same-origin POST Origin while suppressing cross-origin
                # referrers; all other responses retain no-referrer.
                return self.reply(200, page.encode('utf-8'), 'text/html; charset=utf-8', csp=page_csp,
                                  referrer_policy='same-origin',
                                  extra_headers=(('Set-Cookie', cookie),))
            except OAuthError as exc:
                return self.consent_error(exc.status, exc.error)
            except Exception:
                return self.consent_error(503, 'temporarily_unavailable')

        def approve_consent(self):
            claims = self.verified_identity()
            if claims is None:
                return self.consent_error(401, 'access_denied')
            if self.headers.get_all('Origin', []) != [authorization_origin]:
                return self.consent_error(403, 'access_denied')
            if urlsplit(self.path).query or self.headers.get_all('Transfer-Encoding', []):
                return self.consent_error()
            if self.headers.get_all('Content-Type', []) != ['application/x-www-form-urlencoded']:
                return self.consent_error(415)
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,9}', lengths[0]):
                return self.consent_error()
            length = int(lengths[0])
            if not 0 < length <= MAX_CONSENT_INPUT:
                return self.consent_error(413)
            try:
                self.connection.settimeout(10)
                body = self.rfile.read(length)
                if len(body) != length:
                    return self.consent_error()
                form = _consent_parameters(body.decode('utf-8'))
            except (ValueError, UnicodeError, OSError):
                return self.consent_error()
            if (set(form) != {'id', 'csrf'} or any(not re.fullmatch(r'[A-Za-z0-9_-]{20,256}', form[key])
                                                   for key in ('id', 'csrf'))):
                return self.consent_error()
            cookies = self.headers.get_all('Cookie', [])
            if sum(len(value) for value in cookies) > 4096:
                return self.consent_error(403, 'access_denied')
            nonces = []
            for header in cookies:
                for item in header.split(';'):
                    key, separator, value = item.strip().partition('=')
                    if key == CONSENT_COOKIE:
                        nonces.append(value if separator else '')
            if (len(nonces) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{20,256}', nonces[0])
                    or not hmac.compare_digest(nonces[0], form['csrf'])):
                return self.consent_error(403, 'access_denied')
            try:
                location = oauth_service.approve(form['id'], {key: claims[key] for key in ('iss', 'sub')}, form['csrf'])
                # The core checks the exact registered callback. This additional
                # header check prevents malformed redirect output from escaping.
                if not isinstance(location, str) or not 1 <= len(location) <= 16_384:
                    return self.consent_error(503, 'temporarily_unavailable')
                parsed = urlsplit(location)
                if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                        or parsed.fragment or any(ord(c) <= 32 or ord(c) >= 127 for c in location)):
                    return self.consent_error(503, 'temporarily_unavailable')
                clear = CONSENT_COOKIE + '=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0'
                return self.reply(303, b'', csp=CONSENT_CSP,
                                  extra_headers=(('Location', location), ('Set-Cookie', clear)))
            except OAuthError as exc:
                return self.consent_error(exc.status, exc.error)
            except Exception:
                return self.consent_error(503, 'temporarily_unavailable')

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/healthz":
                return self.reply(200, {"status": "ok", "service": "agent-dashboard"})
            if path == '/oauth/authorize' and oauth_service is not None:
                return self.begin_consent()
            if path == "/identity":
                # A boolean-only legacy authorizer is insufficient to establish
                # identity. Never decode unverified headers or use email headers.
                claims = self.verified_identity()
                if claims is None:
                    return self.reply(401, {"error": "Google IAP sign-in required"}, csp=IDENTITY_CSP)
                if not owner_emails or claims["email"].casefold() not in owner_emails:
                    return self.reply(403, {"error": "Identity enrollment display is not available for this account"}, csp=IDENTITY_CSP)
                page = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                        "<title>Verified Google identity</title></head><body>"
                        "<h1>Verified Google identity</h1><dl><dt>Email</dt><dd><code>"
                        + escape(claims["email"], quote=True) + "</code></dd><dt>Subject</dt><dd><code>"
                        + escape(claims["sub"], quote=True) + "</code></dd></dl>"
                        "<p>Use these values to request enrollment. This page does not grant access.</p>"
                        "</body></html>")
                return self.reply(200, page.encode("utf-8"), "text/html; charset=utf-8", csp=IDENTITY_CSP)
            if authorize and not authorize(self.headers.get("X-Goog-IAP-JWT-Assertion", "")):
                return self.reply(401, {"error": "Google IAP sign-in required"})
            if path == "/api/status":
                return self.reply(200, monitor.snapshot())
            if path in assets:
                filename, mime = assets[path]
                return self.reply(200, (WEB_ROOT / filename).read_bytes(), mime)
            return self.reply(404, {"error": "Not found"})

        def do_HEAD(self):
            if urlsplit(self.path).path.startswith('/oauth/'):
                return self.reject()
            self.do_GET()

        def do_POST(self):
            if urlsplit(self.path).path == '/oauth/approve' and oauth_service is not None:
                return self.approve_consent()
            return self.reject()

        def reject(self):
            self.reply(405, {"error": "This dashboard is read-only"})
        do_PUT = do_PATCH = do_DELETE = do_OPTIONS = reject

    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8090")))
    args = parser.parse_args(argv)
    production = bool(os.environ.get("K_SERVICE"))
    try:
        audience = os.environ.get("DASHBOARD_IAP_AUDIENCE", "")
        if production and not audience:
            raise ValueError("Cloud Run requires DASHBOARD_IAP_AUDIENCE and direct IAP protection")
        if production and (not os.environ.get("HUB_STATUS_TOKEN")
                           or not os.environ.get("HUB_URL", "").startswith("https://")
                           or os.environ.get("HUB_CLOUD_RUN_AUTH_MODE") != "metadata"):
            raise ValueError("Cloud Run requires an HTTPS HUB_URL, HUB_STATUS_TOKEN and metadata identity mode")
        authorize = IapVerifier(audience) if audience else None
        token = os.environ.get("HUB_STATUS_TOKEN") or os.environ.get("HUB_MANAGER_TOKEN", "")
        principal = "status" if os.environ.get("HUB_STATUS_TOKEN") else "manager"
        gateway = StatusGateway(os.environ.get("HUB_URL", "http://127.0.0.1:8080"), token,
                                os.environ.get("HUB_CLOUD_RUN_AUTH_MODE") or None, principal)
        from .oauth_runtime import load_oauth_service
        server = make_server("0.0.0.0" if production else "127.0.0.1", args.port,
                             Monitor(gateway), authorize=authorize, oauth_service=load_oauth_service())
    except (ValueError, ImportError) as exc:
        parser.error(str(exc))
    print(f"Read-only dashboard listening on port {server.server_port}.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
