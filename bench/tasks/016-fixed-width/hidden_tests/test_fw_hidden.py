import unittest
from fixedwidth import format_line, parse_line, parse_lines


class TestFWHidden(unittest.TestCase):
    def test_strip_fields(self):
        schema = [('a', 4), ('b', 3)]
        self.assertEqual(parse_line(' x  y  ', schema), {'a': 'x', 'b': 'y'})

    def test_too_short(self):
        schema = [('a', 4), ('b', 3)]
        with self.assertRaises(ValueError):
            parse_line('ab', schema)

    def test_ignore_excess(self):
        schema = [('a', 2)]
        self.assertEqual(parse_line('abcdef', schema), {'a': 'ab'})

    def test_newline_stripped(self):
        schema = [('a', 2), ('b', 2)]
        self.assertEqual(parse_line('abcd\n', schema), {'a': 'ab', 'b': 'cd'})

    def test_format_right_pad_truncate(self):
        schema = [('a', 4), ('b', 3)]
        self.assertEqual(format_line({'a': 'x', 'b': 'toolong'}, schema), 'x   too')

    def test_parse_lines_skips_empty(self):
        schema = [('a', 1)]
        self.assertEqual(parse_lines('a\n\nb\n', schema), [{'a': 'a'}, {'a': 'b'}])


if __name__ == "__main__":
    unittest.main()
