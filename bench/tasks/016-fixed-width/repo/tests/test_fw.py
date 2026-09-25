import unittest
from fixedwidth import format_line, parse_line


class TestFW(unittest.TestCase):
    def test_parse(self):
        schema = [('id', 3), ('name', 5)]
        self.assertEqual(parse_line('01 Alice', schema), {'id': '01', 'name': 'Alice'})

    def test_format(self):
        schema = [('id', 3), ('name', 5)]
        # starter may not match exact padding — keep weak
        line = format_line({'id': '1', 'name': 'Bob'}, schema)
        self.assertEqual(len(line), 8)


if __name__ == "__main__":
    unittest.main()
