"""Manager-only proxy for the separate private, bounded research service.

No provider credentials, shell commands, retries, or caller-selected destination.
The destination is pinned in the deployed source after its cloud URI is verified.
"""
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .core import HubError

# Set from the verified Cloud Run resource during deployment, not from tool input.
SERVICE_URL = ''
MAX_RESPONSE_BYTES = 64000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def metadata_identity(audience):
    request = Request('http://metadata.google.internal/computeMetadata/v1/instance/'
        'service-accounts/default/identity?audience=' + quote(audience, safe='') + '&format=full',
        headers={'Metadata-Flavor': 'Google'})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=5) as response:
            if response.headers.get('Metadata-Flavor') != 'Google':
                raise ValueError()
            token = response.read(16385).decode('ascii')
            if len(token) > 16384 or not re.fullmatch(r'[A-Za-z0-9_.-]+', token):
                raise ValueError()
            return token
    except Exception:
        raise HubError('Cloud research identity unavailable', 503) from None


class FrontierClient:
    def __init__(self, endpoint=SERVICE_URL, *, identity=metadata_identity, opener=None):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.run.app')
                or parsed.username or parsed.password or parsed.port is not None
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('Private Cloud Run research origin required')
        self.endpoint = endpoint
        self.identity = identity
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())

    def call(self, operation, data):
        if operation not in ('run', 'get'):
            raise HubError('Unknown research operation')
        payload = json.dumps(data, allow_nan=False, separators=(',', ':')).encode()
        if len(payload) > 4096:
            raise HubError('Research request is too large', 413)
        request = Request(self.endpoint + '/v1/research/' + operation, data=payload,
            headers={'Authorization': 'Bearer ' + self.identity(self.endpoint),
                     'Content-Type': 'application/json'}, method='POST')
        try:
            with self.opener.open(request, timeout=48 if operation == 'run' else 8) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError()
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except HTTPError as error:
            # Never forward upstream bodies: Google responses may contain identity details.
            if error.code in (400, 404, 409, 429):
                messages = {400: 'Invalid research request', 404: 'Research request not found',
                    409: 'Research request conflicts or requires reconciliation',
                    429: 'Research capacity is reserved or its daily allowance is exhausted'}
                raise HubError(messages[error.code], error.code) from None
            raise HubError('Cloud research service unavailable; inspect the same request ID before retrying', 503) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise HubError('Research outcome is unknown; use hub_frontier_get with the same request ID', 503) from None
