"""Advisory memory consulted while a room is claimed.

The room engine only needs a stable key and a context object. Lesson storage
stays empty until a live usage ledger is connected.
"""


def state_key(workspace):
    return 'workspace:' + workspace


def select_state(state, workspace, prompt):
    if not isinstance(state, dict):
        state = {}
    return {
        'revision': state.get('revision', 0),
        'context_sha256': state.get('context_sha256', ''),
        'lessons': [],
    }


def record(hub, actor, data):
    from .core import HubError
    raise HubError('Lesson recording is not connected', 501)


def read(hub, actor, workspace, cursor='', limit=5):
    return {'lessons': [], 'cursor': ''}
