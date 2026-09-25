import unittest

from csvquery import select


class TestSelectVisible(unittest.TestCase):
    def test_project_columns(self):
        src = 'name,age,city\nAda,36,London\nBob,22,Paris\n'
        out = select(src, ['name', 'city'])
        self.assertIn('name,city', out.splitlines()[0])
        self.assertIn('Ada,London', out)
        self.assertIn('Bob,Paris', out)


if __name__ == '__main__':
    unittest.main()
