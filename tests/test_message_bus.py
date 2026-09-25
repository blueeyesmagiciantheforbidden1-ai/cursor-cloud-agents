"""The message bus survives a brief Claude revocation."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def load_hub():
    path = Path(__file__).resolve().parents[1] / "hub.py"
    spec = importlib.util.spec_from_file_location("message_bus", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MessageBusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = load_hub()
        root = Path(self.temp.name) / "hub"
        self.hub.HUB_DIR = root
        self.hub.LOG = root / "messages.jsonl"
        self.hub.READ_STATE = root / "read_state.json"
        self.hub.THREAD = root / "THREAD.md"

    def tearDown(self):
        self.temp.cleanup()

    def test_reconnect_republishes_the_log_without_dropping_mail(self):
        self.assertEqual(self.hub.main([
            "post", "--from", "claude", "--to", "cursor",
            "--subject", "blueeyes", "--body", "account revoked briefly",
        ]), 0)
        self.assertEqual(self.hub.main([
            "post", "--from", "cursor", "--to", "codex",
            "--subject", "bus", "--body", "shared memory still attached",
        ]), 0)
        self.hub.THREAD.write_text("stale\n", encoding="utf-8")
        status = self.hub.reconnect_bus()
        self.assertTrue(status["reconnected"])
        self.assertEqual(status["messages"], 2)
        self.assertEqual(status["unread"]["cursor"], 1)
        self.assertEqual(status["unread"]["codex"], 1)
        self.assertEqual(status["unread"]["claude"], 0)
        thread = self.hub.THREAD.read_text(encoding="utf-8")
        self.assertIn("account revoked briefly", thread)
        self.assertIn("shared memory still attached", thread)
        lines = [json.loads(line) for line in self.hub.LOG.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([item["seq"] for item in lines], [1, 2])
        self.assertTrue(status["log_present"])
        self.assertEqual(self.hub.main(["reconnect"]), 0)

    def test_missing_log_does_not_blank_the_rendered_thread(self):
        self.hub.THREAD.parent.mkdir(parents=True)
        self.hub.THREAD.write_text("keep this thread\n", encoding="utf-8")
        status = self.hub.reconnect_bus()
        self.assertTrue(status["reconnected"])
        self.assertIsNone(status["messages"])
        self.assertFalse(status["log_present"])
        self.assertEqual(self.hub.THREAD.read_text(encoding="utf-8"), "keep this thread\n")


if __name__ == "__main__":
    unittest.main()
