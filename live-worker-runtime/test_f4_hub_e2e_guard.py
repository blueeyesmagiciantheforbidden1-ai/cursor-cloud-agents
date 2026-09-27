"""Subprocess checks for the optional F4 hub contract test import guard."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


RUNTIME = Path(__file__).resolve().parent
VENDORED = RUNTIME.parent / 'agent-hub'


class F4HubE2EGuardTests(unittest.TestCase):
    def run_child(self, *args, hub_path=None, timeout=120):
        env = os.environ.copy()
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['PYTHONPATH'] = os.pathsep.join((str(VENDORED), str(RUNTIME)))
        if hub_path is None:
            env.pop('F4_HUB_PATH', None)
        else:
            env['F4_HUB_PATH'] = str(hub_path)
        return subprocess.run(
            [sys.executable, '-B', *args], cwd=RUNTIME, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', timeout=timeout,
        )

    def make_fake_hub(self, root, core):
        package = Path(root) / 'agent_hub'
        package.mkdir()
        (package / '__init__.py').write_text('', encoding='utf-8')
        (package / 'worker.py').write_text(
            'class LeaseLost(Exception): pass\n', encoding='utf-8',
        )
        (package / 'core.py').write_text(core, encoding='utf-8')

    def assert_import_error(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('OK (skipped=', result.stdout, result.stdout)
        self.assertIn(message, result.stdout, result.stdout)

    def test_vendored_hub_already_loaded_is_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_fake_hub(
                root, "HEARTBEAT_PHASES = ('setup', 'model_call', 'finishing')\n",
            )
            result = self.run_child(
                '-c', 'import agent_hub.worker, unittest; '
                "unittest.main(module=None, argv=['unittest', 'test_f4_hub_e2e'])",
                hub_path=root,
            )
        self.assert_import_error(
            result, 'agent_hub was not loaded from F4_HUB_PATH',
        )

    def test_f4_hub_path_without_agent_hub_is_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            result = self.run_child(
                '-m', 'unittest', 'test_f4_hub_e2e', hub_path=root,
            )
        self.assert_import_error(result, 'does not contain agent_hub')

    def test_pre_f4_hub_at_f4_hub_path_is_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_fake_hub(root, 'LEASE_SECONDS = 45\n')
            result = self.run_child(
                '-m', 'unittest', 'test_f4_hub_e2e', hub_path=root,
            )
        self.assert_import_error(result, 'missing HEARTBEAT_PHASES')

    def test_vendored_first_with_real_f4_hub_runs_the_tests(self):
        hub_path = os.environ.get('F4_HUB_PATH')
        if not hub_path:
            self.skipTest('F4_HUB_PATH is unset; no real F4 hub supplied')
        result = self.run_child(
            '-m', 'unittest',
            'test_f4_hub_e2e.F4HubE2ETests.test_5_happy_path_finishing',
            hub_path=hub_path, timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('OK', result.stdout.splitlines(), result.stdout)
        self.assertNotIn('skipped', result.stdout, result.stdout)

    def test_unset_keeps_the_skip(self):
        result = self.run_child('-m', 'unittest', 'test_f4_hub_e2e')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('OK (skipped=', result.stdout, result.stdout)


if __name__ == '__main__':
    unittest.main()
