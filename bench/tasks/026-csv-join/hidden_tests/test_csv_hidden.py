import copy
import unittest

from csvtools import read_rows, join_rows


class TestCsvHidden(unittest.TestCase):
    def test_read_empty(self):
        self.assertEqual(read_rows(''), [])
        self.assertEqual(read_rows('   '), [])

    def test_read_header_only(self):
        self.assertEqual(read_rows('id,name\n'), [])

    def test_inner_join(self):
        left = [{'id': '1', 'n': 'a'}, {'id': '2', 'n': 'b'}]
        right = [{'id': '2', 'v': 'x'}, {'id': '3', 'v': 'y'}]
        out = join_rows(left, right, 'id', how='inner')
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['n'], 'b')
        self.assertEqual(out[0]['v'], 'x')
        self.assertEqual(out[0]['id'], '2')

    def test_left_join_fills_none(self):
        left = [{'id': '1', 'n': 'a'}]
        right = [{'id': '9', 'v': 'z'}]
        out = join_rows(left, right, 'id', how='left')
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]['v'])
        self.assertEqual(out[0]['n'], 'a')

    def test_multi_match(self):
        left = [{'id': '1'}]
        right = [{'id': '1', 'v': 'a'}, {'id': '1', 'v': 'b'}]
        out = join_rows(left, right, 'id', how='inner')
        self.assertEqual([r['v'] for r in out], ['a', 'b'])

    def test_column_collision(self):
        left = [{'id': '1', 'name': 'L'}]
        right = [{'id': '1', 'name': 'R'}]
        out = join_rows(left, right, 'id', how='inner')
        self.assertEqual(out[0]['name'], 'L')
        self.assertEqual(out[0]['right_name'], 'R')

    def test_no_mutate(self):
        left = [{'id': '1', 'n': 'a'}]
        right = [{'id': '1', 'v': 'x'}]
        l2, r2 = copy.deepcopy(left), copy.deepcopy(right)
        join_rows(left, right, 'id')
        self.assertEqual(left, l2)
        self.assertEqual(right, r2)

    def test_bad_how(self):
        with self.assertRaises(ValueError):
            join_rows([{'id': '1'}], [{'id': '1'}], 'id', how='outer')

    def test_missing_key(self):
        with self.assertRaises(ValueError):
            join_rows([{'a': '1'}], [{'id': '1'}], 'id')


if __name__ == '__main__':
    unittest.main()
