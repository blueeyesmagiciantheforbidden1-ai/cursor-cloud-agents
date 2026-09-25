import copy
from pathlib import Path

from eventjournal.codec import decode_event, encode_event


class EventStore:
    def __init__(self, path):
        self.path = Path(path)

    def _last_seq(self):
        if not self.path.exists():
            return 0
        last = 0
        with self.path.open(encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    seq, _ = decode_event(line)
                    last = seq
        return last

    def append(self, payload):
        payload = copy.deepcopy(payload)
        seq = self._last_seq() + 1
        with self.path.open('a', encoding='utf-8') as f:
            f.write(encode_event(seq, payload))
        return seq

    def read_from(self, start_seq=1):
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                seq, payload = decode_event(line)
                if seq >= start_seq:
                    out.append((seq, payload))
        return out
