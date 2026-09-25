import json


def encode_event(seq, payload):
    # Bug: no newline; unsorted keys; spaces
    return json.dumps({'payload': payload, 'seq': seq})


def decode_event(line):
    data = json.loads(line)
    return data['seq'], data['payload']
