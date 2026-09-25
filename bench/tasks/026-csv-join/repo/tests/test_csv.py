import unittest

from csvtools import read_rows, join_rows


class TestCsvSmoke(unittest.TestCase):
    def test_read_basic(self):
        text = 'id,name\n1,a\n2,b\n'
        rows = read_rows(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['id'], '1')


if __name__ == '__main__':
    unittest.main()
