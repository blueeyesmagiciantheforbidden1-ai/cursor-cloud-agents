from eventjournal.codec import decode_event, encode_event


class EventStore:
    def __init__(self, path):
        self.path = path

    def append(self, payload):
        # Bug: always starts at 1; mutates payload
        payload['touched'] = True
        seq = 1
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(encode_event(seq, payload))
            f.write('\n')
        return seq

    def read_from(self, start_seq=1):
        try:
            with open(self.path, encoding='utf-8') as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            if not line.strip():
                continue
            seq, payload = decode_event(line)
            if seq >= start_seq:
                out.append((seq, payload))
        return out
