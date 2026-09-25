import unittest
from inisections import dump, parse


class TestIni(unittest.TestCase):
    def test_simple(self):
        text = "[db]\nhost=localhost\nport=5432\n"
        data = parse(text)
        self.assertEqual(data["db"]["host"], "localhost")
        self.assertEqual(data["db"]["port"], "5432")

    def test_comment_and_blank(self):
        text = "# hi\n\n[a]\nx=1\n"
        self.assertEqual(parse(text), {"a": {"x": "1"}})


if __name__ == "__main__":
    unittest.main()
