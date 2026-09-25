import unittest

from csvquery import select


class TestSelectHidden(unittest.TestCase):
    def test_where_filter(self):
        src = 'id,status\n1,ok\n2,bad\n3,ok\n'
        out = select(src, ['id'], where=('status', 'ok'))
        lines = [ln for ln in out.strip().splitlines()]
        self.assertEqual(lines[0], 'id')
        self.assertEqual(lines[1:], ['1', '3'])

    def test_missing_column_keyerror(self):
        with self.assertRaises(KeyError):
            select('a,b\n1,2\n', ['a', 'z'])

    def test_missing_where_column_keyerror(self):
        with self.assertRaises(KeyError):
            select('a,b\n1,2\n', ['a'], where=('nope', '1'))

    def test_header_only(self):
        out = select('a,b\n', ['b', 'a'])
        self.assertEqual(out.strip().splitlines(), ['b,a'])

    def test_quoting(self):
        src = 'name,note\n"A, B","x""y"\n'
        out = select(src, ['name', 'note'])
        rows = list(__import__('csv').reader(__import__('io').StringIO(out)))
        self.assertEqual(rows[1], ['A, B', 'x"y'])

    def test_column_order(self):
        out = select('a,b,c\n1,2,3\n', ['c', 'a'])
        self.assertEqual(out.strip().splitlines()[0], 'c,a')
        self.assertEqual(out.strip().splitlines()[1], '3,1')


if __name__ == '__main__':
    unittest.main()
