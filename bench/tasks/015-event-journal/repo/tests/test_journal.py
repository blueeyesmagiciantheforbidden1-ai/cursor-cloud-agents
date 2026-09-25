import tempfile
import unittest
from pathlib import Path

from eventjournal import EventStore, decode_event, encode_event


class TestJournal(unittest.TestCase):
    def test_encode_has_keys(self):
        line = encode_event(1, {'a': 1})
        seq, payload = decode_event(line)
        self.assertEqual(seq, 1)
        self.assertEqual(payload, {'a': 1})

    def test_append_read(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'events.jsonl'
            store = EventStore(path)
            s1 = store.append({'x': 1})
            self.assertEqual(s1, 1)
            rows = store.read_from(1)
            self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
