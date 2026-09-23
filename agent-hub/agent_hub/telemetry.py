"""Bounded worker observations; unknown provider balances remain unknown."""
from datetime import datetime, timezone
import math
import os
import re

from .core import AGENTS, HubError

STALE_SECONDS = 90
USAGE_STALE_SECONDS = 900
ROSTER = ('codex', 'claude', 'cursor', 'copilot', 'grok')
LABELS = {'codex': 'Codex', 'claude': 'Claude Code', 'cursor': 'Cursor',
          'copilot': 'GitHub Copilot', 'grok': 'Grok Build'}
ERRORS = {'none': None, 'not_checked': 'Not checked',
          'not_exposed': 'The provider has not exposed this measurement',
          'auth_failed': 'Provider authentication failed',
          'task_failed': 'The last task failed', 'unavailable': 'Measurement unavailable'}


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def timestamp(value):
    try:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError()
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            raise ValueError()
        return date.timestamp()
    except (ValueError, OverflowError):
        raise HubError('observed_at must be a timezone-aware ISO timestamp') from None


def number(value, field):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1e15:
        raise HubError(f'{field} must be a finite nonnegative number or null')
    return value


def usage_entry(actor, value, now):
    if not isinstance(value, dict):
        raise HubError('Each usage entry must be an object')
    scope, metric, state = (value.get(key) for key in ('scope', 'metric', 'status'))
    source, error = value.get('source', 'provider_cli'), value.get('error', 'none')
    if scope not in ('account', 'session') or metric not in ('credits', 'tokens', 'requests', 'budget_usd'):
        raise HubError('Usage scope or metric is invalid')
    if state not in ('available', 'unavailable', 'auth_failed') or source not in ('provider_cli', 'provider_api', 'manual'):
        raise HubError('Usage status or source is invalid')
    if not isinstance(error, str) or error not in ERRORS:
        raise HubError('Unknown usage error code')
    observed = timestamp(value['observed_at']) if value.get('observed_at') else None
    if observed is not None and (observed > now + 60 or observed < 0):
        raise HubError('Usage observation time is invalid')
    if state == 'available' and observed is None:
        raise HubError('Available usage requires observed_at')
    used, limit, remaining = [number(value.get(key), key) for key in ('used', 'limit', 'remaining')]
    if state != 'available':
        used = limit = remaining = None
    elif all(item is None for item in (used, limit, remaining)):
        raise HubError('Available usage requires a measured value')
    if limit is not None and remaining is not None and remaining > limit:
        raise HubError('Remaining usage cannot exceed its limit')
    return {'provider': actor, 'scope': scope, 'metric': metric, 'used': used, 'limit': limit,
            'remaining': remaining, 'unit': 'USD' if metric == 'budget_usd' else metric,
            'source': source, 'observed_at': iso(observed) if observed is not None else None,
            'status': state, 'error': ERRORS[error]}


def report_worker(hub, actor, data):
    if actor not in AGENTS:
        raise HubError('Only authenticated workers can report telemetry', 403)
    if not isinstance(data, dict):
        raise HubError('Worker report must be an object')
    worker_id = data.get('worker_id', 'default')
    if not isinstance(worker_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', worker_id):
        raise HubError('worker_id must be a local configured label')
    state, auth = data.get('status', 'unknown'), data.get('auth_status', 'unknown')
    if state not in ('idle', 'busy', 'error', 'unknown', 'offline') or auth not in ('unknown', 'verified', 'failed'):
        raise HubError('Worker status or auth_status is invalid')
    room_id = data.get('current_room_id')
    if room_id is not None:
        if not isinstance(room_id, str) or not re.fullmatch(r'[a-f0-9]{32}', room_id):
            raise HubError('Invalid current_room_id')
        hub.get(actor, room_id)
    code = data.get('last_exit_code')
    if code is not None and (type(code) is not int or not -(2**31) <= code <= 2**32):
        raise HubError('Invalid last_exit_code')
    now = hub.clock()
    usage = data.get('usage', [])
    if not isinstance(usage, list) or len(usage) > 8:
        raise HubError('At most eight usage measurements may be reported')
    entry = {'id': actor + ':' + worker_id, 'agent_id': actor, 'worker_id': worker_id,
             'status': state, 'auth_status': auth, 'current_room_id': room_id,
             'last_exit_code': code, 'received_at': now,
             'usage': [usage_entry(actor, item, now) for item in usage]}
    hub.store.put_worker(entry['id'], entry)
    return {'accepted': True, 'observed_at': iso(now)}


def status_snapshot(hub):
    now = hub.clock()
    observations, rooms = hub.store.list_workers(limit=100), hub.list('manager')
    alerts, agents, usage = [], [], []
    def alert(identifier, severity, kind, title, message, agent=None, at=None):
        alerts.append({'id': identifier, 'severity': severity, 'kind': kind, 'title': title,
                       'message': message, 'agent_id': agent, 'observed_at': iso(now if at is None else at)})
    for agent_id in ROSTER:
        rows = [item for item in observations if item['agent_id'] == agent_id]
        workers = [{'id': row['worker_id'],
                    'status': 'offline' if now - row['received_at'] > STALE_SECONDS else row['status'],
                    'last_seen': iso(row['received_at']), 'auth_status': row['auth_status'],
                    'current_room_id': row['current_room_id'], 'last_exit_code': row['last_exit_code']}
                   for row in rows]
        latest = rows[0] if rows else None
        fresh = [row for row in rows if now - row['received_at'] <= STALE_SECONDS and row['status'] != 'offline']
        state = ('unconfigured' if not rows else 'offline' if not fresh else
                 'busy' if any(row['status'] == 'busy' for row in fresh) else
                 'unknown' if any(row['status'] in ('error', 'unknown') for row in fresh) else 'idle')
        auth = latest['auth_status'] if latest else 'unknown'
        error = 'Provider authentication failed' if auth == 'failed' else None
        agents.append({'id': agent_id, 'label': LABELS[agent_id], 'configured': bool(rows), 'status': state,
                       'last_seen': iso(latest['received_at']) if latest else None, 'auth_status': auth,
                       'current_room_id': next((row['current_room_id'] for row in fresh if row['status'] == 'busy'), None),
                       'last_exit_code': latest['last_exit_code'] if latest else None,
                       'error': error, 'workers': workers})
        if state == 'offline':
            alert(agent_id + '-offline', 'warning', 'worker_offline', LABELS[agent_id] + ' is offline',
                  'Worker stopped or no heartbeat arrived in the last 90 seconds.', agent_id, latest['received_at'])
        if auth == 'failed' and fresh:
            alert(agent_id + '-auth', 'critical', 'authentication', LABELS[agent_id] + ' needs sign-in', error, agent_id)
        if latest and latest['last_exit_code'] not in (None, 0):
            alert(agent_id + '-task', 'warning', 'task_error', LABELS[agent_id] + ' task failed',
                  'Check the last task result before retrying.', agent_id, latest['received_at'])
        measures = {}
        for row in rows:
            for original in row.get('usage', []):
                measure = dict(original)
                key = (measure['scope'], measure['metric'])
                if key in measures:
                    continue
                if measure['status'] == 'available' and now - timestamp(measure['observed_at']) > USAGE_STALE_SECONDS:
                    measure['status'] = 'stale'
                measures[key] = measure
        if ('account', 'credits') not in measures:
            measures[('account', 'credits')] = {'provider': agent_id, 'scope': 'account', 'metric': 'credits',
                'used': None, 'limit': None, 'remaining': None, 'unit': 'credits', 'source': None,
                'observed_at': None, 'status': 'unavailable', 'error': 'Account credit balance has not been connected'}
        usage.extend(measures.values())
        for measure in measures.values():
            if (measure['status'] == 'available' and measure['scope'] == 'account'
                    and measure['remaining'] is not None and measure['limit'] not in (None, 0)
                    and measure['remaining'] / measure['limit'] <= 0.1):
                alert(agent_id + '-low-' + measure['metric'], 'warning', 'usage_low', LABELS[agent_id] + ' usage is low',
                      'Ten percent or less remains in the reported account limit.', agent_id)
    counts, summaries = {}, []
    for room in rooms:
        counts[room['status']] = counts.get(room['status'], 0) + 1
        summaries.append({'id': room['id'], 'status': room['status'], 'agents': room['agents'],
                          'created_at': iso(room['created_at']), 'updated_at': iso(room['updated_at']),
                          'next_agent': room.get('next_agent'),
                          'current_agent': room['agents'][room['step'] % len(room['agents'])] if room['status'] == 'running' else None,
                          'completed_steps': room['step'], 'total_steps': len(room['agents']) * room['rounds'],
                          'rounds': room['rounds']})
        if room['status'] in ('failed', 'stalled'):
            alert('room-' + room['id'], 'warning', 'room_' + room['status'], 'Task ' + room['status'],
                  'Inspect this task in ChatGPT before retrying: ' + room['id'], at=room['updated_at'])
    return {'schema_version': 1, 'generated_at': iso(now),
            'hub': {'status': 'degraded' if alerts else 'ok', 'service': 'agent-hub',
                    'store': 'firestore' if type(hub.store).__name__ == 'FirestoreStore' else 'sqlite',
                    'deployment': 'cloud' if os.environ.get('K_SERVICE') else 'local'},
            'agents': agents, 'usage': usage, 'alerts': alerts,
            'queue': {'counts': counts, 'observed_rooms': len(rooms), 'window': 'latest 50 rooms'},
            'rooms': summaries}
