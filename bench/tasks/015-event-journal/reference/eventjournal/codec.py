import json


def encode_event(seq, payload):
    return json.dumps(
        {'payload': payload, 'seq': seq},
        separators=(',', ':'),
        sort_keys=True,
    ) + '\n'


def decode_event(line):
    try:
        data = json.loads(line)
    except json.JSONDecodeError as e:
        raise ValueError('invalid event') from e
    if not isinstance(data, dict) or 'seq' not in data or 'payload' not in data:
        raise ValueError('invalid event')
    seq = data['seq']
    payload = data['payload']
    if not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(payload, dict):
        raise ValueError('invalid event')
    return seq, payload
