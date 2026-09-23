"""Private OAuth authorization-code/PKCE service built on OAuthLib 3.3.1.

HTTP adapters must supply an independently verified IAP identity, enforce HTTPS,
bound request bodies, bind the returned consent to their browser session, and
never log query strings, codes, credentials or token responses. This module
does not verify IAP JWTs or enroll identities.

Only predefined public or client_secret_post clients are supported. Administrative
set_subject_enabled is a trusted operation, never a public OAuth route.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from oauthlib.oauth2 import RequestValidator, WebApplicationServer
from oauthlib.oauth2.rfc6749.errors import OAuth2Error

# OAuthLib debug records contain credentials; suppress its namespace entirely.
_oauth_logger = logging.getLogger("oauthlib")
_oauth_logger.addHandler(logging.NullHandler())
_oauth_logger.propagate = False

IAP_ISSUER = "https://cloud.google.com/iap"
SCOPE = "hub:manage"
_HASH = re.compile(r"[a-f0-9]{64}")
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{20,256}")


def credential_hash(value):
    """Hash a high-entropy credential; callers must not use human passwords."""
    if not isinstance(value, str):
        raise ValueError("Credential must be a string")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity_key(identity):
    return credential_hash(json.dumps([identity["iss"], identity["sub"]], separators=(",", ":")))


def _https(value, *, origin=False, query=False):
    if not isinstance(value, str) or not value or any(ord(c) <= 32 for c in value):
        raise ValueError("A canonical HTTPS URL is required")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid URL") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or (parsed.query and not query)
            or (origin and parsed.path) or parsed.hostname != parsed.hostname.lower()
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("A canonical HTTPS URL is required")
    return value


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    redirect_uris: tuple[str, ...]
    secret_hash: str | None = None

    def __post_init__(self):
        if (not isinstance(self.client_id, str) or not 1 <= len(self.client_id) <= 256
                or any(ord(c) < 33 or ord(c) > 126 for c in self.client_id)):
            raise ValueError("Invalid predefined client ID")
        if not self.redirect_uris or isinstance(self.redirect_uris, str):
            raise ValueError("Register exact redirect URIs")
        object.__setattr__(self, "redirect_uris", tuple(self.redirect_uris))
        for uri in self.redirect_uris:
            _https(uri)
        if len(set(self.redirect_uris)) != len(self.redirect_uris):
            raise ValueError("Duplicate redirect URI")
        if self.secret_hash is not None and not _HASH.fullmatch(self.secret_hash):
            raise ValueError("Client secret must be a SHA-256 hash")

    @property
    def token_endpoint_auth_method(self):
        return "client_secret_post" if self.secret_hash is not None else "none"


@dataclass(frozen=True)
class PinnedIdentity:
    issuer: str
    subject: str

    def __post_init__(self):
        if (self.issuer != IAP_ISSUER or not isinstance(self.subject, str)
                or not 1 <= len(self.subject) <= 512
                or any(ord(c) <= 32 or ord(c) == 127 for c in self.subject)):
            raise ValueError("A verified Google IAP issuer and subject must be pinned")

    def claims(self):
        return {"iss": self.issuer, "sub": self.subject}


@dataclass(frozen=True)
class OAuthConfig:
    issuer: str
    resource: str
    clients: tuple[OAuthClient, ...]
    identities: tuple[PinnedIdentity, ...]
    access_ttl: int = 300
    code_ttl: int = 120
    consent_ttl: int = 600
    refresh_ttl: int = 2_592_000

    def __post_init__(self):
        _https(self.issuer, origin=True)
        _https(self.resource)
        object.__setattr__(self, "clients", tuple(self.clients))
        object.__setattr__(self, "identities", tuple(self.identities))
        if not self.clients or any(not isinstance(c, OAuthClient) for c in self.clients):
            raise ValueError("Predefined clients are required")
        if len({c.client_id for c in self.clients}) != len(self.clients):
            raise ValueError("Duplicate predefined client")
        if (len(self.identities) != 2 or any(not isinstance(i, PinnedIdentity) for i in self.identities)
                or len({(i.issuer, i.subject) for i in self.identities}) != 2):
            raise ValueError("Exactly two distinct, verified IAP identities are required")
        for value, ceiling in ((self.access_ttl, 3600), (self.code_ttl, 600),
                               (self.consent_ttl, 1800), (self.refresh_ttl, 2_592_000)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("OAuth lifetime outside permitted bounds")
        if self.refresh_ttl < self.access_ttl:
            raise ValueError("Refresh family must outlive access tokens")


class OAuthError(ValueError):
    def __init__(self, error, description="OAuth request rejected", status=400):
        self.error = error
        self.description = description
        self.status = status
        super().__init__(description)

    def as_dict(self):
        return {"error": self.error, "error_description": self.description}


def _params(value):
    if isinstance(value, str):
        if len(value) > 16_384 or re.search(r"%(?![0-9a-fA-F]{2})", value):
            raise OAuthError("invalid_request")
        try:
            pairs = parse_qsl(value, keep_blank_values=True, strict_parsing=True,
                              encoding="utf-8", errors="strict", max_num_fields=20)
        except (ValueError, UnicodeError):
            raise OAuthError("invalid_request") from None
    elif isinstance(value, dict):
        pairs = list(value.items())
    elif isinstance(value, (tuple, list)):
        pairs = value
    else:
        raise OAuthError("invalid_request")
    result = {}
    if len(pairs) > 20:
        raise OAuthError("invalid_request")
    for pair in pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise OAuthError("invalid_request")
        key, item = pair
        if (not isinstance(key, str) or not isinstance(item, str) or key in result
                or not 1 <= len(key) <= 64 or len(item) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in key + item)):
            raise OAuthError("invalid_request")
        result[key] = item
    return result


class OAuthService:
    def __init__(self, config: OAuthConfig, store, *, clock=time.time):
        self.config = config
        self.store = store
        self.clock = clock
        self.clients = {client.client_id: client for client in config.clients}
        self.identities = {_identity_key(i.claims()): i.claims() for i in config.identities}

        def initialize(tx):
            for key, identity in self.identities.items():
                existing = tx.get("subjects", key)
                if existing is None:
                    tx.put("subjects", key, {**identity, "enabled": True, "generation": 1})
                elif existing.get("iss") != identity["iss"] or existing.get("sub") != identity["sub"]:
                    raise ValueError("Pinned OAuth identity record is inconsistent")
        self.store.run(initialize)

    def _subject(self, value):
        if isinstance(value, PinnedIdentity):
            value = value.claims()
        if not isinstance(value, dict):
            raise OAuthError("access_denied", status=403)
        identity = {"iss": value.get("iss"), "sub": value.get("sub")}
        if not all(isinstance(v, str) for v in identity.values()):
            raise OAuthError("access_denied", status=403)
        if _identity_key(identity) not in self.identities:
            raise OAuthError("access_denied", status=403)
        return identity

    def _active(self, tx, identity, generation=None):
        key = _identity_key(identity)
        pinned = self.identities.get(key)
        saved = tx.get("subjects", key) if pinned else None
        return bool(saved and saved.get("enabled") is True
                    and saved.get("iss") == identity["iss"]
                    and saved.get("sub") == identity["sub"]
                    and (generation is None or saved.get("generation", 1) == generation))

    def set_subject_enabled(self, identity, enabled):
        """Trusted administration. Persisted revocation survives restart."""
        identity = self._subject(identity)
        if type(enabled) is not bool:
            raise ValueError("enabled must be Boolean")
        def update(tx):
            key = _identity_key(identity)
            record = tx.get("subjects", key)
            if record is None:
                raise OAuthError("access_denied", status=403)
            tx.put("subjects", key, {**record, "enabled": enabled,
                    "generation": record.get("generation", 1) + (record["enabled"] != enabled)})
        self.store.run(update)

    def metadata(self, issuer, authorization_endpoint):
        if issuer != self.config.issuer:
            raise ValueError("Metadata issuer must match configured issuer")
        _https(authorization_endpoint)
        return {
            "issuer": issuer,
            "authorization_endpoint": authorization_endpoint,
            "token_endpoint": issuer + "/oauth/token",
            "revocation_endpoint": issuer + "/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": sorted({
                c.token_endpoint_auth_method for c in self.config.clients}),
            "scopes_supported": [SCOPE],
            "authorization_response_iss_parameter_supported": True,
        }

    def protected_resource_metadata(self):
        return {"resource": self.config.resource,
                "authorization_servers": [self.config.issuer],
                "scopes_supported": [SCOPE],
                "bearer_methods_supported": ["header"]}

    def _server(self, tx, now):
        return WebApplicationServer(
            _Validator(self, tx, now),
            token_expires_in=self.config.access_ttl,
            token_generator=lambda request: secrets.token_urlsafe(32),
            refresh_token_generator=lambda request: secrets.token_urlsafe(32))

    def begin_authorization(self, query, verified_subject):
        identity = self._subject(verified_subject)
        params = _params(query)
        # OIDC Core 3.1.2.1 defines ui_locales as an optional, space-separated
        # language preference. This bridge does not localize consent: bound the
        # hint's syntax, then discard it before protocol validation or storage.
        # _params has already rejected duplicate keys and malformed encodings.
        locales = params.pop("ui_locales", None)
        # OAuth 2.0 treats an empty parameter value as omitted.
        if locales not in (None, "") and (
                not 1 <= len(locales) <= 256 or len(locales.split(" ")) > 8
                or not all(re.fullmatch(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*", tag)
                           for tag in locales.split(" "))):
            raise OAuthError("invalid_request", "Invalid UI locale hint")
        allowed = {"response_type", "client_id", "redirect_uri", "scope", "state",
                   "code_challenge", "code_challenge_method", "resource"}
        if set(params) != allowed:
            raise OAuthError("invalid_request", "Required authorization parameters missing or unsupported")
        if params["response_type"] != "code":
            raise OAuthError("unsupported_response_type")
        if params["resource"] != self.config.resource:
            raise OAuthError("invalid_target")
        if (params["code_challenge_method"] != "S256"
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params["code_challenge"])):
            raise OAuthError("invalid_request", "PKCE S256 is required")
        if not 1 <= len(params["state"]) <= 1024:
            raise OAuthError("invalid_request", "A bounded state value is required")
        if params["scope"] != SCOPE:
            raise OAuthError("invalid_scope")
        now = self.clock()
        opaque_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        uri = self.config.issuer + "/oauth/authorize?" + urlencode(params)

        def begin(tx):
            if not self._active(tx, identity):
                raise OAuthError("access_denied", status=403)
            try:
                self._server(tx, now).validate_authorization_request(uri)
            except OAuth2Error as exc:
                raise OAuthError(exc.error) from None
            tx.put("consents", credential_hash(opaque_id), {
                "identity": identity, "params": params, "csrf_hash": credential_hash(csrf),
                "generation": tx.get("subjects", _identity_key(identity)).get("generation", 1),
                "expires_at": now + self.config.consent_ttl, "used": False,
            })
            return {"id": opaque_id, "csrf": csrf, "client_id": params["client_id"],
                    "redirect_uri": params["redirect_uri"], "scope": SCOPE,
                    "subject": identity, "expires_at": now + self.config.consent_ttl}
        return self.store.run(begin)

    def approve(self, transaction_id, verified_subject, csrf):
        identity = self._subject(verified_subject)
        if not isinstance(transaction_id, str) or not _OPAQUE.fullmatch(transaction_id):
            raise OAuthError("invalid_request")
        if not isinstance(csrf, str) or not _OPAQUE.fullmatch(csrf):
            raise OAuthError("access_denied", status=403)
        now = self.clock()

        def approve(tx):
            key = credential_hash(transaction_id)
            consent = tx.get("consents", key)
            if (not consent or consent["used"] or consent["expires_at"] <= now
                    or consent["identity"] != identity
                    or not self._active(tx, identity, consent["generation"])
                    or not hmac.compare_digest(consent["csrf_hash"], credential_hash(csrf))):
                raise OAuthError("access_denied", status=403)
            params = consent["params"]
            uri = self.config.issuer + "/oauth/authorize?" + urlencode(params)
            try:
                headers, _, status = self._server(tx, now).create_authorization_response(
                    uri, scopes=[SCOPE], credentials={"user": identity, "subject_generation": consent["generation"]})
            except OAuth2Error as exc:
                raise OAuthError(exc.error) from None
            location = headers.get("Location", "")
            parsed = urlsplit(location)
            response_params = parse_qsl(parsed.query, keep_blank_values=True)
            if status != 302 or "code" not in dict(response_params) or "error" in dict(response_params):
                raise OAuthError("invalid_request")
            if urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")) != params["redirect_uri"]:
                raise OAuthError("invalid_request")
            consent["used"] = True
            tx.put("consents", key, consent)
            response_params.append(("iss", self.config.issuer))
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(response_params), ""))
        return self.store.run(approve)

    def exchange(self, form):
        params = _params(form)
        grant = params.get("grant_type")
        if grant not in ("authorization_code", "refresh_token"):
            raise OAuthError("unsupported_grant_type")
        required = {"grant_type", "client_id", "resource"}
        required |= {"code", "redirect_uri", "code_verifier"} if grant == "authorization_code" else {"refresh_token"}
        allowed = required | {"client_secret"} | ({"scope"} if grant == "refresh_token" else set())
        if not required <= set(params) or not set(params) <= allowed:
            raise OAuthError("invalid_request")
        if params["resource"] != self.config.resource:
            raise OAuthError("invalid_target")
        if grant == "authorization_code" and not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", params["code_verifier"]):
            raise OAuthError("invalid_request", "Invalid PKCE verifier")
        credential = params["code" if grant == "authorization_code" else "refresh_token"]
        if not _OPAQUE.fullmatch(credential):
            raise OAuthError("invalid_grant")
        if "scope" in params and params["scope"] != SCOPE:
            raise OAuthError("invalid_scope")
        client = self.clients.get(params["client_id"])
        if not client or (client.secret_hash is None and "client_secret" in params):
            raise OAuthError("invalid_client", status=401)
        now = self.clock()

        def exchange(tx):
            # OAuthLib performs PKCE, redirect and client validation inside the
            # same transaction that consumes the grant and saves tokens.
            try:
                _, body, status = self._server(tx, now).create_token_response(
                    self.config.issuer + "/oauth/token", http_method="POST",
                    body=urlencode(params), headers={"Content-Type": "application/x-www-form-urlencoded"})
            except OAuth2Error as exc:
                return exc.status_code, {"error": exc.error}
            return status, json.loads(body)
        status, result = self.store.run(exchange)
        # Return errors AFTER commit so refresh replay detection can revoke the
        # family even though the exchange itself is rejected.
        if status != 200 or "error" in result:
            raise OAuthError(result.get("error", "invalid_grant"), status=status)
        return result

    def authenticate(self, access_token, required_scope=SCOPE):
        if not isinstance(access_token, str) or not _OPAQUE.fullmatch(access_token):
            raise OAuthError("invalid_token", status=401)
        if required_scope != SCOPE:
            raise OAuthError("insufficient_scope", status=403)
        now = self.clock()

        def authenticate(tx):
            record = tx.get("access", credential_hash(access_token))
            if not record or record["expires_at"] <= now:
                raise OAuthError("invalid_token", status=401)
            family = tx.get("families", record["family"])
            if (not family or family["revoked"] or family["expires_at"] <= now
                    or record["issuer"] != self.config.issuer or record["resource"] != self.config.resource
                    or record["client_id"] not in self.clients
                    or record["scope"] != required_scope
                    or not self._active(tx, record["identity"], record["generation"])):
                raise OAuthError("invalid_token", status=401)
            return {**record["identity"], "client_id": record["client_id"],
                    "scope": record["scope"], "resource": record["resource"]}
        return self.store.run(authenticate)

    def revoke(self, token, *, expected_client_id=None):
        """Revoke a token family without disclosing token existence.

        HTTP adapters must supply the identified client and apply client-auth
        policy. Trusted internal callers may omit the client restriction.
        Unknown, malformed, and differently bound tokens are silent no-ops.
        """
        if not isinstance(token, str) or not _OPAQUE.fullmatch(token):
            return
        key = credential_hash(token)
        def revoke(tx):
            record = tx.get("access", key) or tx.get("refresh", key)
            if record and (expected_client_id is None or record["client_id"] == expected_client_id):
                family = tx.get("families", record["family"])
                if family:
                    family["revoked"] = True
                    tx.put("families", record["family"], family)
        self.store.run(revoke)


class _Validator(RequestValidator):
    def __init__(self, service, tx, now):
        self.service, self.tx, self.now = service, tx, now

    def validate_client_id(self, client_id, request, *args, **kwargs):
        request.client = self.service.clients.get(client_id)
        return request.client is not None

    def validate_redirect_uri(self, client_id, redirect_uri, request, *args, **kwargs):
        client = self.service.clients.get(client_id)
        return bool(client and redirect_uri in client.redirect_uris)

    def get_default_redirect_uri(self, client_id, request, *args, **kwargs):
        return None  # This service requires explicit exact callbacks.

    def validate_response_type(self, client_id, response_type, client, request, *args, **kwargs):
        return response_type == "code"

    def validate_scopes(self, client_id, scopes, client, request, *args, **kwargs):
        return scopes == [SCOPE]

    def get_default_scopes(self, client_id, request, *args, **kwargs):
        return [SCOPE]

    def is_pkce_required(self, client_id, request):
        return True

    def client_authentication_required(self, request, *args, **kwargs):
        client = self.service.clients.get(request.client_id)
        return client is None or client.secret_hash is not None

    def authenticate_client(self, request, *args, **kwargs):
        client = self.service.clients.get(request.client_id)
        supplied = getattr(request, "client_secret", None)
        if not client or client.secret_hash is None or not isinstance(supplied, str):
            return False
        if not hmac.compare_digest(client.secret_hash, credential_hash(supplied)):
            return False
        request.client = client
        return True

    def authenticate_client_id(self, client_id, request, *args, **kwargs):
        client = self.service.clients.get(client_id)
        if not client or client.secret_hash is not None:
            return False
        request.client = client
        return True

    def validate_grant_type(self, client_id, grant_type, client, request, *args, **kwargs):
        return grant_type in ("authorization_code", "refresh_token")

    def save_authorization_code(self, client_id, code, request, *args, **kwargs):
        self.tx.put("codes", credential_hash(code["code"]), {
            "identity": request.user, "client_id": client_id,
            "generation": request.subject_generation,
            "redirect_uri": request.redirect_uri, "scope": SCOPE,
            "resource": self.service.config.resource, "issuer": self.service.config.issuer,
            "challenge": request.code_challenge, "challenge_method": "S256",
            "expires_at": self.now + self.service.config.code_ttl, "used": False,
        })

    def validate_code(self, client_id, code, client, request, *args, **kwargs):
        record = self.tx.get("codes", credential_hash(code))
        if (not record or record["client_id"] != client_id or record["used"]
                or record["expires_at"] <= self.now or record["resource"] != request.resource
                or record["issuer"] != self.service.config.issuer
                or not self.service._active(self.tx, record["identity"], record["generation"])):
            return False
        request.user, request.scopes = record["identity"], [record["scope"]]
        request.hub_record = record
        return True

    def get_code_challenge(self, code, request):
        return request.hub_record["challenge"]

    def get_code_challenge_method(self, code, request):
        return request.hub_record["challenge_method"]

    def confirm_redirect_uri(self, client_id, code, redirect_uri, client, request, *args, **kwargs):
        return request.hub_record["redirect_uri"] == redirect_uri

    def invalidate_authorization_code(self, client_id, code, request, *args, **kwargs):
        key = credential_hash(code)
        record = self.tx.get("codes", key)
        record["used"] = True
        self.tx.put("codes", key, record)

    def validate_refresh_token(self, refresh_token, client, request, *args, **kwargs):
        record = self.tx.get("refresh", credential_hash(refresh_token))
        if not record or record["client_id"] != client.client_id or record["resource"] != request.resource:
            return False
        family = self.tx.get("families", record["family"])
        if not family or family["revoked"] or family["expires_at"] <= self.now:
            return False
        if record["used"]:
            family["revoked"] = True
            self.tx.put("families", record["family"], family)
            return False
        if (record["expires_at"] <= self.now or record["issuer"] != self.service.config.issuer
                or not self.service._active(self.tx, record["identity"], record["generation"])):
            return False
        request.user = record["identity"]
        request.hub_record, request.hub_family = record, family
        return True

    def get_original_scopes(self, refresh_token, request, *args, **kwargs):
        return [request.hub_record["scope"]]

    def rotate_refresh_token(self, request):
        return True

    def save_bearer_token(self, token, request, *args, **kwargs):
        record = request.hub_record
        if request.grant_type == "authorization_code":
            family_key = credential_hash(secrets.token_urlsafe(32))
            family = {"identity": record["identity"], "client_id": record["client_id"],
                      "expires_at": self.now + self.service.config.refresh_ttl, "revoked": False}
            self.tx.put("families", family_key, family)
        else:
            family_key, family = record["family"], request.hub_family
            record["used"] = True
            self.tx.put("refresh", credential_hash(request.refresh_token), record)
        lifetime = min(self.service.config.access_ttl, family["expires_at"] - self.now)
        token["expires_in"] = int(lifetime)
        common = {"identity": record["identity"], "client_id": record["client_id"],
                  "scope": SCOPE, "resource": self.service.config.resource,
                  "issuer": self.service.config.issuer, "family": family_key,
                  "generation": record["generation"]}
        self.tx.put("access", credential_hash(token["access_token"]),
                    {**common, "expires_at": self.now + lifetime})
        self.tx.put("refresh", credential_hash(token["refresh_token"]),
                    {**common, "expires_at": family["expires_at"], "used": False})

    def save_token(self, token, request, *args, **kwargs):
        self.save_bearer_token(token, request, *args, **kwargs)
