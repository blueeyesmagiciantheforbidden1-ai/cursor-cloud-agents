import unittest

from invoice.totals import compute_totals


class TestTotalsHappy(unittest.TestCase):
    def test_simple(self):
        lines = [{'qty': 2, 'unit_price': 10.0}]
        result = compute_totals(lines, discount_percent=10, tax_percent=10)
        self.assertAlmostEqual(result['subtotal'], 20.0, places=2)
        # discounted = 18, tax = 1.8, total = 19.8
        self.assertAlmostEqual(result['total'], 19.8, places=2)

    def test_no_discount_no_tax(self):
        lines = [{'qty': 1, 'unit_price': 5.5}]
        result = compute_totals(lines)
        self.assertEqual(result['subtotal'], 5.5)
        self.assertEqual(result['total'], 5.5)


if __name__ == '__main__':
    unittest.main()
