import copy
import unittest

from intervals import merge, overlaps


class TestIntervalsHidden(unittest.TestCase):
    def test_merge_touching(self):
        self.assertEqual(merge([[1, 2], [2, 3]]), [[1, 3]])

    def test_merge_does_not_mutate_input(self):
        src = [[5, 6], [1, 2], [2, 4]]
        snapshot = copy.deepcopy(src)
        merge(src)
        self.assertEqual(src, snapshot)

    def test_merge_empty(self):
        self.assertEqual(merge([]), [])

    def test_merge_nested_unsorted(self):
        self.assertEqual(merge([[8, 10], [1, 3], [2, 6]]), [[1, 6], [8, 10]])

    def test_overlaps_touching_false(self):
        self.assertFalse(overlaps([1, 2], [2, 3]))

    def test_overlaps_point_empty(self):
        self.assertFalse(overlaps([1, 1], [1, 2]))
        self.assertFalse(overlaps([1, 1], [0, 1]))

    def test_value_error_on_inverted(self):
        with self.assertRaises(ValueError):
            merge([[3, 1]])
        with self.assertRaises(ValueError):
            overlaps([3, 1], [0, 1])

    def test_type_error_on_bad_shape(self):
        with self.assertRaises(TypeError):
            overlaps([1], [0, 1])
        with self.assertRaises(TypeError):
            merge([[1, 2, 3]])


if __name__ == '__main__':
    unittest.main()
