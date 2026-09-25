import unittest

from vending import VendingMachine


class TestVendingSmoke(unittest.TestCase):
    def test_buy(self):
        m = VendingMachine({'chips': 50})
        m.insert(25)
        m.insert(25)
        result = m.select('chips')
        self.assertEqual(result['item'], 'chips')
        self.assertEqual(result['change'], 0)


if __name__ == '__main__':
    unittest.main()
