"""Offline proof that this image can verify an RS256 token with google-auth.

The fleet controller does not verify ID tokens and does not install the
library. GoogleIDAuthenticator.__call__ imports google.oauth2.id_token lazily
and maps any failure, including ImportError, to BoundaryError
('authentication_required'). A broker process that reached HTTP would then
answer 401 to every worker while serve.py --check stayed green. check()
fails closed first, with the fixed code broker_auth_library_unavailable and
no library text.

Production verification is google.oauth2.id_token.verify_oauth2_token. That
loads a {kid: x509 certificate} map through the request callable
GoogleIDAuthenticator passes, then checks the issuer. The certificate below
is self-signed and public. The private key was used once to sign TOKEN and
was not written into this tree.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

CODE = 'broker_auth_library_unavailable'
AUDIENCE = 'https://runcrew.invalid/broker-auth-probe'
ISSUER = 'https://accounts.google.com'
CLAIM_NAME = 'probe'
CLAIM_VALUE = 'broker_auth_ok'
# 2100-01-01T00:00:00Z. iat is 2023-11-14T22:13:20Z.
ISSUED = 1700000000
EXPIRES = 4102444800
CERT_PEM = """-----BEGIN CERTIFICATE-----
MIICszCCAZugAwIBAgIBATANBgkqhkiG9w0BAQsFADAcMRowGAYDVQQDDBFicm9r
ZXItYXV0aC1wcm9iZTAgFw0yMzAxMDEwMDAwMDBaGA8yMTAwMDEwMTAwMDAwMFow
HDEaMBgGA1UEAwwRYnJva2VyLWF1dGgtcHJvYmUwggEiMA0GCSqGSIb3DQEBAQUA
A4IBDwAwggEKAoIBAQDadLK2sq9ADaKgHqsgBAFulnKDXXRAMoVym5K/hW7/Ea5w
Gk0gpLIzn8btOtRhAoHnOlwMwo5g8KiHOePUgrT/HQJEzg0UZ5vbJZ5ecYau2fo7
gnezavACYctA+o3hgvnAd6y25WjDOGUbASfy/q98Au2zesdV8Ui3LYQJ+2s8uKAj
97EH/jTgtmINrtcurtL2E/b9kmhzRjKjmEp4we+ULiXrcQkNSfM7etFA3Y3JWvd9
MfnkF9C+fnYZc7SyYyMZkants+Nsefu9ELQheWmr1YchHdlDYTSJrAIr/lbnHeR4
e0TiixCF5CId1IKr+g0HylBlQ9Z/Bwc285tIfCBZAgMBAAEwDQYJKoZIhvcNAQEL
BQADggEBALSUocIWZV2UJjyezZOo0IIsHLGhhL5ljvVmEgwYzlrEbHbB/jzFxHZN
GisUEKj4mGHsmIk7FLno7kSvwCV4u3l3Lg/Eeup1PIcPJ2GUDO5QC+4yz2YuRaBj
0ncppg2QvEo/Cp9Mdi6zIdSczRT3YJdNEIjU/nU4VqwSfFSxhM05QNet0GuJ34l7
uokmu0h+2ScQeg7euDnRqY08tS7Shz6RKMAQjkMUtzcWnq6Ck03nfcl35TmDLan7
NjxP9z7mn40pLxMQa8EcZdmLvSGdcRPU5uj8R6SuvRZqd0B8ZdwM9syX17PUeZbl
FWhVYPbiEQBn2CkmVhwZNRXhAOmr110=
-----END CERTIFICATE-----
"""
TOKEN = (
    'eyJ0eXAiOiAiSldUIiwgImFsZyI6ICJSUzI1NiIsICJraWQiOiAiayJ9.'
    'eyJhdWQiOiAiaHR0cHM6Ly9ydW5jcmV3LmludmFsaWQvYnJva2VyLWF1dGgtcHJvYmUiLCAiaXNzIjogImh0dHBzOi8vYWNjb3VudHMuZ29vZ2xlLmNvbSIsICJpYXQiOiAxNzAwMDAwMDAwLCAiZXhwIjogNDEwMjQ0NDgwMCwgInByb2JlIjogImJyb2tlcl9hdXRoX29rIn0.'
    '00-Ds9kR7_UCJ8XFC_x2-2lUx__jcqY2sAlzdCeg5OS_cd5iEpAdnFcBFJLqsumC7mvuPQiakejohjfSq0sLOJliuylWBjihFkUotTRuE7r7vWcTuZLwBWTAs0m1VUj-TNmYqBi0FQtyq1wIx8sBhFsqXNYPOczPgOsz4-H291uuEIPWWd4oFg9juK2F4XwWKY5NM4Nw20QfQaE6VC9UjDErKCAUwvdT-ZluL_1W4TTVi2AYcI1DqyJdnojbH74AbeYEPdwn4MtvLD3e9k8SlN7tAIPTNUAGIi3XNiPob2mpMixE7iqM4tp6ZZQpSM0vo2LzbH8_jRIujbB1OIhsWQ'
)


class BrokerAuthLibraryUnavailable(Exception):
    """Fixed code only. Callers must not surface a library message."""

    def __init__(self):
        super().__init__(CODE)


def check():
    try:
        _verify()
    except BrokerAuthLibraryUnavailable:
        raise
    except Exception:
        raise BrokerAuthLibraryUnavailable() from None


def _verify():
    import google.oauth2.id_token as id_token
    if not callable(getattr(id_token, 'verify_oauth2_token', None)):
        raise BrokerAuthLibraryUnavailable()

    def stub(url, method='GET', **kwargs):
        return SimpleNamespace(status=200, data=json.dumps({'k': CERT_PEM}).encode())

    claims = id_token.verify_oauth2_token(TOKEN, stub, audience=AUDIENCE)
    if claims != {
        'aud': AUDIENCE, 'iss': ISSUER, 'iat': ISSUED, 'exp': EXPIRES, CLAIM_NAME: CLAIM_VALUE}:
        raise BrokerAuthLibraryUnavailable()
