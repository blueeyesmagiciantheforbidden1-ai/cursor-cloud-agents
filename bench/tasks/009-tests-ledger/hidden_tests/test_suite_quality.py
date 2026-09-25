"""Hidden checks: authored visible tests must catch common ledger bugs."""

import importlib
import sys
import unittest


def _run_visible_tests():
    # Drop cached test modules so they pick up a patched Ledger.
    for name in list(sys.modules):
        if name == 'tests' or name.startswith('tests.'):
            del sys.modules[name]
    loader = unittest.TestLoader()
    suite = loader.discover('tests', top_level_dir='.')
    result = unittest.TestResult()
    suite.run(result)
    return result


class TestAuthoredSuiteQuality(unittest.TestCase):
    def test_catches_zero_amount_bug(self):
        import ledger

        class Broken(ledger.Ledger):
            def transfer(self, src, dst, amount):
                self._seen.add(src)
                self._seen.add(dst)
                self._balances[src] = self._balances.get(src, 0) - amount
                self._balances[dst] = self._balances.get(dst, 0) + amount

        original = ledger.Ledger
        ledger.Ledger = Broken
        try:
            result = _run_visible_tests()
            self.assertGreater(
                len(result.failures) + len(result.errors),
                0,
                'visible tests should fail when zero amounts are accepted',
            )
        finally:
            ledger.Ledger = original
            for name in list(sys.modules):
                if name == 'tests' or name.startswith('tests.'):
                    del sys.modules[name]

    def test_catches_unsorted_accounts_bug(self):
        import ledger

        class Broken(ledger.Ledger):
            def accounts(self):
                return sorted(self._seen, reverse=True)

        original = ledger.Ledger
        ledger.Ledger = Broken
        try:
            result = _run_visible_tests()
            self.assertGreater(
                len(result.failures) + len(result.errors),
                0,
                'visible tests should fail when accounts() is unsorted',
            )
        finally:
            ledger.Ledger = original
            for name in list(sys.modules):
                if name == 'tests' or name.startswith('tests.'):
                    del sys.modules[name]

    def test_real_ledger_still_passes_visible(self):
        import ledger
        importlib.reload(ledger)
        result = _run_visible_tests()
        self.assertEqual(len(result.failures) + len(result.errors), 0, result.failures)


if __name__ == '__main__':
    unittest.main()
