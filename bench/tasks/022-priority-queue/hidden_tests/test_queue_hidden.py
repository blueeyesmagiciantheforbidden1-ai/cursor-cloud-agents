import unittest

from jobqueue import Job, PriorityQueue


class TestPriorityHidden(unittest.TestCase):
    def test_priority_order(self):
        q = PriorityQueue()
        q.push(Job('low', 10))
        q.push(Job('high', 1))
        q.push(Job('mid', 5))
        self.assertEqual(q.pop().job_id, 'high')
        self.assertEqual(q.pop().job_id, 'mid')
        self.assertEqual(q.pop().job_id, 'low')

    def test_fifo_same_priority(self):
        q = PriorityQueue()
        q.push(Job('first', 3))
        q.push(Job('second', 3))
        q.push(Job('third', 3))
        self.assertEqual(q.pop().job_id, 'first')
        self.assertEqual(q.pop().job_id, 'second')
        self.assertEqual(q.pop().job_id, 'third')

    def test_peek_does_not_remove(self):
        q = PriorityQueue()
        q.push(Job('a', 2))
        q.push(Job('b', 1))
        self.assertEqual(q.peek().job_id, 'b')
        self.assertEqual(len(q), 2)
        self.assertEqual(q.pop().job_id, 'b')

    def test_duplicate_id_raises(self):
        q = PriorityQueue()
        q.push(Job('x', 1))
        with self.assertRaises(ValueError):
            q.push(Job('x', 2))

    def test_cancel_then_repush(self):
        q = PriorityQueue()
        q.push(Job('x', 5))
        self.assertTrue(q.cancel('x'))
        self.assertFalse(q.cancel('x'))
        q.push(Job('x', 1))
        self.assertEqual(q.pop().job_id, 'x')

    def test_empty_pop_peek(self):
        q = PriorityQueue()
        with self.assertRaises(IndexError):
            q.pop()
        with self.assertRaises(IndexError):
            q.peek()

    def test_job_validation(self):
        with self.assertRaises(ValueError):
            Job('', 1)
        with self.assertRaises(ValueError):
            Job('ok', -1)

    def test_cancel_does_not_break_order(self):
        q = PriorityQueue()
        q.push(Job('a', 1))
        q.push(Job('b', 2))
        q.push(Job('c', 3))
        q.cancel('b')
        self.assertEqual(q.pop().job_id, 'a')
        self.assertEqual(q.pop().job_id, 'c')


if __name__ == '__main__':
    unittest.main()
