import unittest

from rooms import RoomInventory, BookingLedger, ConflictError, find_rooms


class TestRoomsHidden(unittest.TestCase):
    def test_inventory_validation(self):
        inv = RoomInventory()
        with self.assertRaises(ValueError):
            inv.add_room('A', 0)
        inv.add_room('A', 4, {'projector'})
        with self.assertRaises(ValueError):
            inv.add_room('A', 8)

    def test_equipment_not_mutated_and_frozen(self):
        inv = RoomInventory()
        eq = {'hdmi'}
        inv.add_room('B', 2, eq)
        eq.add('whiteboard')
        got = inv.get('B')
        self.assertEqual(got['equipment'], frozenset({'hdmi'}))
        self.assertIsInstance(got['equipment'], frozenset)

    def test_conflict_and_adjacent_ok(self):
        inv = RoomInventory()
        inv.add_room('A', 4)
        led = BookingLedger(inv)
        led.reserve('A', 0, 30)
        with self.assertRaises(ConflictError):
            led.reserve('A', 15, 45)
        led.reserve('A', 30, 60)

    def test_bad_window(self):
        inv = RoomInventory()
        inv.add_room('A', 4)
        led = BookingLedger(inv)
        with self.assertRaises(ValueError):
            led.reserve('A', 10, 10)
        with self.assertRaises(ValueError):
            find_rooms(inv, led, -1, 10)

    def test_cancel(self):
        inv = RoomInventory()
        inv.add_room('A', 4)
        led = BookingLedger(inv)
        bid = led.reserve('A', 0, 30)
        self.assertTrue(led.cancel(bid))
        self.assertFalse(led.cancel(bid))
        led.reserve('A', 0, 30)

    def test_for_room_sorted(self):
        inv = RoomInventory()
        inv.add_room('A', 4)
        led = BookingLedger(inv)
        led.reserve('A', 40, 50)
        led.reserve('A', 0, 10)
        times = [b['start'] for b in led.for_room('A')]
        self.assertEqual(times, [0, 40])

    def test_finder(self):
        inv = RoomInventory()
        inv.add_room('small', 2, set())
        inv.add_room('big', 10, {'projector', 'hdmi'})
        inv.add_room('med', 6, {'projector'})
        led = BookingLedger(inv)
        led.reserve('big', 0, 60)
        found = find_rooms(inv, led, 0, 60, min_capacity=5, need_equipment={'projector'})
        self.assertEqual(found, ['med'])
        found2 = find_rooms(inv, led, 60, 90, min_capacity=5, need_equipment={'projector'})
        self.assertEqual(found2, ['big', 'med'])

    def test_all_rooms_sorted(self):
        inv = RoomInventory()
        inv.add_room('b', 1)
        inv.add_room('a', 1)
        self.assertEqual([r['room_id'] for r in inv.all_rooms()], ['a', 'b'])


if __name__ == '__main__':
    unittest.main()
