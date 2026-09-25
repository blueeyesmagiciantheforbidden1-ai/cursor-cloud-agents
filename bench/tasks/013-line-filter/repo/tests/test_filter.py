import unittest
from linefilter import filter_lines


class TestFilter(unittest.TestCase):
    def test_basic(self):
        lines = ["alpha", "beta", "alphabet"]
        self.assertEqual(filter_lines(lines, "alp"), ["alpha", "alphabet"])

    def test_ignore_case(self):
        lines = ["Alpha", "beta"]
        self.assertEqual(filter_lines(lines, "alp", ignore_case=True), ["Alpha"])


if __name__ == "__main__":
    unittest.main()
