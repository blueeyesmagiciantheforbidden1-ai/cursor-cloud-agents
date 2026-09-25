import tempfile
import unittest
from pathlib import Path

from minitemplate import TemplateLoader, get_filter, render


class TestTemplateHidden(unittest.TestCase):
    def test_whitespace_and_chain(self):
        self.assertEqual(
            render('{{ name | strip | title }}', {'name': '  ada lovelace '}),
            'Ada Lovelace',
        )

    def test_missing_name(self):
        with self.assertRaises(KeyError):
            render('{{nope}}', {})

    def test_bad_filter(self):
        with self.assertRaises(ValueError):
            render('{{name|nope}}', {'name': 'x'})

    def test_len_filter(self):
        self.assertEqual(render('{{s|len}}', {'s': 'abcd'}), '4')
        self.assertEqual(get_filter('len')('xy'), '2')

    def test_loader_safety(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'ok.txt').write_text('{{n}}', encoding='utf-8')
            loader = TemplateLoader(root)
            self.assertEqual(loader.render('ok.txt', {'n': 'Z'}), 'Z')
            with self.assertRaises(ValueError):
                loader.load('../ok.txt')


if __name__ == "__main__":
    unittest.main()
