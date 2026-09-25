import unittest
from textkit import normalize_ws, slugify, truncate


class TestTextkit(unittest.TestCase):
    def test_normalize_basic(self):
        self.assertEqual(normalize_ws("a   b"), "a b")

    def test_slugify_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_truncate_basic(self):
        self.assertEqual(truncate("abcdef", 10), "abcdef")


if __name__ == "__main__":
    unittest.main()
