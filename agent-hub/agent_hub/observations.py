"""Bounded single-machine observations for the private dashboard.

Authentication belongs to the server's dedicated observer-only route. Local
process observations never authenticate a model, worker or provider account.
Display prose is regenerated from fixed IDs; raw receipt/prompt/config text is
neither stored nor echoed. Submitted provider timestamps remain authoritative
for freshness, even when the collector sends a new heartbeat.
"""
from copy import deepcopy
import json
import math
import re

from .core import HubError
from .telemetry import LABELS, ROSTER, STALE_SECONDS, USAGE_STALE_SECONDS, iso, timestamp


MAX_OBSERVATION_BYTES = 100_000
ACTIVITY = {
    'copilot-relay': ('copilot', 'Codex + Copilot relay', 'A local collaboration relay receipt was observed.'),
    'grok-review': ('grok', 'Grok architecture review', 'A saved architecture review was observed.'),
    'claude-mcp': ('claude', 'Claude verification report', 'A saved verification report was observed.'),
    'cursor-sdk': ('cursor', 'Cursor SDK review', 'A bounded Cursor SDK collaboration receipt was observed.'),
}
MILESTONES = {
    'local': 'Local collaboration hub', 'domain': 'New Google organization',
    'cloud': 'Google Cloud with login', 'plugin': 'Private ChatGPT app · two accounts',
    'research': 'Measured improvement pipeline', 'retina': 'Retina backup import',
}


def _require(condition):
    if not condition:
        raise HubError('Observation does not match the bounded display schema')


def _fields(value, required):
    _require(isinstance(value, dict) and set(value) == set(required))


def _text(value, maximum=1000):
    _require(isinstance(value, str) and len(value) <= maximum and not any(ord(char) < 32 for char in value))
    return value


def _choice(value, choices):
    _require(isinstance(value, str) and value in choices)
    return value


def _number(value, maximum=1e15, *, nullable=True, integer=False, minimum=0):
    if value is None and nullable:
        return None
    _require(type(value) is int if integer else type(value) in (int, float))
    _require(math.isfinite(value) and minimum <= value <= maximum)
    return value


def _time(value, now, *, future=False):
    observed = timestamp(value)
    _require(0 <= observed <= (1e11 if future else now + 60))
    return iso(observed)


def _rows(value, maximum):
    _require(isinstance(value, list) and len(value) <= maximum)
    return value


def _unique(rows, field):
    _require(len({row[field] for row in rows}) == len(rows))
    return rows


def _usage(value, now):
    common = ('provider', 'scope', 'metric', 'label', 'used', 'remaining', 'limit', 'unit',
              'observed_at', 'source', 'status', 'error')
    _require(isinstance(value, dict))
    metric = _choice(value.get('metric'), ('quota_percent', 'credits'))
    _fields(value, common + (('window_minutes', 'resets_at') if metric == 'quota_percent' else ()))
    _require(value['provider'] == 'codex' and value['scope'] == 'account' and value['source'] == 'provider_api')
    _require(value['error'] is None)
    _text(value['label'], 120)
    state = _choice(value['status'], ('available', 'stale'))
    observed = _time(value['observed_at'], now)
    result = {'provider': 'codex', 'scope': 'account', 'metric': metric,
              'source': 'provider_api', 'observed_at': observed, 'status': state, 'error': None}
    if metric == 'quota_percent':
        used = _number(value['used'], 100, nullable=False)
        remaining = _number(value['remaining'], 100, nullable=False)
        _require(type(value['limit']) in (int, float) and value['limit'] == 100 and value['unit'] == '%')
        _require(abs(used + remaining - 100) < 1e-6)
        minutes = _number(value['window_minutes'], 525600, nullable=False, integer=True, minimum=1)
        reset = _time(value['resets_at'], now, future=True)
        _require(timestamp(reset) > timestamp(observed))
        result.update(label='Weekly allowance' if minutes == 10080 else f'{minutes}-minute allowance',
                      used=used, remaining=remaining, limit=100, unit='%', window_minutes=minutes, resets_at=reset)
    else:
        _require(value['used'] is None and value['limit'] is None and value['unit'] == 'credits')
        result.update(label='Extra credits', used=None, limit=None, unit='credits',
                      remaining=_number(value['remaining'], nullable=False))
    return result


def validate_observation(data, now):
    """Project exactly the LocalCollector schema into safe display facts."""
    try:
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise HubError('Observation must be bounded finite JSON') from None
    _require(len(encoded) <= MAX_OBSERVATION_BYTES)
    _fields(data, ('machine_id', 'inventory', 'machine', 'activity', 'milestones', 'collector', 'local_usage'))
    alias = data['machine_id']
    _require(isinstance(alias, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,32}', alias) is not None)
    collector = data['collector']
    _fields(collector, ('status', 'observed_at', 'interval_seconds', 'source'))
    _require(collector['source'] == 'local-machine')
    result = {'machine_id': alias, 'collector': {
        'status': _choice(collector['status'], ('ok', 'unavailable')),
        'observed_at': _time(collector['observed_at'], now), 'source': 'local-machine',
        'interval_seconds': _number(collector['interval_seconds'], 300, nullable=False, integer=True, minimum=1)}}
    inventory = []
    for row in _rows(data['inventory'], len(ROSTER)):
        _fields(row, ('agent_id', 'label', 'installed', 'process_count', 'runtime_status', 'checked_at', 'note'))
        agent = _choice(row['agent_id'], ROSTER)
        _text(row['label'], 100)
        _text(row['note'])
        _require(type(row['installed']) is bool)
        count = _number(row['process_count'], 10000, integer=True)
        state = _choice(row['runtime_status'], ('unknown', 'running', 'stopped'))
        _require(state == ('unknown' if count is None else 'running' if count else 'stopped'))
        inventory.append({'agent_id': agent, 'label': LABELS[agent], 'installed': row['installed'],
            'process_count': count, 'runtime_status': state, 'checked_at': _time(row['checked_at'], now),
            'note': 'Local process observation; does not confirm a connected task worker.'})
    result['inventory'] = _unique(inventory, 'agent_id')
    machine = data['machine']
    _fields(machine, ('status', 'observed_at', 'platform', 'logical_cpus', 'ram_total_gb', 'ram_used_gb',
                      'ram_percent', 'disk_free_gb', 'cpu_percent'))
    result['machine'] = {'status': _choice(machine['status'], ('ok', 'unknown')),
        'observed_at': _time(machine['observed_at'], now),
        'platform': _choice(machine['platform'], ('Windows', 'nt', 'posix', 'linux', 'darwin')),
        'logical_cpus': _number(machine['logical_cpus'], 8192, integer=True, minimum=1)}
    for field in ('ram_total_gb', 'ram_used_gb', 'disk_free_gb', 'ram_percent', 'cpu_percent'):
        result['machine'][field] = _number(machine[field], 100 if field.endswith('percent') else 1e8)
    total, used = machine['ram_total_gb'], machine['ram_used_gb']
    _require(total is None or used is None or used <= total)
    activity = []
    for row in _rows(data['activity'], len(ACTIVITY)):
        _fields(row, ('id', 'agent_id', 'status', 'title', 'detail', 'observed_at', 'source'))
        identifier = _choice(row['id'], ACTIVITY)
        agent, title, detail = ACTIVITY[identifier]
        _require(row['agent_id'] == agent and row['source'] == 'saved collaboration receipt')
        _text(row['title'], 200)
        _text(row['detail'])
        state = _choice(row['status'], ('completed', 'blocked'))
        activity.append({'id': identifier, 'agent_id': agent, 'status': state, 'title': title,
                         'detail': detail if state == 'completed' else 'The local collector reports this collaboration is blocked.',
                         'observed_at': _time(row['observed_at'], now), 'source': 'saved collaboration receipt'})
    result['activity'] = _unique(activity, 'id')
    milestones = []
    for row in _rows(data['milestones'], len(MILESTONES)):
        _fields(row, ('id', 'title', 'status', 'detail'))
        identifier = _choice(row['id'], MILESTONES)
        _text(row['title'], 200)
        _text(row['detail'])
        state = _choice(row['status'], ('ready', 'in_progress', 'blocked'))
        milestones.append({'id': identifier, 'title': MILESTONES[identifier], 'status': state,
                           'detail': {'ready': 'Reported ready by the configured local collector.',
                                      'in_progress': 'Local collector reports this work is in progress.',
                                      'blocked': 'Local collector reports that a required setup step is outstanding.'}[state]})
    result['milestones'] = _unique(milestones, 'id')
    usage = [_usage(row, now) for row in _rows(data['local_usage'], 9)]
    _require(len({(row['metric'], row.get('window_minutes')) for row in usage}) == len(usage))
    result['local_usage'] = usage
    return result


def report_observation(hub, data):
    now = hub.clock()
    observation = validate_observation(data, now)
    hub.store.put_observation({'received_at': now, 'data': observation})
    return {'accepted': True, 'received_at': iso(now), 'scope': 'single_machine'}


def enrich_status(hub, snapshot):
    """Add the latest observer's facts without changing worker authentication."""
    record = hub.store.get_observation()
    if record is None:
        return snapshot
    now = hub.clock()
    local = deepcopy(record['data'])
    received = record['received_at']
    stale = now - received > STALE_SECONDS or now - timestamp(local['collector']['observed_at']) > STALE_SECONDS
    result = deepcopy(snapshot)
    for field in ('inventory', 'machine', 'activity', 'milestones', 'collector'):
        result[field] = local[field]
    result['collector'].update(received_at=iso(received), machine_id=local['machine_id'], scope='single_machine')
    if stale:
        result['collector']['status'] = 'stale'
    machine_stale = stale or now - timestamp(local['machine']['observed_at']) > STALE_SECONDS
    if machine_stale:
        result['machine']['status'] = 'stale'
    for row in result['inventory']:
        if stale or now - timestamp(row['checked_at']) > STALE_SECONDS:
            row['runtime_status'] = 'stale'
    usage = list(result.get('usage', []))
    for entry in local['local_usage']:
        if (now - timestamp(entry['observed_at']) > USAGE_STALE_SECONDS
                or ('resets_at' in entry and now >= timestamp(entry['resets_at']))):
            entry['status'] = 'stale'
        def same(row):
            return all(row.get(key) == entry.get(key) for key in ('provider', 'scope', 'metric', 'window_minutes'))
        existing = [row for row in usage if same(row)]
        if any(row.get('observed_at') and timestamp(row['observed_at']) > timestamp(entry['observed_at']) for row in existing):
            continue
        usage = [row for row in usage if not same(row)] + [entry]
    result['usage'] = usage
    alerts = list(result.get('alerts', []))
    if stale:
        alerts.append({'id': 'local-collector-stale', 'severity': 'warning', 'kind': 'collector_stale',
                       'title': 'Local machine observations are stale',
                       'message': 'No current local-machine observation is available; displayed values are last known.',
                       'agent_id': None, 'observed_at': local['collector']['observed_at']})
    memory = local['machine'].get('ram_percent')
    if not machine_stale and memory is not None and memory >= 90:
        alerts.append({'id': 'local-memory', 'severity': 'warning', 'kind': 'capacity',
                       'title': 'Local machine memory is nearly full',
                       'message': f'{memory}% RAM is in use. This can prevent agent processes from starting.',
                       'agent_id': None, 'observed_at': local['machine']['observed_at']})
    result['alerts'] = alerts
    if alerts and 'hub' in result:
        result['hub']['status'] = 'degraded'
    return result
