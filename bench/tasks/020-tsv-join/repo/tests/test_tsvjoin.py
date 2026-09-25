import unittest
from tsvjoin import dump_tsv, join_tables, load_tsv


class TestTsvJoin(unittest.TestCase):
    def test_load(self):
        headers, rows = load_tsv('id\tname\n1\tAda\n')
        self.assertEqual(headers, ['id', 'name'])
        self.assertEqual(rows, [{'id': '1', 'name': 'Ada'}])

    def test_inner_join(self):
        lh, lr = ['id', 'n'], [{'id': '1', 'n': 'a'}]
        rh, rr = ['id', 'x'], [{'id': '1', 'x': 'z'}]
        headers, rows = join_tables(lh, lr, rh, rr, 'id', how='inner')
        self.assertEqual(rows[0]['n'], 'a')
        self.assertEqual(rows[0]['x'], 'z')


if __name__ == "__main__":
    unittest.main()
