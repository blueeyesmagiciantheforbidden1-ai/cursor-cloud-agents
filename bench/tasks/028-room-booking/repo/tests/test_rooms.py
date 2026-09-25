import unittest

from rooms import RoomInventory, BookingLedger


class TestRoomsSmoke(unittest.TestCase):
    def test_add_and_reserve(self):
        inv = RoomInventory()
        inv.add_room('A', 4)
        led = BookingLedger(inv)
        bid = led.reserve('A', 0, 60, 'standup')
        self.assertTrue(bid)
        self.assertEqual(len(led.for_room('A')), 1)


if __name__ == '__main__':
    unittest.main()
