"""Bounded, attributed project memory. Peer agreement is not test evidence.

No model calls, embeddings, training, deployment or credential access occurs here.
Room messages are immutable sources. Managers can record existing contributions;
they cannot use this API to manufacture a contribution on an agent's behalf.
"""
import copy
import hashlib
import json
import re

from .core import AGENTS, HubError, utf8_size

MAX_LESSONS = 64
MAX_STATE_BYTES = 240_000
# Admission cannot consume reserved revocation space. Even an otherwise full
# document can retain one bounded objection per lesson, below Firestore's limit.
MAX_REVOCATION_STATE_BYTES = 800_000
CONTEXT_BYTES = 2000
CONTEXT_ITEMS = 4


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def state_key(workspace):
    if not isinstance(workspace, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', workspace):
        raise HubError('workspace must be a configured alias')
    return 'learning-' + workspace


def source(hub, actor, room_id, index):
    if actor not in ('manager', *AGENTS):
        raise HubError('A participant or manager credential is required', 403)
    if not isinstance(room_id, str) or not re.fullmatch(r'[a-f0-9]{32}', room_id):
        raise HubError('A valid source room is required')
    room = hub.get(actor, room_id)
    if type(index) is not int or not 0 <= index < len(room['messages']):
        raise HubError('source_message must identify an existing completed message')
    message = room['messages'][index]
    if actor != 'manager' and actor != message['agent']:
        raise HubError('Agents can attribute only their own completed messages', 403)
    receipt = dict(room_id=room_id, message_index=index, agent=message['agent'],
                   message_sha256=digest(message), exit_code=message['exit_code'])
    return room, message, receipt


def _text(value, name, limit=1200):
    if not isinstance(value, str) or not 1 <= utf8_size(value.strip(), name) <= limit:
        raise HubError(f'{name} must contain 1 to {limit} UTF-8 bytes')
    # Known credential markers are rejected as a backstop; this is not a general DLP classifier.
    if re.search(r'(?i)(?:-----BEGIN .*PRIVATE KEY|\bBearer\s+[A-Za-z0-9._-]{16}|\bsk-[A-Za-z0-9._-]{12}|\bgh[pousr]_[A-Za-z0-9]{12})', value):
        raise HubError('Credentials cannot be stored as project lessons')
    return value.strip()


def _fields(data, required, optional=()):
    if not isinstance(data, dict) or not set(required) <= set(data) or set(data) - set(required) - set(optional):
        raise HubError('Invalid learning operation fields')


def _mutate(hub, workspace, change, *, proposal_id=None, revocation=False):
    def apply(state):
        if not state:
            state.update(schema_version=1, revision=0, lessons={})
        before = copy.deepcopy(state)
        result = change(state)
        if state == before:
            return copy.deepcopy(result)
        state['revision'] += 1
        first_objection = (isinstance(revocation, tuple) and revocation[0] == 'dispute'
                           and before.get('lessons', {}).get(revocation[1], {}).get('status') in ('proposed', 'reviewed'))
        cap = MAX_REVOCATION_STATE_BYTES if revocation is True or first_objection else MAX_STATE_BYTES
        if len(json.dumps(state, ensure_ascii=False).encode()) > cap:
            raise HubError('Project memory is full; export and archive it before adding more', 409)
        return copy.deepcopy(result)
    key = state_key(workspace)
    if proposal_id:
        archived_key = key + '-archive-' + proposal_id
        def with_archive(states):
            if states[archived_key]:
                return copy.deepcopy(states[archived_key]['lesson'])
            return apply(states[key])
        return hub.store.mutate_states((key, archived_key), with_archive)
    return hub.store.mutate_state(key, apply)


def record(hub, actor, data):
    operation = data.get('operation') if isinstance(data, dict) else None
    if operation == 'propose':
        _fields(data, ('operation', 'room_id', 'source_message', 'text', 'kind'))
        if data['kind'] not in ('practice', 'project_fact', 'failure'):
            raise HubError('kind must be practice, project_fact, or failure')
        text = _text(data['text'], 'text')
        room, message, receipt = source(hub, actor, data['room_id'], data['source_message'])
        if text not in message['text']:
            raise HubError('A lesson must quote its completed source message exactly')
        if message['exit_code'] != 0 and data['kind'] != 'failure':
            raise HubError('Failed executions can supply failure notes only')
        identity = dict(workspace=room['workspace'], text=text, kind=data['kind'], source=receipt)
        lesson_id = digest(identity)
        def propose(state):
            if lesson_id in state['lessons']:
                return state['lessons'][lesson_id]
            if len(state['lessons']) >= MAX_LESSONS:
                raise HubError('Project memory entry limit reached; archive before adding more', 409)
            item = dict(identity, id=lesson_id, status='proposed', reviews=[],
                        recorded_by=actor, created_at=hub.clock(), updated_at=hub.clock(),
                        evidence_level='agent_report', model_weights_changed=False)
            state['lessons'][lesson_id] = item
            return item
        return _mutate(hub, room['workspace'], propose, proposal_id=lesson_id)
    if operation == 'review':
        _fields(data, ('operation', 'room_id', 'source_message', 'lesson_id', 'verdict', 'quote'))
        if data['verdict'] not in ('support', 'reject'):
            raise HubError('verdict must be support or reject')
        quote = _text(data['quote'], 'quote')
        room, message, receipt = source(hub, actor, data['room_id'], data['source_message'])
        if quote not in message['text'] or message['exit_code'] != 0:
            raise HubError('Reviews require an exact quote from a successful completed contribution')
        try:
            def unique_pairs(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError('Duplicate key')
                    value[key] = item
                return value
            structured = json.loads(quote, object_pairs_hook=unique_pairs)
        except (ValueError, RecursionError):
            raise HubError('Review quote must be a JSON object with lesson_id, verdict, and reason') from None
        if (not isinstance(structured, dict) or set(structured) != {'lesson_id', 'verdict', 'reason'}
                or structured['lesson_id'] != data['lesson_id'] or structured['verdict'] != data['verdict']
                or not isinstance(structured['reason'], str) or not structured['reason'].strip()
                or quote != message['text'].strip()):
            raise HubError('Review verdict must match the entire completed message as one structured JSON object')
        def review(state):
            lesson = state['lessons'].get(data['lesson_id'])
            if lesson is None:
                raise HubError('Lesson not found in this workspace', 404)
            if receipt['agent'] == lesson['source']['agent']:
                raise HubError('A different agent must review the lesson', 409)
            if receipt['room_id'] == lesson['source']['room_id'] and receipt['message_index'] <= lesson['source']['message_index']:
                raise HubError('The review must follow the source contribution')
            review_id = digest(dict(source=receipt, lesson_id=lesson['id']))
            all_reviews = lesson['reviews'] + ([lesson['terminal_rejection']] if lesson.get('terminal_rejection') else [])
            existing = next((r for r in all_reviews if r['id'] == review_id), None)
            if existing:
                if existing['verdict'] != data['verdict'] or existing['quote'] != quote:
                    raise HubError('An immutable review cannot be rewritten', 409)
                return lesson
            if lesson['status'] == 'retired':
                raise HubError('Retired lessons cannot be reactivated', 409)
            review_record = dict(id=review_id, source=receipt, quote=quote,
                                 verdict=data['verdict'], recorded_by=actor)
            if len(lesson['reviews']) >= len(AGENTS) * 2:
                if data['verdict'] != 'reject' or lesson.get('terminal_rejection'):
                    raise HubError('Review limit reached; existing rejection remains effective', 409)
                # Reserve an objection slot independently of positive review capacity.
                lesson['terminal_rejection'] = review_record
            else:
                lesson['reviews'].append(review_record)
            # One objection prevents automatic reuse; votes do not outweigh counterevidence.
            lesson['status'] = 'disputed' if lesson.get('terminal_rejection') or any(r['verdict'] == 'reject' for r in lesson['reviews']) else 'reviewed'
            lesson['evidence_level'] = 'peer_reviewed_not_test_verified'
            lesson['updated_at'] = hub.clock()
            return lesson
        if not isinstance(data['lesson_id'], str):
            raise HubError('lesson_id must be a string')
        return _mutate(hub, room['workspace'], review,
                       revocation=('dispute', data['lesson_id']) if data['verdict'] == 'reject' else False)
    if operation == 'archive':
        _fields(data, ('operation', 'workspace', 'lesson_id'))
        if actor != 'manager':
            raise HubError('Only the manager can archive project memory', 403)
        if not isinstance(data['lesson_id'], str) or not re.fullmatch(r'[a-f0-9]{64}', data['lesson_id']):
            raise HubError('lesson_id must be a valid digest')
        key = state_key(data['workspace'])
        archive_key = key + '-archive-' + data['lesson_id']
        def archive(states):
            if states[archive_key]:
                return copy.deepcopy(states[archive_key])
            state = states[key]
            item = state.get('lessons', {}).get(data['lesson_id'])
            if item is None:
                raise HubError('Lesson not found', 404)
            if item['status'] != 'retired':
                raise HubError('Retire the lesson before archiving it', 409)
            states[archive_key].update(schema_version=1, lesson=copy.deepcopy(item), archived_at=hub.clock())
            del state['lessons'][data['lesson_id']]
            state['revision'] += 1
            return copy.deepcopy(states[archive_key])
        return hub.store.mutate_states((key, archive_key), archive)
    if operation == 'reconnect':
        _fields(data, ('operation', 'workspace'))
        if actor not in ('manager', *AGENTS):
            raise HubError('A participant or manager credential is required', 403)
        workspace = data['workspace']

        def attach(state):
            if not state:
                state.update(schema_version=1, revision=0, lessons={})
            lessons = state.get('lessons', {})
            if not isinstance(lessons, dict):
                raise HubError('Project memory is unreadable', 409)
            # A brief Claude revocation closes the attach. Reconnecting reads
            # the same document; it does not retire, archive, or rewrite lessons.
            return {'workspace': workspace, 'revision': state.get('revision', 0),
                    'lessons': len(lessons), 'reconnected': True}

        attached = _mutate(hub, workspace, attach)
        stored = hub.store.get_state(state_key(workspace))
        lessons = stored.get('lessons', {})
        return {'workspace': workspace, 'revision': stored.get('revision', 0),
                'lessons': len(lessons) if isinstance(lessons, dict) else 0,
                'reconnected': attached.get('reconnected') is True}
    if operation == 'retire':
        _fields(data, ('operation', 'workspace', 'lesson_id'))
        if actor != 'manager':
            raise HubError('Only the manager can retire project memory', 403)
        if not isinstance(data['lesson_id'], str):
            raise HubError('lesson_id must be a string')
        def retire(state):
            lesson = state['lessons'].get(data['lesson_id'])
            if lesson is None:
                raise HubError('Lesson not found', 404)
            if lesson['status'] != 'retired':
                lesson.update(status='retired', updated_at=hub.clock())
            return lesson
        return _mutate(hub, data['workspace'], retire, revocation=True)
    raise HubError('operation must be propose, review, retire, archive, or reconnect')


def read(hub, actor, workspace, *, cursor='', limit=5):
    if actor != 'manager':
        raise HubError('Only the manager can browse project memory; workers receive task-scoped context', 403)
    if not isinstance(cursor, str) or cursor and not re.fullmatch(r'[a-f0-9]{64}', cursor):
        raise HubError('cursor must be empty or a lesson digest')
    if type(limit) is not int or not 1 <= limit <= 10:
        raise HubError('limit must be an integer from 1 to 10')
    state = hub.store.get_state(state_key(workspace))
    items = sorted((item for key, item in state.get('lessons', {}).items() if key > cursor), key=lambda item: item['id'])
    summaries = []
    for item in items[:limit]:
        summary = {key: copy.deepcopy(item[key]) for key in
                   ('id', 'text', 'kind', 'status', 'source', 'evidence_level', 'created_at', 'updated_at')}
        reviews = item['reviews'] + ([item['terminal_rejection']] if item.get('terminal_rejection') else [])
        # Full source outputs remain available via hub_get. Avoid duplicating
        # ten potentially escaped review quotes in both MCP content encodings.
        summary['reviews'] = [dict(source=r['source'], verdict=r['verdict']) for r in reviews]
        summaries.append(summary)
    return dict(workspace=workspace, revision=state.get('revision', 0),
                lessons=summaries, next_cursor=summaries[-1]['id'] if len(items) > limit else None,
                caveat='Attributed agent reports and peer reviews; agreement is not independent test evidence.')


def select(hub, workspace, prompt):
    state = hub.store.get_state(state_key(workspace))
    return select_state(state, workspace, prompt)


def select_state(state, workspace, prompt):
    terms = set(re.findall(r'[a-z0-9_]{3,}', prompt.lower()))
    candidates = []
    for item in state.get('lessons', {}).values():
        if item['status'] != 'reviewed':
            continue
        score = len(terms & set(re.findall(r'[a-z0-9_]{3,}', item['text'].lower())))
        if score:
            candidates.append((score, item))
    candidates.sort(key=lambda pair: (-pair[0], pair[1]['id']))
    selected = []
    for _, item in candidates:
        view = {key: copy.deepcopy(item[key]) for key in ('id', 'text', 'kind', 'source', 'evidence_level')}
        view['reviewers'] = sorted(set(r['source']['agent'] for r in item['reviews']))
        if len(json.dumps([*selected, view], ensure_ascii=False).encode()) <= CONTEXT_BYTES:
            selected.append(view)
        if len(selected) >= CONTEXT_ITEMS:
            break
    return dict(schema_version=1, workspace=workspace, revision=state.get('revision', 0),
                lessons=selected, context_sha256=digest(selected),
                authority='advisory_only', tests_verified=False)
