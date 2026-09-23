"""Load explicitly configured private OAuth identities; never auto-enroll users."""
import json
import os
from urllib.parse import urlsplit


def https_origin(value):
    if (not isinstance(value, str) or not value
            or any(c.isspace() or ord(c) < 32 or 127 <= ord(c) < 160 for c in value)):
        raise ValueError('OAuth origin must be an HTTPS origin')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or parsed.port not in (None, 443)):
        raise ValueError('OAuth origin must be an HTTPS origin')
    return value.rstrip('/')


def load_oauth_service():
    raw = os.environ.get('HUB_OAUTH_CONFIG_JSON', '')
    if not raw:
        return None
    try:
        if len(raw.encode('utf-8')) > 16_384:
            raise ValueError()
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {'issuer', 'resource', 'clients', 'identities'}:
            raise ValueError()
        issuer = https_origin(data['issuer'])
        if data['resource'] != issuer + '/mcp':
            raise ValueError()
        if (not isinstance(data['identities'], list) or len(data['identities']) != 2
                or not isinstance(data['clients'], list) or not 1 <= len(data['clients']) <= 2):
            raise ValueError()
        from .oauth_service import OAuthClient, OAuthConfig, OAuthService, PinnedIdentity
        from .oauth_store import FirestoreOAuthStore
        clients = tuple(OAuthClient(item['client_id'], tuple(item['redirect_uris']))
                        for item in data['clients'] if set(item) == {'client_id', 'redirect_uris'})
        identities = tuple(PinnedIdentity(item['issuer'], item['subject'])
                           for item in data['identities'] if set(item) == {'issuer', 'subject'})
        if len(clients) != len(data['clients']) or len(identities) != 2:
            raise ValueError()
        config = OAuthConfig(issuer=issuer, resource=data['resource'], clients=clients, identities=identities)
        project = os.environ['GOOGLE_CLOUD_PROJECT']
        database = os.environ.get('HUB_OAUTH_DATABASE', 'runcrew-auth')
        if database != 'runcrew-auth':
            raise ValueError()
        return OAuthService(config, FirestoreOAuthStore(project, database=database))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('Private OAuth configuration is invalid; no identities were enrolled') from None
