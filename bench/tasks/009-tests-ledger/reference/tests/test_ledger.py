import unittest

from ledger import Ledger


class TestLedgerSuite(unittest.TestCase):
    def test_transfer_updates_balances(self):
        book = Ledger()
        book.transfer('a', 'b', 10)
        self.assertEqual(book.balance('a'), -10)
        self.assertEqual(book.balance('b'), 10)

    def test_unknown_balance_zero(self):
        self.assertEqual(Ledger().balance('nobody'), 0)

    def test_amount_zero_raises(self):
        book = Ledger()
        with self.assertRaises(ValueError):
            book.transfer('a', 'b', 0)
        self.assertEqual(book.balance('a'), 0)
        self.assertEqual(book.balance('b'), 0)

    def test_amount_negative_raises(self):
        book = Ledger()
        with self.assertRaises(ValueError):
            book.transfer('a', 'b', -5)

    def test_amount_non_int_raises(self):
        book = Ledger()
        with self.assertRaises(ValueError):
            book.transfer('a', 'b', 1.5)

    def test_overdraft_allowed(self):
        book = Ledger()
        book.transfer('a', 'b', 3)
        self.assertEqual(book.balance('a'), -3)

    def test_accounts_sorted_and_includes_both(self):
        book = Ledger()
        book.transfer('zeta', 'alpha', 1)
        self.assertEqual(book.accounts(), ['alpha', 'zeta'])

    def test_failed_transfer_does_not_register_accounts(self):
        book = Ledger()
        with self.assertRaises(ValueError):
            book.transfer('a', 'b', 0)
        self.assertEqual(book.accounts(), [])


if __name__ == '__main__':
    unittest.main()
