"""Explicit, bounded turn-taking. Expired jobs stall; they never rerun silently."""
import copy
import hashlib
import json
import re
import secrets
import time
import uuid

DEFAULT_AGENTS = ('codex', 'claude', 'cursor', 'copilot')
AGENTS = (*DEFAULT_AGENTS, 'grok')
MAX_OUTPUT = 16000
MAX_PROMPT = 8000
MAX_ATTEMPTS = 24
MAX_HISTORY_BYTES = 160 * 1024
LEASE_SECONDS = 45


class HubError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def utf8_size(value, field):
    try:
        return len(value.encode('utf-8'))
    except UnicodeEncodeError:
        raise HubError(f'{field} must contain valid Unicode')


def history_size(messages):
    # Include JSON escaping and metadata, so the HTTP representation is bounded too.
    return len(json.dumps(messages, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def public_room(room):
    result = copy.deepcopy(room)
    result.pop('lease', None)
    result.pop('completed_leases', None)
    return result


def expire(room, now):
    lease = room.get('lease')
    if room['status'] == 'running' and lease and (now >= lease['expires_at'] or now >= lease['deadline']):
        room.update(status='stalled', next_agent=None, updated_at=now)
        return True
    return False


class Hub:
    def __init__(self, store, clock=time.time, *, frontier=None):
        self.store = store
        self.clock = clock
        self.frontier = frontier

    def frontier_call(self, actor, operation, data):
        if actor != 'manager':
            raise HubError('Only the manager can access research experiments', 403)
        if self.frontier is None:
            raise HubError('Cloud research service is not configured', 503)
        return self.frontier.call(operation, data)

    def create(self, actor, data):
        if actor != 'manager':
            raise HubError('Only the manager can start a collaboration', 403)
        prompt = data.get('prompt')
        agents = data.get('agents', list(DEFAULT_AGENTS))
        workspace = data.get('workspace', 'default')
        rounds = data.get('rounds', 1)
        timeout = data.get('timeout_seconds', 300)
        purpose = data.get('purpose', 'project')
        if not isinstance(purpose, str) or purpose not in ('project', 'improvement'):
            raise HubError('purpose must be project or improvement')
        if purpose == 'improvement':
            # The 25% planner alone cannot reserve real subscription allowance.
            raise HubError('Model improvement is paused until live usage accounting and atomic budget reservations are connected', 503)
        if not isinstance(prompt, str) or not 1 <= utf8_size(prompt.strip(), 'prompt') <= MAX_PROMPT:
            raise HubError('prompt must contain 1 to 8000 UTF-8 bytes')
        if not isinstance(agents, list) or not agents or len(agents) > len(AGENTS) or any(agent not in AGENTS for agent in agents):
            raise HubError('agents must be a nonempty list of supported agent names')
        if len(set(agents)) != len(agents):
            raise HubError('agents must be unique')
        if type(rounds) is not int or not 1 <= rounds <= 3:
            raise HubError('rounds must be an integer from 1 to 3')
        if type(timeout) is not int or not 30 <= timeout <= 900:
            raise HubError('timeout_seconds must be an integer from 30 to 900')
        if not isinstance(workspace, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', workspace):
            raise HubError('workspace must be a configured local alias')
        now = self.clock()
        room = dict(id=uuid.uuid4().hex, prompt=prompt.strip(), agents=agents, rounds=rounds,
                    workspace=workspace, purpose=purpose, timeout_seconds=timeout, created_at=now, updated_at=now,
                    status='queued', step=0,
                    next_agent=None if workspace.lower().startswith('release_test_') else agents[0],
                    messages=[], lease=None,
                    completed_leases=[], attempts=0)
        self.store.create(room)
        return public_room(room)

    def get(self, actor, room_id):
        def change(room):
            if actor != 'manager' and actor not in room['agents']:
                raise HubError('Not a participant in this collaboration', 403)
            expire(room, self.clock())
            return public_room(room)

        return self.store.mutate(room_id, change)

    def list(self, actor):
        if actor != 'manager':
            raise HubError('Only the manager can list all collaborations', 403)
        return [self.get(actor, room['id']) for room in self.store.list()]

    def claim(self, actor, room_id=None):
        if actor not in AGENTS:
            raise HubError('Only an agent can claim work', 403)
        # Exact-room claims isolate release verification and targeted dispatch
        # from unrelated queued work. Older clients retain their queue behavior.
        candidates = [self.get(actor, room_id)] if room_id is not None else self.store.list(agent=actor)
        for candidate in candidates:
            if room_id is None and candidate['workspace'].lower().startswith('release_test_'):
                continue
            token = secrets.token_urlsafe(32)
            from .learning import select_state, state_key

            def change(room, learning_state):
                # Synthetic release checks must never reach ordinary model workers.
                if room_id is None and room['workspace'].lower().startswith('release_test_'):
                    return None
                reserved = room['workspace'].lower().startswith('release_test_')
                expected_agent = room['agents'][room['step'] % len(room['agents'])] if reserved else room.get('next_agent')
                if room['status'] != 'queued' or expected_agent != actor:
                    return None
                learning_context = select_state(learning_state, room['workspace'], room['prompt'])
                now = self.clock()
                attempts = room.get('attempts', len(room['completed_leases']))
                if attempts >= MAX_ATTEMPTS:
                    room.update(status='failed', next_agent=None, updated_at=now,
                                failure_reason='Attempt limit reached; start a new collaboration')
                    return None
                room['lease'] = dict(token=token, agent=actor, expires_at=now + LEASE_SECONDS,
                                     deadline=now + room['timeout_seconds'],
                                     learning_context=copy.deepcopy(learning_context))
                room.update(status='running', next_agent=None, attempts=attempts + 1, updated_at=now)
                return dict(room_id=room['id'], lease_token=token, prompt=room['prompt'],
                            messages=copy.deepcopy(room['messages']), workspace=room['workspace'],
                            timeout_seconds=room['timeout_seconds'], step=room['step'],
                            deadline=room['lease']['deadline'], server_time=now,
                            learning_context=copy.deepcopy(learning_context),
                            lease_expires_at=room['lease']['expires_at'])

            task = self.store.mutate_room_with_state(candidate['id'], state_key(candidate['workspace']), change)
            if task is not None:
                return {'task': task}
        return {'task': None}

    def heartbeat(self, actor, room_id, token):
        def change(room):
            now = self.clock()
            expire(room, now)
            if not self._owns(room, actor, token):
                return False
            room['lease']['expires_at'] = min(now + LEASE_SECONDS, room['lease']['deadline'])
            room['updated_at'] = now
            return {'active': True, 'deadline': room['lease']['deadline'],
                    'server_time': now, 'lease_expires_at': room['lease']['expires_at']}

        result = self.store.mutate(room_id, change)
        if not result:
            raise HubError('Task is no longer active for this lease; stop execution', 409)
        return result

    @staticmethod
    def _owns(room, actor, token):
        lease = room.get('lease')
        return bool(room['status'] == 'running' and lease and lease['agent'] == actor
                    and isinstance(token, str) and token.isascii()
                    and secrets.compare_digest(lease['token'], token))

    def complete(self, actor, room_id, data):
        token = data.get('lease_token')
        output = data.get('output')
        exit_code = data.get('exit_code')
        if (not isinstance(token, str) or not token.isascii() or not 1 <= len(token) <= 128
                or not isinstance(output, str) or type(exit_code) is not int):
            raise HubError('lease_token, output, and integer exit_code are required')
        if utf8_size(output, 'output') > MAX_OUTPUT:
            raise HubError('output exceeds 16000 UTF-8 bytes', 413)
        digest = hashlib.sha256(token.encode()).hexdigest()

        def change(room):
            if any(item['hash'] == digest and item['agent'] == actor for item in room['completed_leases']):
                return {'room_id': room_id, 'status': room['status']}
            now = self.clock()
            expire(room, now)
            if not self._owns(room, actor, token):
                return None
            message = dict(agent=actor, text=output, exit_code=exit_code,
                           step=room['step'], time=now)
            context = room['lease'].get('learning_context')
            if context and context.get('lessons'):
                message['learning_context'] = dict(revision=context['revision'],
                    context_sha256=context['context_sha256'], lesson_ids=[item['id'] for item in context['lessons']])
            history_exhausted = history_size([*room['messages'], message]) > MAX_HISTORY_BYTES
            if not history_exhausted:
                room['messages'].append(message)
            room['completed_leases'].append(dict(hash=digest, agent=actor))
            room['lease'] = None
            room['updated_at'] = now
            if history_exhausted:
                room.update(status='failed', next_agent=None, history_exhausted=True,
                            failure_reason='History byte limit reached; result was not stored. Start a new collaboration')
            elif exit_code != 0:
                room.update(status='failed', next_agent=None)
            else:
                room['step'] += 1
                if room['step'] >= room['rounds'] * len(room['agents']):
                    room.update(status='completed', next_agent=None)
                elif room.get('attempts', len(room['completed_leases'])) >= MAX_ATTEMPTS:
                    room.update(status='failed', next_agent=None,
                                failure_reason='Attempt limit reached; start a new collaboration')
                else:
                    room.update(status='queued', next_agent=None if room['workspace'].lower().startswith('release_test_')
                                else room['agents'][room['step'] % len(room['agents'])])
            return {'room_id': room_id, 'status': room['status']}

        result = self.store.mutate(room_id, change)
        if result is None:
            raise HubError('Task is no longer active for this lease; result not accepted', 409)
        return result

    def learn(self, actor, data):
        from .learning import record
        return record(self, actor, data)

    def lessons(self, actor, workspace, *, cursor='', limit=5):
        from .learning import read
        return read(self, actor, workspace, cursor=cursor, limit=limit)

    def cancel(self, actor, room_id):
        if actor != 'manager':
            raise HubError('Only the manager can cancel', 403)

        def change(room):
            if room['status'] not in ('completed', 'cancelled'):
                room.update(status='cancelled', next_agent=None, lease=None, updated_at=self.clock())
            return public_room(room)

        return self.store.mutate(room_id, change)

    def retry(self, actor, room_id):
        if actor != 'manager':
            raise HubError('Only the manager can retry after checking the previous attempt', 403)

        def change(room):
            expire(room, self.clock())
            if room['status'] not in ('failed', 'stalled'):
                raise HubError('Only failed or stalled work can be retried', 409)
            if room.get('attempts', len(room['completed_leases'])) >= MAX_ATTEMPTS:
                raise HubError('Attempt limit reached; start a new collaboration', 409)
            if room.get('history_exhausted'):
                raise HubError('History byte limit reached; start a new collaboration', 409)
            room.update(status='queued', next_agent=None if room['workspace'].lower().startswith('release_test_')
                        else room['agents'][room['step'] % len(room['agents'])],
                        lease=None, updated_at=self.clock())
            room.pop('failure_reason', None)
            return public_room(room)

        return self.store.mutate(room_id, change)
