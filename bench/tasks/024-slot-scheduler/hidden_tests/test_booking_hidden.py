import unittest

from booking import Slot, Calendar


class TestSlotsHidden(unittest.TestCase):
    def test_slot_validation(self):
        with self.assertRaises(ValueError):
            Slot(5, 5)
        with self.assertRaises(ValueError):
            Slot(10, 5)
        with self.assertRaises(ValueError):
            Slot(-1, 5)

    def test_touching_not_overlap(self):
        a = Slot(0, 30)
        b = Slot(30, 60)
        self.assertFalse(a.overlaps(b))
        self.assertFalse(b.overlaps(a))

    def test_partial_overlap(self):
        a = Slot(0, 40)
        b = Slot(30, 60)
        self.assertTrue(a.overlaps(b))

    def test_book_rejects_overlap(self):
        cal = Calendar()
        self.assertTrue(cal.book(Slot(10, 20)))
        self.assertFalse(cal.book(Slot(15, 25)))
        self.assertEqual(len(cal.bookings()), 1)

    def test_book_allows_adjacent(self):
        cal = Calendar()
        self.assertTrue(cal.book(Slot(10, 20)))
        self.assertTrue(cal.book(Slot(20, 30)))
        self.assertEqual(len(cal.bookings()), 2)

    def test_available_skips_booked(self):
        cal = Calendar()
        cal.book(Slot(10, 20))
        slots = cal.available(0, 40, 10)
        starts = [s.start for s in slots]
        self.assertIn(0, starts)
        self.assertNotIn(10, starts)
        self.assertNotIn(15, starts)
        self.assertIn(20, starts)
        self.assertIn(30, starts)

    def test_available_validation(self):
        cal = Calendar()
        with self.assertRaises(ValueError):
            cal.available(0, 10, 0)
        with self.assertRaises(ValueError):
            cal.available(10, 5, 2)

    def test_bookings_sorted_copy(self):
        cal = Calendar()
        cal.book(Slot(50, 60))
        cal.book(Slot(10, 20))
        b = cal.bookings()
        self.assertEqual([s.start for s in b], [10, 50])
        b.append(Slot(99, 100))
        self.assertEqual(len(cal.bookings()), 2)

    def test_cancel_exact(self):
        cal = Calendar()
        cal.book(Slot(0, 10))
        self.assertFalse(cal.cancel(0, 9))
        self.assertTrue(cal.cancel(0, 10))


if __name__ == '__main__':
    unittest.main()
