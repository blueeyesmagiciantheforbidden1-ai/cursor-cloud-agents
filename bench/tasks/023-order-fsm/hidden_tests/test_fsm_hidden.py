import unittest

from orders import Order, InvalidTransition


class TestOrderFsmHidden(unittest.TestCase):
    def test_empty_id(self):
        with self.assertRaises(ValueError):
            Order('')
        with self.assertRaises(ValueError):
            Order(None)

    def test_illegal_pay_from_draft(self):
        o = Order('a')
        with self.assertRaises(InvalidTransition):
            o.pay()
        self.assertEqual(o.state, 'draft')

    def test_double_submit(self):
        o = Order('a')
        o.submit()
        with self.assertRaises(InvalidTransition):
            o.submit()

    def test_cancel_from_paid(self):
        o = Order('a')
        o.submit()
        o.pay()
        o.cancel()
        self.assertEqual(o.state, 'cancelled')

    def test_cancel_after_ship_forbidden(self):
        o = Order('a')
        o.submit()
        o.pay()
        o.ship()
        with self.assertRaises(InvalidTransition) as ctx:
            o.cancel()
        self.assertIn('shipped', str(ctx.exception))
        self.assertEqual(o.state, 'shipped')

    def test_no_transition_after_delivered(self):
        o = Order('a')
        o.submit()
        o.pay()
        o.ship()
        o.deliver()
        for method in (o.submit, o.pay, o.ship, o.deliver, o.cancel):
            with self.assertRaises(InvalidTransition):
                method()

    def test_cancel_then_submit_forbidden(self):
        o = Order('a')
        o.cancel()
        with self.assertRaises(InvalidTransition):
            o.submit()

    def test_message_includes_state(self):
        o = Order('a')
        with self.assertRaises(InvalidTransition) as ctx:
            o.ship()
        self.assertIn('draft', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
