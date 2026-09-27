"""Best-effort advisory fleet readiness POST to the hub (C2, controller side).

Posts POST /v1/fleet/readiness. Off by default. Reads each controller's
state-store document and optional Cloud Run execution get (bounded); never
reads the hub's runcrew_fleet_state / fleet-readiness store. Failures are
counted, sanitized log events only and must not affect CAS, launch, or tick
results.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from fleet_controller import SAFE_CODE, map_readiness_phase


READINESS_PATH = '/v1/fleet/readiness'
SCHEMA = 1
# Mirrored from runcrew/agent_hub/core.py (FLEET/AGENTS), enforced by
# runcrew/agent_hub/fleet_readiness.py (validate_document).
HUB_AGENTS = ('codex', 'claude', 'cursor', 'copilot', 'grok')
# Short timeout so a hung hub cannot stall the controller tick.
POST_TIMEOUT_SECONDS = 2.5
# Bound store.read / cloud.get under the Runtime lock so a hung backend cannot
# stall every fleet tick; skip the publish when either call times out.
SNAPSHOT_TIMEOUT_SECONDS = 1.0
MAX_AGENTS = 16
MAX_AGENT_KEY = 32
# Mirrored from runcrew/agent_hub/fleet_readiness.py (MAX_REASON_CODE, MAX_EXECUTION_ID,
# _REASON_CODE, _EXECUTION_ID).
MAX_REASON_CODE = 64
MAX_EXECUTION_ID = 64
AGENT_KEY = re.compile(r'[a-z][a-z0-9_]{0,31}')
REASON_CODE = re.compile(r'[a-z0-9_]{1,64}')
EXECUTION_ID = re.compile(r'[A-Za-z0-9._:-]{1,64}')
# Fields that must never appear in a readiness body (secrets / grants / raw state).
FORBIDDEN_PAYLOAD_KEYS = frozenset({
    'grant', 'grant_sha256', 'token', 'password', 'secret', 'credential',
    'credentials', 'authorization', 'RUNCREW_EXECUTION_GRANT',
})
DOCUMENT_KEYS = frozenset({'schema', 'published_at', 'ttl_seconds', 'agents'})
AGENT_ENTRY_KEYS = frozenset({'phase', 'since', 'reason_code', 'execution'})
READINESS_PHASES = frozenset({
    'idle', 'launching', 'pending_start', 'starting', 'ready', 'blocked', 'unknown',
})

ENABLED_ENV = 'RUNCREW_FLEET_READINESS_PUBLISH'
HUB_URL_ENV = 'RUNCREW_HUB_URL'
HUB_URL_FALLBACK_ENV = 'HUB_URL'
TOKEN_ENV = 'RUNCREW_HUB_CONTROLLER_TOKEN'
TICK_SECONDS_ENV = 'RUNCREW_TICK_SECONDS'
DEFAULT_TICK_SECONDS = 60
# Hub POST /v1/fleet/readiness accepts manager or fleet; the controller token's
# principal is fleet, so X-Hub-Agent must be fleet (see runcrew/agent_hub/server.py).
HUB_AGENT_HEADER = 'fleet'
# Hub reason_code is required and must match REASON_CODE; use this when the
# slot has no error (hub has no null form for reason_code on POST).
DEFAULT_REASON_CODE = 'ok'


class NoRedirect(HTTPRedirectHandler):
    # Pattern from cca/agent-hub/agent_hub/worker.py — build_opener requires an
    # HTTPRedirectHandler subclass, not a bare object with redirect_request.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never forward a controller credential to another host.
        return None


def ttl_for_tick_interval(tick_seconds):
    """ttl_seconds = 3 x tick interval, clamped to the contract's 30..600."""
    if type(tick_seconds) is not int or tick_seconds < 1:
        tick_seconds = DEFAULT_TICK_SECONDS
    return max(30, min(600, 3 * tick_seconds))


def slot_agent_key(controller):
    """Hub agents[] key: the provider name accepted by the hub contract."""
    return controller.policy.profile.provider


def _opaque_execution_id(state, execution=None):
    """Short opaque execution id, or None. Never a grant or full resource body."""
    for candidate in (
        state.get('execution_uid') if isinstance(state, dict) else None,
        (execution or {}).get('uid') if isinstance(execution, dict) else None,
    ):
        if isinstance(candidate, str) and EXECUTION_ID.fullmatch(candidate) and len(candidate) <= MAX_EXECUTION_ID:
            return candidate
    name = state.get('execution') if isinstance(state, dict) else None
    if isinstance(name, str) and '/' in name:
        tail = name.rsplit('/', 1)[-1]
        if EXECUTION_ID.fullmatch(tail) and len(tail) <= MAX_EXECUTION_ID:
            return tail
    return None


def _reason_code(state):
    """Hub requires reason_code as [a-z0-9_]{1,64}; no null on POST."""
    if not isinstance(state, dict):
        return DEFAULT_REASON_CODE
    code = state.get('error')
    if isinstance(code, str) and REASON_CODE.fullmatch(code) and len(code) <= MAX_REASON_CODE:
        return code
    return DEFAULT_REASON_CODE


def agent_readiness_entry(state, execution=None, *, clock=time.time):
    """One agents[<name>] object for the hub contract. Exact keys only."""
    phase = 'unknown'
    since = int(clock())
    if isinstance(state, dict):
        slot_phase = state.get('phase')
        if isinstance(slot_phase, str):
            phase = map_readiness_phase(slot_phase, execution)
        updated = state.get('updated_at')
        if type(updated) is int and updated > 0:
            since = updated
    return {
        'phase': phase,
        'since': since,
        'reason_code': _reason_code(state),
        'execution': _opaque_execution_id(state, execution),
    }


def build_readiness_document(agents, *, published_at, ttl_seconds):
    """Build the POST body. Raises ValueError if the contract shape is violated."""
    if type(published_at) is not int or published_at < 0:
        raise ValueError('published_at_invalid')
    if type(ttl_seconds) is not int or not 30 <= ttl_seconds <= 600:
        raise ValueError('ttl_seconds_invalid')
    if not isinstance(agents, dict) or len(agents) > MAX_AGENTS:
        raise ValueError('agents_invalid')
    clean = {}
    for name, entry in agents.items():
        if (not isinstance(name, str) or name not in HUB_AGENTS
                or not AGENT_KEY.fullmatch(name) or len(name) > MAX_AGENT_KEY):
            raise ValueError('agent_key_invalid')
        if not isinstance(entry, dict) or set(entry) != AGENT_ENTRY_KEYS:
            raise ValueError('agent_entry_invalid')
        if entry['phase'] not in READINESS_PHASES:
            raise ValueError('phase_invalid')
        if type(entry['since']) is not int or entry['since'] < 0:
            raise ValueError('since_invalid')
        reason = entry['reason_code']
        if not (isinstance(reason, str) and REASON_CODE.fullmatch(reason)
                and len(reason) <= MAX_REASON_CODE):
            raise ValueError('reason_code_invalid')
        execution = entry['execution']
        if execution is not None and not (isinstance(execution, str)
                                          and EXECUTION_ID.fullmatch(execution)
                                          and len(execution) <= MAX_EXECUTION_ID):
            raise ValueError('execution_invalid')
        clean[name] = {
            'phase': entry['phase'],
            'since': entry['since'],
            'reason_code': reason,
            'execution': execution,
        }
    document = {
        'schema': SCHEMA,
        'published_at': published_at,
        'ttl_seconds': ttl_seconds,
        'agents': clean,
    }
    assert set(document) == DOCUMENT_KEYS
    return document


def document_from_slots(slot_states, *, executions=None, published_at=None, ttl_seconds=None,
                        tick_seconds=DEFAULT_TICK_SECONDS, clock=time.time):
    """slot_states: {agent: controller state dict}. executions: optional {agent: execution dict}."""
    executions = executions or {}
    agents = {}
    for name, state in slot_states.items():
        agents[name] = agent_readiness_entry(state, executions.get(name), clock=clock)
    return build_readiness_document(
        agents,
        published_at=int(clock()) if published_at is None else published_at,
        ttl_seconds=ttl_for_tick_interval(tick_seconds) if ttl_seconds is None else ttl_seconds,
    )


def _log_failure(log, code, failures):
    """Counted, sanitized event. Fixed code only; no URLs, tokens, or bodies."""
    if not isinstance(code, str) or not SAFE_CODE.fullmatch(code):
        code = 'unrecorded'
    failures['count'] = failures.get('count', 0) + 1
    log(json.dumps({
        'kind': 'runcrew_fleet_readiness_publish',
        'status': 'failed',
        'reason': code,
        'failures': failures['count'],
    }, separators=(',', ':')))


def post_readiness(hub_url, token, document, *, timeout=POST_TIMEOUT_SECONDS, opener=None):
    """POST the document. Raises on transport/HTTP failure (caller catches)."""
    if not isinstance(hub_url, str) or not isinstance(token, str) or not hub_url or not token:
        raise ValueError('hub_credential_absent')
    parsed = urlsplit(hub_url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')):
        raise ValueError('hub_url_invalid')
    if '\r' in token or '\n' in token:
        raise ValueError('hub_token_invalid')
    origin = hub_url.rstrip('/')
    body = json.dumps(document, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    request = Request(
        origin + READINESS_PATH,
        data=body,
        headers={
            'Content-Type': 'application/json',
            'X-Hub-Token': token,
            'X-Hub-Agent': HUB_AGENT_HEADER,
        },
        method='POST',
    )
    client = opener or build_opener(ProxyHandler({}), NoRedirect())
    with client.open(request, timeout=timeout) as response:
        response.read(4096)


class FleetReadinessPublisher:
    """Optional post-tick publisher. Disabled unless enabled and hub credentials exist."""

    def __init__(self, *, enabled=False, hub_url='', token='', tick_seconds=DEFAULT_TICK_SECONDS,
                 clock=time.time, post=None, log=print, timeout=POST_TIMEOUT_SECONDS):
        self.enabled = bool(enabled)
        self.hub_url = hub_url if isinstance(hub_url, str) else ''
        self.token = token if isinstance(token, str) else ''
        self.tick_seconds = tick_seconds if type(tick_seconds) is int else DEFAULT_TICK_SECONDS
        self.clock = clock
        self._post = post or post_readiness
        self.log = log
        self.timeout = timeout
        self.failures = {'count': 0}
        self.posts = []

    @classmethod
    def from_env(cls, environ=None, **kwargs):
        env = os.environ if environ is None else environ
        enabled = env.get(ENABLED_ENV) == '1'
        hub_url = env.get(HUB_URL_ENV) or env.get(HUB_URL_FALLBACK_ENV) or ''
        token = env.get(TOKEN_ENV) or ''
        raw_tick = env.get(TICK_SECONDS_ENV, '')
        tick_seconds = DEFAULT_TICK_SECONDS
        if isinstance(raw_tick, str) and re.fullmatch(r'[0-9]{2,4}', raw_tick):
            tick_seconds = int(raw_tick)
        return cls(enabled=enabled, hub_url=hub_url, token=token, tick_seconds=tick_seconds, **kwargs)

    def active(self):
        return bool(self.enabled and self.hub_url and self.token)

    def publish_slots(self, slot_states, *, executions=None):
        """Publish one readiness document for all given slots. Best-effort; never raises."""
        if not self.active():
            return None
        try:
            document = document_from_slots(
                slot_states,
                executions=executions,
                tick_seconds=self.tick_seconds,
                clock=self.clock,
            )
            self._post(self.hub_url, self.token, document, timeout=self.timeout)
            self.posts.append(document)
            return document
        except ValueError as error:
            code = str(error)
            _log_failure(self.log, code if SAFE_CODE.fullmatch(code) else 'payload_invalid', self.failures)
            return None
        except HTTPError as error:
            code = 'hub_http_other'
            if type(error.code) is int:
                code = {
                    400: 'hub_http_400', 401: 'hub_http_401', 403: 'hub_http_403',
                    404: 'hub_http_404', 409: 'hub_http_409', 413: 'hub_http_413',
                }.get(error.code, 'hub_http_other')
                if 500 <= error.code <= 599:
                    code = 'hub_http_5xx'
            _log_failure(self.log, code, self.failures)
            return None
        except (URLError, OSError, TimeoutError):
            _log_failure(self.log, 'hub_unreachable', self.failures)
            return None
        except Exception:
            _log_failure(self.log, 'publish_failed', self.failures)
            return None


def _call_with_timeout(fn, timeout_seconds):
    """Run fn in a daemon thread; return (value, timed_out)."""
    box = {'value': None, 'error': None}

    def run():
        try:
            box['value'] = fn()
        except Exception as error:
            box['error'] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        return None, True
    if box['error'] is not None:
        raise box['error']
    return box['value'], False


def publish_after_tick(controllers, publisher, *, snapshot_timeout=SNAPSHOT_TIMEOUT_SECONDS):
    """After a multi-slot tick: one POST covering every controller. Best-effort.

    Intended for the fleet Runtime tick loop. Controllers are not mutated.
    Store/cloud reads are bounded; on timeout the publish is skipped.
    """
    if publisher is None or not publisher.active():
        return None
    keyed_controllers = [(slot_agent_key(controller), controller) for controller in controllers]
    if len({name for name, _ in keyed_controllers}) != len(keyed_controllers):
        _log_failure(publisher.log, 'agent_key_collision', publisher.failures)
        return None
    slot_states = {}
    executions = {}
    for name, controller in keyed_controllers:
        try:
            state_pair, timed_out = _call_with_timeout(controller.store.read, snapshot_timeout)
            if timed_out:
                _log_failure(publisher.log, 'snapshot_timeout', publisher.failures)
                return None
            state, _ = state_pair
        except Exception:
            state = {'phase': 'unknown', 'updated_at': int(publisher.clock())}
        if state is None:
            state = {'phase': 'idle', 'updated_at': int(publisher.clock())}
        slot_states[name] = state
        execution_name = state.get('execution') if isinstance(state, dict) else None
        if isinstance(execution_name, str) and execution_name:
            try:
                execution, timed_out = _call_with_timeout(
                    lambda n=execution_name, c=controller: c.cloud.get(n),
                    snapshot_timeout,
                )
                if timed_out:
                    _log_failure(publisher.log, 'snapshot_timeout', publisher.failures)
                    return None
                if execution is not None:
                    executions[name] = execution
            except Exception:
                pass
    return publisher.publish_slots(slot_states, executions=executions)
