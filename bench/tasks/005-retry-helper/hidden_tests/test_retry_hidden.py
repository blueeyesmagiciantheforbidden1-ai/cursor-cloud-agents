import unittest
from unittest import mock

from retrykit import InvalidAttemptsError, retry_call


class TestRetryHidden(unittest.TestCase):
    def test_retries_then_succeeds(self):
        state = {'n': 0}

        def flaky():
            state['n'] += 1
            if state['n'] < 3:
                raise RuntimeError('nope')
            return 'ok'

        self.assertEqual(retry_call(flaky, attempts=3, exceptions=(RuntimeError,)), 'ok')
        self.assertEqual(state['n'], 3)

    def test_exhausts_and_reraises(self):
        def always():
            raise ValueError('x')

        with self.assertRaises(ValueError):
            retry_call(always, attempts=2, exceptions=(ValueError,))

    def test_does_not_catch_other_exceptions(self):
        def boom():
            raise TypeError('t')

        with self.assertRaises(TypeError):
            retry_call(boom, attempts=5, exceptions=(ValueError,))

    def test_invalid_attempts_type(self):
        with self.assertRaises(InvalidAttemptsError):
            retry_call(lambda: 1, attempts=0)
        with self.assertRaises(ValueError):
            retry_call(lambda: 1, attempts=0)

    def test_on_retry_called(self):
        seen = []

        def flaky():
            if not seen:
                seen.append('fail')
                raise RuntimeError('r')
            return 1

        calls = []
        retry_call(
            flaky,
            attempts=2,
            exceptions=(RuntimeError,),
            on_retry=lambda exc, attempt: calls.append((type(exc), attempt)),
        )
        self.assertEqual(calls, [(RuntimeError, 1)])

    def test_delay_sleeps(self):
        state = {'n': 0}

        def flaky():
            state['n'] += 1
            if state['n'] < 2:
                raise RuntimeError('r')
            return True

        with mock.patch('time.sleep') as sleep:
            retry_call(flaky, attempts=2, delay=0.25, exceptions=(RuntimeError,))
            sleep.assert_called_once_with(0.25)


if __name__ == '__main__':
    unittest.main()
