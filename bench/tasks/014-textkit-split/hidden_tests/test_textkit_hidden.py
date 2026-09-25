import importlib
import unittest
from textkit import normalize_ws, slugify, truncate


class TestTextkitHidden(unittest.TestCase):
    def test_modules_exist(self):
        n = importlib.import_module('textkit.normalize')
        s = importlib.import_module('textkit.slugify')
        t = importlib.import_module('textkit.truncate')
        self.assertTrue(hasattr(n, 'normalize_ws'))
        self.assertTrue(hasattr(s, 'slugify'))
        self.assertTrue(hasattr(t, 'truncate'))

    def test_normalize_tabs_newlines_strip(self):
        self.assertEqual(normalize_ws("  a\t\tb\nc  "), "a b c")

    def test_slugify_collapse_and_strip(self):
        self.assertEqual(slugify("Hello,  World!!!"), "hello-world")
        self.assertEqual(slugify("--Wow--"), "wow")

    def test_truncate_ellipsis_budget(self):
        self.assertEqual(truncate("abcdefghij", 6), "abc...")

    def test_truncate_max_len_too_small(self):
        with self.assertRaises(ValueError):
            truncate("abc", 2)

    def test_truncate_word_boundary(self):
        self.assertEqual(truncate("one two three", 10, word_boundary=True), "one two...")

    def test_truncate_word_boundary_fallback(self):
        self.assertEqual(truncate("supercalifragilistic", 8, word_boundary=True), "super...")


if __name__ == "__main__":
    unittest.main()
