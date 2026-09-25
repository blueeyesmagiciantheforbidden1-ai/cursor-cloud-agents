import unittest

from booking import Slot, Calendar


class TestBookingSmoke(unittest.TestCase):
    def test_book_one(self):
        cal = Calendar()
        self.assertTrue(cal.book(Slot(0, 30)))
        self.assertEqual(len(cal.bookings()), 1)


if __name__ == '__main__':
    unittest.main()
