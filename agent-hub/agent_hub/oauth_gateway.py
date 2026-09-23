"""Request-driven OAuth boundary for the IAM-private RunCrew manager MCP API."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from urllib.parse import parse_qsl, urlsplit

from .mcp import MCPClient, MANAGER_TOOLS, MAX_INPUT_BYTES, NO_CONTENT, SUPPORTED_PROTOCOLS
from .oauth_runtime import https_origin, load_oauth_service
from .worker import Config

SCOPE = 'hub:manage'
READ_ONLY = frozenset(('hub_status', 'hub_get', 'hub_list', 'hub_lessons', 'hub_frontier_get'))


class InputError(ValueError):
    pass


def form_fields(payload):
    try:
        pairs = parse_qsl(payload.decode('utf-8'), keep_blank_values=True,
                          strict_parsing=True, max_num_fields=20)
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError()
        return dict(pairs)
    except (ValueError, UnicodeError):
        raise InputError('Malformed or duplicate form fields') from None


def make_server(host, port, *, service=None, upstream=None, authorization_origin=None):
    if service is not None:
        issuer = https_origin(service.config.issuer)
        authorization_origin = https_origin(authorization_origin)
        challenge = 'Bearer resource_metadata="'+issuer+'/.well-known/oauth-protected-resource", scope="'+SCOPE+'"'
    else:
        issuer, challenge = None, None

    class Handler(BaseHTTPRequestHandler):
        server_version = 'MyHeroGateway'
        sys_version = ''

        def log_message(self, *_):
            return  # Never log request URLs, authorization codes, or headers.

        def reply(self, status, value, headers=None):
            body = b'' if value is NO_CONTENT else json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Pragma', 'no-cache')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def read_body(self, limit):
            values = self.headers.get_all('Content-Length', [])
            if (len(values) != 1 or not re.fullmatch(r'[0-9]+', values[0])
                    or self.headers.get('Transfer-Encoding')):
                raise InputError('A single Content-Length is required')
            size = int(values[0])
            if not 0 < size <= limit:
                raise InputError('Request body exceeds its limit')
            self.connection.settimeout(10)
            payload = self.rfile.read(size)
            if len(payload) != size:
                raise InputError('Incomplete request body')
            return payload

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/healthz':
                return self.reply(200, {'service': 'myhero-gateway',
                                        'oauth_configured': service is not None})
            if service is None:
                return self.reply(503, {'error': 'temporarily_unavailable'})
            if path in ('/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp'):
                return self.reply(200, service.protected_resource_metadata())
            if path == '/.well-known/oauth-authorization-server':
                return self.reply(200, service.metadata(issuer, authorization_origin+'/oauth/authorize'))
            return self.reply(405 if path == '/mcp' else 404, {'error': 'not_found'})

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            path = urlsplit(self.path).path
            if service is None:
                return self.reply(503, {'error': 'temporarily_unavailable'})
            try:
                origin_values = self.headers.get_all('Origin', [])
                if len(origin_values) > 1 or (origin_values and origin_values[0] not in (issuer, 'https://chatgpt.com')):
                    return self.reply(403, {'error': 'origin_not_allowed'})
                if path == '/mcp':
                    return self.mcp_request()
                if path not in ('/oauth/token', '/oauth/revoke'):
                    return self.reply(404, {'error': 'not_found'})
                if self.headers.get_content_type() != 'application/x-www-form-urlencoded':
                    return self.reply(415, {'error': 'unsupported_media_type'})
                # Only predefined public clients are supported in this bridge.
                # Reject unexpected Basic or other header credentials entirely.
                if self.headers.get_all('Authorization', []):
                    return self.reply(401, {'error': 'invalid_client'})
                form = form_fields(self.read_body(16_384))
                if path == '/oauth/token':
                    return self.reply(200, service.exchange(form))
                if (set(form) - {'token', 'token_type_hint', 'client_id'}
                        or form.get('client_id') not in {c.client_id for c in service.config.clients}
                        or not form.get('token')):
                    return self.reply(400, {'error': 'invalid_request'})
                service.revoke(form['token'], expected_client_id=form['client_id'])
                return self.reply(200, {})
            except InputError:
                self.close_connection = True
                return self.reply(400, {'error': 'invalid_request'})
            except Exception as error:
                # The core emits fixed protocol diagnostics; never relay storage
                # exceptions, upstream bodies, credential values, or stack traces.
                from .oauth_service import OAuthError
                if isinstance(error, OAuthError):
                    return self.reply(error.status, error.as_dict())
                return self.reply(503, {'error': 'temporarily_unavailable'})

        def mcp_request(self):
            if self.headers.get_content_type() != 'application/json':
                return self.reply(415, {'error': 'unsupported_media_type'})
            versions = self.headers.get_all('MCP-Protocol-Version', [])
            if len(versions) > 1 or (versions and versions[0] not in SUPPORTED_PROTOCOLS):
                return self.reply(400, {'error': 'invalid_request'})
            auth = self.headers.get_all('Authorization', [])
            if len(auth) != 1 or not re.fullmatch(r'Bearer [A-Za-z0-9._~-]{32,512}', auth[0]):
                return self.reply(401, {'error': 'invalid_token'}, {'WWW-Authenticate': challenge})
            try:
                service.authenticate(auth[0][7:], required_scope=SCOPE)
            except Exception as error:
                from .oauth_service import OAuthError
                if isinstance(error, OAuthError):
                    return self.reply(401, {'error': 'invalid_token'}, {'WWW-Authenticate': challenge})
                raise
            payload = self.read_body(MAX_INPUT_BYTES)
            try:
                message = json.loads(payload.decode('utf-8'),
                                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError, RecursionError):
                return self.reply(400, {'error': 'invalid_request'})
            if not isinstance(message, dict):
                return self.reply(400, {'error': 'invalid_request'})
            method = message.get('method')
            if method not in ('initialize', 'ping', 'tools/list', 'tools/call', 'notifications/initialized'):
                return self.reply(403, {'error': 'method_not_allowed'})
            if method == 'tools/call':
                params = message.get('params')
                if not isinstance(params, dict) or params.get('name') not in MANAGER_TOOLS:
                    return self.reply(403, {'error': 'tool_not_allowed'})
            if upstream is None:
                return self.reply(503, {'error': 'upstream_unavailable'})
            # MCPClient builds its own service identity and manager headers. No
            # incoming auth, IAP, X-Hub or serverless header is forwarded.
            result = upstream.rpc(message)
            if result is NO_CONTENT:
                return self.reply(202, NO_CONTENT)
            if method == 'tools/list' and 'result' in result:
                for tool in result['result'].get('tools', []):
                    schemes = [{'type': 'oauth2', 'scopes': [SCOPE]}]
                    tool['securitySchemes'] = schemes
                    tool['_meta'] = {'securitySchemes': schemes}
                    tool['annotations'] = {'readOnlyHint': tool['name'] in READ_ONLY,
                                           'destructiveHint': tool['name'] == 'hub_cancel',
                                           'openWorldHint': tool['name'] in ('hub_start', 'hub_retry')}
            return self.reply(200, result)

        def reject(self):
            return self.reply(405, {'error': 'method_not_allowed'})
        do_PUT = do_PATCH = do_DELETE = do_OPTIONS = reject

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    service = load_oauth_service()
    upstream = None
    if service is not None:
        upstream = MCPClient(Config(
            https_origin(os.environ['HUB_URL']), 'manager', os.environ['HUB_MANAGER_TOKEN'],
            'HUB_MANAGER_TOKEN', {}, cloud_run_auth_mode='metadata'))
    server = make_server('0.0.0.0', int(os.environ.get('PORT', '8080')), service=service,
                         upstream=upstream,
                         authorization_origin=os.environ.get('HUB_OAUTH_AUTHORIZATION_ORIGIN'))
    print('MyHero authentication gateway listening.', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
