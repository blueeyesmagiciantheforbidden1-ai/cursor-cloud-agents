import copy
import unittest

from invoice.totals import compute_totals


class TestInvoiceHidden(unittest.TestCase):
    def test_empty_lines(self):
        result = compute_totals([])
        self.assertEqual(result['subtotal'], 0.0)
        self.assertEqual(result['tax'], 0.0)
        self.assertEqual(result['total'], 0.0)

    def test_tax_on_discounted(self):
        lines = [{'qty': 1, 'unit_price': 100.0}]
        result = compute_totals(lines, discount_percent=20, tax_percent=10)
        # discounted=80, tax=8, total=88
        self.assertAlmostEqual(result['tax'], 8.0, places=2)
        self.assertAlmostEqual(result['total'], 88.0, places=2)
        self.assertAlmostEqual(result['discount_amount'], 20.0, places=2)

    def test_half_up_rounding(self):
        lines = [{'qty': 1, 'unit_price': 1.005}]
        result = compute_totals(lines)
        self.assertEqual(result['subtotal'], 1.01)
        self.assertEqual(result['total'], 1.01)

    def test_negative_qty_raises(self):
        with self.assertRaises(ValueError):
            compute_totals([{'qty': -1, 'unit_price': 5.0}])

    def test_negative_price_raises(self):
        with self.assertRaises(ValueError):
            compute_totals([{'qty': 1, 'unit_price': -5.0}])

    def test_discount_over_100_raises(self):
        with self.assertRaises(ValueError):
            compute_totals([{'qty': 1, 'unit_price': 1.0}], discount_percent=101)

    def test_tax_over_100_raises(self):
        with self.assertRaises(ValueError):
            compute_totals([{'qty': 1, 'unit_price': 1.0}], tax_percent=150)

    def test_does_not_mutate_input(self):
        lines = [{'qty': 2, 'unit_price': 3.0}]
        original = copy.deepcopy(lines)
        compute_totals(lines, discount_percent=5, tax_percent=5)
        self.assertEqual(lines, original)


if __name__ == '__main__':
    unittest.main()
