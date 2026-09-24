"""Offline proof that this image can verify an RS256 token with google-auth.

The fleet controller does not verify ID tokens and does not install the
library. GoogleIDAuthenticator.__call__ imports google.oauth2.id_token lazily
and maps any failure, including ImportError, to BoundaryError
('authentication_required'). A broker process that reached HTTP would then
answer 401 to every worker while serve.py --check stayed green. check()
fails closed first, with the fixed code broker_auth_library_unavailable and
no library text.

The RS256 key pair is test-only. The private key was used once to sign TOKEN
and was not written into this tree.
"""
from __future__ import annotations

import base64

CODE = 'broker_auth_library_unavailable'
AUDIENCE = 'https://runcrew.invalid/broker-auth-probe'
CLAIM_NAME = 'probe'
CLAIM_VALUE = 'broker_auth_ok'
# 2100-01-01T00:00:00Z. iat is 2023-11-14T22:13:20Z.
EXPIRES = 4102444800
PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAgMoWqYVGzVdHtiPvZdyU
hsgRxzp+maMcm5dmCAD0zYwkZQtyw4rLVEJ2jvjReKlRJ1+NdnrXVQHH8ZrpVSAI
jMUQ9U6mCQ2u2+IyXSrjTakBtEQO3DCVX82gP0bIGP/pn0l3gi0Q50IjToAmUyhh
+QTn7E3V4lSc/E9IWfcwsx0sYp8Vs9x7WbiUe3X9i9X81DUlM3pg/4LXWTZYCA9Z
QRNHr37OeewTBkXrrPUt1xZVM2rfL86HhVvYu75QZNlVHRRr5a0oQ49gtMJAhT3K
f2MGJ5H8muJJcUQt8yBHJv1ttr7Q8hRvyyarX118MfQWbfG6iTzR00yvyt3q5vGQ
rwIDAQAB
-----END PUBLIC KEY-----
"""
TOKEN = (
    'eyJ0eXAiOiAiSldUIiwgImFsZyI6ICJSUzI1NiJ9.'
    'eyJhdWQiOiAiaHR0cHM6Ly9ydW5jcmV3LmludmFsaWQvYnJva2VyLWF1dGgtcHJvYmUiLCAiaWF0IjogMTcwMDAwMDAwMCwgImV4cCI6IDQxMDI0NDQ4MDAsICJwcm9iZSI6ICJicm9rZXJfYXV0aF9vayJ9.'
    'RP4_wdFiRJejbrLTzQm8blbjXrClORTqIUqgZMRGPJ940psTyWdnWP-HhxQu_jA5C7Ea8VioAycDwnYRYJWVPv9pxLrezXeQcV8e1G05vMqq9HT3MmVIWBxrzmmDbT5WA9TAiYy_cOH6k_aQGIcQ19ZNC6RWUs00ewUZybwU4p--EBpDR73Lba0pkfGw6SmwBUIDFjf1mSGJ2t84a-feFrKK0E5cVLxFtiwM82F6xE0o6jzZCQW-PZTqCOJgL_Pw1S_7vPjaZc-q-vwqz15zz5HIxTknBhuhOtTuPRGpLFWByoPStMFB54Q84CiDD1zVVHbZkhdORIF0O8VDVL8jxw'
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
    import google.auth.jwt as jwt
    import google.auth.crypt as crypt
    if not callable(getattr(id_token, 'verify_oauth2_token', None)):
        raise BrokerAuthLibraryUnavailable()
    header_b64, payload_b64, signature_b64 = TOKEN.split('.')
    message = (header_b64 + '.' + payload_b64).encode('ascii')
    signature = base64.urlsafe_b64decode(signature_b64 + '=' * (-len(signature_b64) % 4))
    verifier = crypt.RSAVerifier.from_string(PUBLIC_KEY_PEM)
    if verifier.verify(message, signature) is not True:
        raise BrokerAuthLibraryUnavailable()
    claims = jwt.decode(TOKEN, certs=PUBLIC_KEY_PEM, verify=True, audience=AUDIENCE)
    if (not isinstance(claims, dict) or claims.get('aud') != AUDIENCE
            or claims.get(CLAIM_NAME) != CLAIM_VALUE or claims.get('exp') != EXPIRES):
        raise BrokerAuthLibraryUnavailable()
