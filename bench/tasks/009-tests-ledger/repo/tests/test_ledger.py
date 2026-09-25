import unittest

from ledger import Ledger


class TestLedgerVisible(unittest.TestCase):
    def test_starts_empty(self):
        book = Ledger()
        self.assertEqual(book.balance('cash'), 0)


if __name__ == '__main__':
    unittest.main()
