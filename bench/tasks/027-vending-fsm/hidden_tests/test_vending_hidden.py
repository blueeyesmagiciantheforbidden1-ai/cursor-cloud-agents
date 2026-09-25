import unittest

from vending import VendingMachine, InsufficientFunds


class TestVendingHidden(unittest.TestCase):
    def test_prices_validation(self):
        with self.assertRaises(ValueError):
            VendingMachine({})
        with self.assertRaises(ValueError):
            VendingMachine({'a': 0})
        with self.assertRaises(ValueError):
            VendingMachine({'a': -5})
        with self.assertRaises(ValueError):
            VendingMachine({'a': 1.5})

    def test_coin_validation(self):
        m = VendingMachine({'a': 25})
        with self.assertRaises(ValueError):
            m.insert(1)
        with self.assertRaises(ValueError):
            m.insert(50)
        self.assertEqual(m.insert(100), 100)

    def test_insufficient(self):
        m = VendingMachine({'soda': 75})
        m.insert(25)
        with self.assertRaises(InsufficientFunds):
            m.select('soda')
        self.assertEqual(m.balance, 25)

    def test_change_and_reset(self):
        m = VendingMachine({'candy': 65})
        m.insert(100)
        result = m.select('candy')
        self.assertEqual(result['change'], 35)
        self.assertEqual(m.balance, 0)

    def test_unknown_item(self):
        m = VendingMachine({'a': 10})
        m.insert(10)
        with self.assertRaises(KeyError):
            m.select('missing')
        self.assertEqual(m.balance, 10)

    def test_refund(self):
        m = VendingMachine({'a': 10})
        m.insert(25)
        self.assertEqual(m.refund(), 25)
        self.assertEqual(m.balance, 0)
        self.assertEqual(m.refund(), 0)


if __name__ == '__main__':
    unittest.main()
