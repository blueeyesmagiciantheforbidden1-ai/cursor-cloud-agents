import copy
import json
import tempfile
import unittest
from pathlib import Path

from eventjournal import (
    EventStore,
    decode_event,
    encode_event,
    load_cursor,
    save_cursor,
)


class TestJournalHidden(unittest.TestCase):
    def test_encode_format(self):
        line = encode_event(2, {'b': 1, 'a': 2})
        self.assertTrue(line.endswith('\n'))
        self.assertEqual(line, '{"payload":{"a":2,"b":1},"seq":2}\n')

    def test_decode_bad(self):
        with self.assertRaises(ValueError):
            decode_event('{"seq": 1}')
        with self.assertRaises(ValueError):
            decode_event('{"seq": "x", "payload": {}}')

    def test_sequences_continue(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'e.jsonl'
            store = EventStore(path)
            self.assertEqual(store.append({'n': 1}), 1)
            self.assertEqual(store.append({'n': 2}), 2)
            self.assertEqual(store.read_from(2), [(2, {'n': 2})])

    def test_no_payload_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'e.jsonl'
            store = EventStore(path)
            payload = {'n': 1}
            snap = copy.deepcopy(payload)
            store.append(payload)
            self.assertEqual(payload, snap)

    def test_cursor_roundtrip_and_default(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'cur'
            self.assertEqual(load_cursor(path), 1)
            save_cursor(path, 9)
            self.assertEqual(path.read_text(encoding='utf-8'), '9\n')
            self.assertEqual(load_cursor(path), 9)

    def test_cursor_malformed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'cur'
            path.write_text('nope\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                load_cursor(path)


if __name__ == "__main__":
    unittest.main()
