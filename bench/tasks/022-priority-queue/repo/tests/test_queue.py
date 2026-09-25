import unittest

from jobqueue import Job, PriorityQueue


class TestQueueSmoke(unittest.TestCase):
    def test_push_pop_one(self):
        q = PriorityQueue()
        q.push(Job('a', 1))
        self.assertEqual(len(q), 1)
        job = q.pop()
        self.assertEqual(job.job_id, 'a')
        self.assertEqual(len(q), 0)


if __name__ == '__main__':
    unittest.main()
