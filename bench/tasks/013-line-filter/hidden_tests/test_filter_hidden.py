import copy
import io
import unittest
from contextlib import redirect_stdout
from linefilter import filter_lines
from linefilter.__main__ import main


class TestFilterHidden(unittest.TestCase):
    def test_invert(self):
        lines = ["a", "b", "c"]
        self.assertEqual(filter_lines(lines, "b", invert=True), ["a", "c"])

    def test_empty_pattern(self):
        lines = ["a", "b"]
        self.assertEqual(filter_lines(lines, ''), list(lines))
        self.assertEqual(filter_lines(lines, '', invert=True), [])

    def test_no_mutation(self):
        lines = ["aa", "bb"]
        snap = copy.deepcopy(lines)
        filter_lines(lines, "a")
        self.assertEqual(lines, snap)

    def test_cli_invert_ignore(self):
        stdin = io.StringIO("Foo\nbar\nBAZ\n")
        buf = io.StringIO()
        import sys
        old = sys.stdin
        try:
            sys.stdin = stdin
            with redirect_stdout(buf):
                main(['-i', '-v', 'ba'])
        finally:
            sys.stdin = old
        self.assertEqual(buf.getvalue(), "Foo\n")


if __name__ == "__main__":
    unittest.main()
