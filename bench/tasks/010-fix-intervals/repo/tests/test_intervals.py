import unittest

from intervals import merge, overlaps


class TestIntervalsVisible(unittest.TestCase):
    def test_merge_overlap(self):
        self.assertEqual(merge([[1, 3], [2, 5]]), [[1, 5]])

    def test_overlaps_basic(self):
        self.assertTrue(overlaps([1, 3], [2, 4]))


if __name__ == '__main__':
    unittest.main()
