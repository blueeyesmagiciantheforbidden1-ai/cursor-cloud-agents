import unittest

from textutil import slugify


class TestSlugifyHidden(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify('Hello World'), 'hello-world')

    def test_punctuation_removed(self):
        self.assertEqual(slugify('Hello, World!'), 'hello-world')

    def test_strip_hyphens(self):
        self.assertEqual(slugify('  --Hello--  '), 'hello')

    def test_collapse_hyphens(self):
        self.assertEqual(slugify('a---b'), 'a-b')

    def test_empty(self):
        self.assertEqual(slugify('!!!'), '')
        self.assertEqual(slugify(''), '')

    def test_digits_kept(self):
        self.assertEqual(slugify('Release 2.0'), 'release-20')

    def test_mixed_whitespace(self):
        self.assertEqual(slugify('a\tb\nc'), 'a-b-c')


if __name__ == '__main__':
    unittest.main()
