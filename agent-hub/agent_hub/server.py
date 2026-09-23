"""Small authenticated HTTP API, suitable for a private Cloud Run service."""
import hmac
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .core import AGENTS, DEFAULT_AGENTS, Hub, HubError
from .mcp import NO_CONTENT, SUPPORTED_PROTOCOLS, handle_json
from .store import FirestoreStore, MissingRoom, SQLiteStore
from .telemetry import report_worker, status_snapshot


class QwenRequestError(HubError):
    def __init__(self, error):
        super().__init__('Qwen request stopped: '+error.code, 409)
        self.invocation_id = error.invocation_id
        self.charge_status = error.charge_status


def load_tokens(value):
    tokens = json.loads(value)
    required = {'manager', *DEFAULT_AGENTS}
    optional = {'status', 'grok'}
    if not isinstance(tokens, dict) or not required <= set(tokens) or not set(tokens) <= required | optional:
        raise ValueError('HUB_TOKENS_JSON must define manager and the original four agents; status and grok are optional distinct tokens')
    if any(not isinstance(token, str) or not token.isascii() or not 32 <= len(token) <= 256
           or any(ord(character) < 33 or ord(character) > 126 for character in token)
           for token in tokens.values()):
        raise ValueError('Each hub token must contain 32 to 256 printable ASCII characters without spaces')
    if len(set(tokens.values())) != len(tokens):
        raise ValueError('Each principal needs a distinct token')
    return tokens


def make_handler(hub, tokens, *, allowed_mcp_origins=(), qwen=None):
    allowed_mcp_origins = frozenset(allowed_mcp_origins)
    class Handler(BaseHTTPRequestHandler):
        server_version = 'AgentHub/0.1'

        def log_message(self, format_string, *args):
            # Avoid recording prompts, credentials, or query parameters.
            return

        def send_json(self, status, value):
            payload = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def actor(self):
            supplied = self.headers.get('X-Hub-Token', '')
            if not supplied.isascii():
                raise HubError('Authentication required', 401)
            principal = None
            for name, token in tokens.items():
                if hmac.compare_digest(supplied, token):
                    principal = name
            if principal is None:
                raise HubError('Authentication required', 401)
            claimed = self.headers.get('X-Hub-Agent')
            if claimed is not None and claimed != principal:
                raise HubError('Agent identity does not match token', 403)
            return principal

        def raw_body(self):
            if self._body_consumed:
                return self._cached_body
            if self.headers.get('Transfer-Encoding'):
                raise HubError('Chunked requests are not supported')
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise HubError('Invalid Content-Length')
            if not 0 <= length <= 100000:
                raise HubError('Request body too large', 413)
            payload = self.rfile.read(length)
            self._body_consumed = True
            self._cached_body = payload
            if len(payload) != length:
                raise HubError('Incomplete request body')
            return payload

        def drain_error_body(self):
            # Authentication can fail before route/body parsing. Closing a
            # socket with a valid POST body still unread can reset the connection
            # on Windows and discard the denial response. Consume only the same
            # bounded Content-Length accepted by raw_body, without parsing or
            # dispatching it. Oversized/chunked/slow input closes promptly.
            self.close_connection = True
            if self._body_consumed:
                return
            try:
                self.connection.settimeout(2)
                self.raw_body()
            except (HubError, OSError, TimeoutError):
                pass
            finally:
                self.connection.settimeout(15)

        def body(self):
            try:
                data = json.loads(self.raw_body() or b'{}')
            except (ValueError, UnicodeDecodeError):
                raise HubError('Invalid JSON')
            if not isinstance(data, dict):
                raise HubError('JSON body must be an object')
            return data

        def route(self):
            path = urlsplit(self.path).path
            if self.command == 'GET' and path == '/healthz':
                return {'status': 'ok'}
            actor = self.actor()
            if path == '/v1/status' and self.command == 'GET':
                if actor not in ('manager', 'status'):
                    raise HubError('A manager or status credential is required', 403)
                snapshot = status_snapshot(hub)
                snapshot['qwen'] = {'configured': qwen is not None,
                                    'connection_verified': False,
                                    'profile': qwen.profile.public() if qwen is not None else None}
                return snapshot
            if actor == 'status':
                # Keep the dashboard principal outside all task/MCP/report
                # dispatch, including future routes added below this point.
                raise HubError('Status credential permits only GET /v1/status', 403)
            if path == '/v1/qwen/completions' and self.command == 'POST':
                if actor != 'manager':
                    raise HubError('Only the manager can authorize Qwen requests', 403)
                data = self.body()
                if set(data) != {'invocation_id', 'messages', 'mode'}:
                    raise HubError('Qwen requests require invocation_id, messages and mode')
                if qwen is None:
                    raise HubError('Qwen account connection is not configured', 503)
                from .qwen import QwenError
                try:
                    return qwen.complete(data['messages'], invocation_id=data['invocation_id'], mode=data['mode'])
                except QwenError as exc:
                    raise QwenRequestError(exc) from None
            if path == '/v1/workers/report' and self.command == 'POST':
                return report_worker(hub, actor, self.body())
            if path == '/v1/learning' and self.command == 'POST':
                return hub.learn(actor, self.body())
            if path == '/v1/learning/read' and self.command == 'POST':
                data = self.body()
                if 'workspace' not in data or set(data) - {'workspace', 'cursor', 'limit'}:
                    raise HubError('workspace is required; cursor and limit are optional')
                return hub.lessons(actor, **data)
            if path == '/mcp':
                if self.command != 'POST':
                    raise HubError('Method not allowed', 405)
                origin = self.headers.get('Origin')
                if origin is not None and origin not in allowed_mcp_origins:
                    raise HubError('Origin is not allowed', 403)
                protocol = self.headers.get('MCP-Protocol-Version')
                if protocol is not None and protocol not in SUPPORTED_PROTOCOLS:
                    raise HubError('Unsupported MCP protocol version', 400)
                return handle_json(hub, actor, self.raw_body())
            if path == '/v1/rooms':
                if self.command == 'GET':
                    return {'rooms': hub.list(actor)}
                if self.command == 'POST':
                    return hub.create(actor, self.body())
            if self.command == 'POST' and path == '/v1/tasks/claim':
                self.body()
                return hub.claim(actor)
            claim = re.fullmatch(r'/v1/rooms/([a-f0-9]{32})/claim', path)
            if claim and self.command == 'POST':
                if self.body():
                    raise HubError('Exact-room claim takes an empty body')
                return hub.claim(actor, claim.group(1))
            match = re.fullmatch(r'/v1/rooms/([a-f0-9]{32})(?:/(cancel|retry))?', path)
            if match:
                room_id, operation = match.groups()
                if self.command == 'GET' and operation is None:
                    return hub.get(actor, room_id)
                if self.command == 'POST' and operation:
                    self.body()
                    return getattr(hub, operation)(actor, room_id)
            match = re.fullmatch(r'/v1/tasks/([a-f0-9]{32})/(heartbeat|complete)', path)
            if match and self.command == 'POST':
                room_id, operation = match.groups()
                data = self.body()
                if operation == 'heartbeat':
                    return hub.heartbeat(actor, room_id, data.get('lease_token'))
                return hub.complete(actor, room_id, data)
            raise HubError('Not found', 404)

        def dispatch(self):
            self._body_consumed = False
            self._cached_body = b''
            self.connection.settimeout(15)
            try:
                result = self.route()
                if result is NO_CONTENT:
                    self.send_response(202)
                    self.send_header('Content-Length', '0')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    return
                self.send_json(200, result)
            except QwenRequestError as exc:
                self.drain_error_body()
                self.send_json(exc.status, {'error':str(exc), 'invocation_id':exc.invocation_id,
                                            'charge_status':exc.charge_status})
            except HubError as exc:
                self.drain_error_body()
                self.send_json(exc.status, {'error': str(exc)})
            except MissingRoom:
                self.drain_error_body()
                self.send_json(404, {'error': 'Collaboration not found'})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return
            except Exception as exc:
                # Error class only; provider diagnostics can contain credentials.
                print(json.dumps({'event': 'request_failed', 'type': type(exc).__name__}), file=sys.stderr)
                self.drain_error_body()
                self.send_json(500, {'error': 'Internal error'})

        do_GET = dispatch
        do_POST = dispatch

    return Handler


def main():
    tokens = load_tokens(os.environ.get('HUB_TOKENS_JSON', '{}'))
    backend = os.environ.get('HUB_BACKEND', 'sqlite')
    if os.environ.get('K_SERVICE') and backend != 'firestore':
        raise ValueError('Cloud Run requires HUB_BACKEND=firestore; SQLite would lose queued work')
    if backend == 'firestore':
        store = FirestoreStore(os.environ.get('GOOGLE_CLOUD_PROJECT'),
                               os.environ.get('HUB_FIRESTORE_COLLECTION', 'agent_hub_rooms'),
                               database=os.environ.get('HUB_FIRESTORE_DATABASE', '(default)'))
    elif backend == 'sqlite':
        store = SQLiteStore(os.environ.get('HUB_SQLITE_PATH', 'agent-hub.sqlite3'))
    else:
        raise ValueError('Unknown HUB_BACKEND')
    qwen = None
    if os.environ.get('HUB_QWEN_ENABLED') == '1':
        if backend != 'firestore' or not os.environ.get('K_SERVICE'):
            raise ValueError('The hosted Qwen connection requires the cloud Firestore budget ledger')
        from .qwen import QwenAdapter, QwenProfile
        from .qwen_ledger import FirestoreQwenLedger
        ledger = FirestoreQwenLedger(store.client)
        qwen = QwenAdapter(os.environ.get('QWEN_API_KEY', ''), reserve=ledger.reserve,
                           reconcile=ledger.reconcile, profile=QwenProfile(max_completion_tokens=128))
    host = os.environ.get('HUB_BIND', '0.0.0.0' if os.environ.get('K_SERVICE') else '127.0.0.1')
    allowed_origins = tuple(value.strip() for value in os.environ.get('HUB_MCP_ALLOWED_ORIGINS', '').split(',') if value.strip())
    from .frontier_client import FrontierClient, SERVICE_URL
    frontier = FrontierClient(SERVICE_URL) if SERVICE_URL else None
    server = ThreadingHTTPServer((host, int(os.environ.get('PORT', '8080'))),
                                 make_handler(Hub(store, frontier=frontier), tokens, allowed_mcp_origins=allowed_origins, qwen=qwen))
    server.daemon_threads = True
    print(json.dumps({'event': 'listening', 'host': host, 'port': server.server_port, 'backend': backend}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
