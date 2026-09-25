import unittest
from rollup import summarize


class TestSummarize(unittest.TestCase):
    def test_basic(self):
        rows = [
            {"dept": "a", "n": 1, "m": 2},
            {"dept": "b", "n": 3, "m": 4},
            {"dept": "a", "n": 5, "m": 6},
        ]
        out = summarize(rows, "dept", ["n", "m"])
        self.assertEqual(out, [
            {"dept": "a", "n": 6, "m": 8},
            {"dept": "b", "n": 3, "m": 4},
        ])

    def test_empty(self):
        self.assertEqual(summarize([], "dept", ["n"]), [])


if __name__ == "__main__":
    unittest.main()
