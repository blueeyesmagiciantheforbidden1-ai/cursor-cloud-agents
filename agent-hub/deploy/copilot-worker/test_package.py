import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parent
def load(name):
    spec = importlib.util.spec_from_file_location(ROOT.name.replace("-", "_") + "_" + name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

entry = load("entrypoint")
credentials = load("credential_state")
builder = load("build_native")

class Broker:
    def __init__(self, fail=False):
        self.fail, self.released, self.quarantined, self.committed = fail, False, False, None
    def assert_current(self, lease):
        pass
    def commit(self, lease, body):
        self.committed = body
        if self.fail:
            raise OSError("uncertain")
        return "v2"
    def release(self, lease, version):
        self.released = True
    def quarantine(self, lease, reason):
        self.quarantined = True

class PackageTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".testscratch"
        scratch.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.path = Path(self.temporary.name)
    def tearDown(self):
        if self.path.resolve().parent != (ROOT / ".testscratch").resolve():
            raise RuntimeError("Test cleanup escaped its dedicated scratch directory")
        self.temporary.cleanup()
    def session(self, broker):
        lease = credentials.Lease(credentials.ACCOUNT_REF, 1, "v1", b"opaque-before-refresh")
        return credentials.RefreshSession(broker, lease, self.path / "fresh")
    def test_once_blocks_before_native_or_hub_access(self):
        with patch.object(sys, "argv", ["worker", "--once"]), patch.object(entry, "image_check") as check, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(entry.main(), 2)
            check.assert_not_called()
    def test_target_and_owner_cannot_be_overridden(self):
        for values in ({"hub_url": "https://example.com"}, {"expected_account_ref": "0" * 64},
                       {"execution_mode": "project_work"}, {"agent_id": "other"}, {"command": "anything"}):
            with self.assertRaises(ValueError):
                entry.worker_config(values)
    def test_environment_contains_no_ambient_credentials_or_endpoints(self):
        with patch.dict("os.environ", {"XAI_API_KEY": "test", "GH_TOKEN": "test", "COPILOT_PROVIDER_BASE_URL": "https://example.com"}):
            result = entry.provider_environment(self.path)
        self.assertFalse(set(result) & {"XAI_API_KEY", "GH_TOKEN", "COPILOT_PROVIDER_BASE_URL"})
    def test_explicit_fleet_required_to_include_self(self):
        other = "copilot" if entry.AGENT == "grok" else "grok"
        for fleet in ([other], [entry.AGENT, {}], [entry.AGENT, []], [entry.AGENT, entry.AGENT]):
            with self.assertRaises(ValueError):
                entry.worker_config({"model_policy_agents": fleet})
    def test_native_bytes_reject_script(self):
        path = self.path / "fake"
        path.write_bytes(b"#!/bin/sh\\n")
        with self.assertRaises(ValueError):
            builder.check_binary(path)
    def test_refreshed_bytes_committed_instead_of_seed(self):
        broker = Broker()
        session = self.session(broker)
        session.restore()
        session.auth_path.write_bytes(b"opaque-after-refresh")
        self.assertEqual(session.finish(native_stopped=True), "v2")
        self.assertEqual(broker.committed, b"opaque-after-refresh")
        self.assertTrue(broker.released)
    def test_uncertain_writeback_quarantines_and_retains_bytes(self):
        broker = Broker(fail=True)
        session = self.session(broker)
        session.restore()
        session.auth_path.write_bytes(b"opaque-after-refresh")
        with self.assertRaises(credentials.CredentialError):
            session.finish(native_stopped=True)
        self.assertFalse(broker.released)
        self.assertTrue(broker.quarantined)
        self.assertEqual(session.auth_path.read_bytes(), b"opaque-after-refresh")
        with self.assertRaises(credentials.CredentialError):
            session.restore()
    def test_live_native_process_prevents_release(self):
        broker = Broker()
        session = self.session(broker)
        session.restore()
        with self.assertRaises(credentials.CredentialError):
            session.finish(native_stopped=False)
        self.assertFalse(broker.released)
        self.assertIsNone(broker.committed)
    def test_wrong_owner_and_existing_home_rejected(self):
        with self.assertRaises(credentials.CredentialError):
            credentials.RefreshSession(Broker(), credentials.Lease("0" * 64, 1, "v1", b"x"), self.path / "new")
        session = self.session(Broker())
        session.home.mkdir()
        with self.assertRaises(credentials.CredentialError):
            session.restore()

if __name__ == "__main__":
    unittest.main()
