import unittest
from inisections import dump, parse


class TestIniHidden(unittest.TestCase):
    def test_keys_before_section(self):
        with self.assertRaises(ValueError):
            parse("a=1\n[s]\nb=2\n")

    def test_duplicate_last_wins(self):
        data = parse("[s]\na=1\na=2\n")
        self.assertEqual(data["s"]["a"], "2")

    def test_split_first_eq_only(self):
        data = parse("[s]\nurl=a=b=c\n")
        self.assertEqual(data["s"]["url"], "a=b=c")

    def test_strip_key_and_value(self):
        data = parse("[s]\n  key  =  val  \n")
        self.assertEqual(data["s"]["key"], "val")

    def test_dump_format(self):
        text = dump({"a": {"x": "1"}, "b": {"y": "2"}})
        self.assertEqual(text, "[a]\nx=1\n\n[b]\ny=2")

    def test_case_sensitive(self):
        data = parse("[A]\nk=1\n[a]\nk=2\n")
        self.assertEqual(set(data), {"A", "a"})

    def test_invalid_line(self):
        with self.assertRaises(ValueError):
            parse("[s]\nnot-an-assignment\n")


if __name__ == "__main__":
    unittest.main()
