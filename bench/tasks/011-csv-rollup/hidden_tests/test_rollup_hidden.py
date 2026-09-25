import copy
import unittest
from rollup import summarize


class TestRollupHidden(unittest.TestCase):
    def test_sorted_groups(self):
        rows = [
            {"g": "z", "v": 1},
            {"g": "a", "v": 2},
            {"g": "m", "v": 3},
        ]
        out = summarize(rows, "g", ["v"])
        self.assertEqual([r["g"] for r in out], ["a", "m", "z"])

    def test_missing_group_key(self):
        with self.assertRaises(KeyError):
            summarize([{"v": 1}], "g", ["v"])

    def test_missing_value_key(self):
        with self.assertRaises(KeyError):
            summarize([{"g": "a", "v": 1}], "g", ["v", "w"])

    def test_type_error_str(self):
        with self.assertRaises(TypeError):
            summarize([{"g": "a", "v": "x"}], "g", ["v"])

    def test_type_error_bool(self):
        with self.assertRaises(TypeError):
            summarize([{"g": "a", "v": True}], "g", ["v"])

    def test_no_mutation(self):
        rows = [{"g": "a", "v": 1}, {"g": "a", "v": 2}]
        snapshot = copy.deepcopy(rows)
        summarize(rows, "g", ["v"])
        self.assertEqual(rows, snapshot)

    def test_float_sum(self):
        rows = [{"g": 1, "v": 0.5}, {"g": 1, "v": 1.5}]
        out = summarize(rows, "g", ["v"])
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]["v"], 2.0)


if __name__ == "__main__":
    unittest.main()
