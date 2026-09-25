import unittest

from orders import Order


class TestOrderSmoke(unittest.TestCase):
    def test_happy_path(self):
        o = Order('o1')
        self.assertEqual(o.state, 'draft')
        o.submit()
        o.pay()
        o.ship()
        o.deliver()
        self.assertEqual(o.state, 'delivered')


if __name__ == '__main__':
    unittest.main()
