import copy
import tempfile
import unittest
from pathlib import Path

from tsvjoin import dump_tsv, join_tables, load_tsv
from tsvjoin.__main__ import main


class TestTsvJoinHidden(unittest.TestCase):
    def test_short_rows(self):
        headers, rows = load_tsv('a\tb\tc\n1\t2\n')
        self.assertEqual(rows[0], {'a': '1', 'b': '2', 'c': ''})

    def test_dump_trailing_newline(self):
        text = dump_tsv(['a'], [{'a': '1'}])
        self.assertTrue(text.endswith('\n'))

    def test_left_join_and_rename(self):
        lh = ['id', 'name']
        lr = [{'id': '1', 'name': 'L'}, {'id': '2', 'name': 'M'}]
        rh = ['id', 'name']
        rr = [{'id': '1', 'name': 'R'}]
        headers, rows = join_tables(lh, lr, rh, rr, 'id', how='left')
        self.assertEqual(headers, ['id', 'name', 'name_right'])
        self.assertEqual(rows[0]['name'], 'L')
        self.assertEqual(rows[0]['name_right'], 'R')
        self.assertEqual(rows[1]['name_right'], '')

    def test_multi_match(self):
        lh, lr = ['k'], [{'k': 'a'}]
        rh, rr = ['k', 'v'], [{'k': 'a', 'v': '1'}, {'k': 'a', 'v': '2'}]
        _, rows = join_tables(lh, lr, rh, rr, 'k', how='inner')
        self.assertEqual(len(rows), 2)

    def test_no_mutation(self):
        lr = [{'k': 'a', 'x': '1'}]
        rr = [{'k': 'a', 'y': '2'}]
        snap = copy.deepcopy(lr)
        join_tables(['k', 'x'], lr, ['k', 'y'], rr, 'k', how='inner')
        self.assertEqual(lr, snap)

    def test_bad_how_and_missing_key(self):
        with self.assertRaises(ValueError):
            join_tables(['k'], [], ['k'], [], 'k', how='outer')
        with self.assertRaises(KeyError):
            join_tables(['a'], [], ['k'], [], 'k', how='inner')

    def test_cli_how_left(self):
        import io
        import sys
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as d:
            left = Path(d) / 'l.tsv'
            right = Path(d) / 'r.tsv'
            left.write_text('id\tv\n1\ta\n2\tb\n', encoding='utf-8')
            right.write_text('id\tw\n1\tz\n', encoding='utf-8')
            buf = io.StringIO()
            with redirect_stdout(buf):
                main([str(left), str(right), '--key', 'id', '--how', 'left'])
            out = buf.getvalue()
            self.assertIn('2\tb\t', out)


if __name__ == "__main__":
    unittest.main()
