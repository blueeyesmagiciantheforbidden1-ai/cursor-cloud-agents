"""Offline tests; every temporary file stays below this workspace."""
import copy
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

import workspace_runner as runner


def edit(workspace, task_text, deadline):
    assert time.monotonic() < deadline
    (workspace / "answer.txt").write_text(task_text, encoding="utf-8")


def crash(workspace, task_text, deadline):
    edit(workspace, task_text, deadline)
    raise RuntimeError("raw private diagnostic must not escape")


def wait_forever(workspace, task_text, deadline):
    edit(workspace, task_text, deadline)
    time.sleep(60)
    (workspace / "late.txt").write_text("must never happen")


def sibling_write(workspace, task_text, deadline):
    (workspace.parent / "escaped.txt").write_text("escape")


def swallowed_escape(workspace, task_text, deadline):
    try:
        sibling_write(workspace, task_text, deadline)
    except Exception:
        pass
    edit(workspace, task_text, deadline)


def outside_link(workspace, task_text, deadline):
    (workspace / "link").symlink_to(workspace.parent / "control", target_is_directory=True)


def huge(workspace, task_text, deadline):
    (workspace / "answer.txt").write_text("x" * 4096)


def remove_file(workspace, task_text, deadline):
    (workspace / "answer.txt").unlink()
    (workspace / "new.txt").write_text("new\n")


def forbidden_process(workspace, task_text, deadline):
    subprocess.run([sys.executable, "-c", "pass"])


def forbidden_network(workspace, task_text, deadline):
    import socket
    socket.socket()


def modify_git(workspace, task_text, deadline):
    (workspace / ".git").write_text("forged")


def no_changes(workspace, task_text, deadline):
    pass


def hub_validator():
    # Load runcrew under a private package name: do not accidentally validate
    # against cca/agent-hub when that is already imported by another suite.
    # RUNCREW_AGENT_HUB is runcrew's source/agent-hub (the folder holding
    # agent_hub/). The task workspace kept runcrew beside cca instead.
    roots = [os.environ.get("RUNCREW_AGENT_HUB"), Path(__file__).resolve().parents[2] / "runcrew"]
    folder = next((Path(r) / "agent_hub" for r in roots if r and (Path(r) / "agent_hub" / "core.py").is_file()), None)
    if folder is None:
        raise unittest.SkipTest("runcrew agent_hub not found: set RUNCREW_AGENT_HUB to runcrew source/agent-hub")
    name = "_mh003_runcrew_hub"
    if name + ".core" not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, folder / "__init__.py",
                                                     submodule_search_locations=[str(folder)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        spec.loader.exec_module(package)
        core_spec = importlib.util.spec_from_file_location(name + ".core", folder / "core.py")
        core = importlib.util.module_from_spec(core_spec)
        sys.modules[name + ".core"] = core
        core_spec.loader.exec_module(core)
    return sys.modules[name + ".core"]._runner_receipt


class WorkspaceRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mh003-test-", dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        self.base = self.folder / "base"
        self.base.mkdir()
        (self.base / "answer.txt").write_text("base\n")
        self.root = self.folder / "runs"
        self.root.mkdir()
        self.spec = {"base_snapshot": str(self.base), "task_text": "changed\n",
                     "test_command": [sys.executable, "-c", "assert open('answer.txt').read() == 'changed\\n'"]}

    def run_task(self, provider=edit, limits=None):
        result = runner.run_coding_task(self.spec, provider, root=self.root, limits=limits or {})
        proof = result["discard_proof"]
        self.assertTrue(proof["directory_absent"], result)
        self.assertFalse(os.path.lexists(proof["path"]))
        self.assertRegex(proof["tree_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual((self.base / "answer.txt").read_text(), "base\n")
        if result["status"] != "succeeded":
            self.assertIsNone(result["diff"])
        return result

    def test_disabled_by_default(self):
        self.assertIs(runner.ENABLED, False)

    def test_happy_path_and_receipt(self):
        result = self.run_task()
        self.assertEqual(result["status"], "succeeded", result)
        receipt = result["runner_receipt"]
        accepted, reason = hub_validator()(receipt)
        self.assertIsNone(reason)
        self.assertEqual(accepted["source"], "worker_reported")
        self.assertNotIn("source", receipt)
        self.assertEqual(receipt["files_changed"], 1)
        self.assertEqual(receipt["tests"]["passed"], 1)
        self.assertEqual(receipt["diff_sha256"], hashlib.sha256(result["diff"]).hexdigest())
        self.assertIn(b"-base\n+changed\n", result["diff"].replace(b"\r\n", b"\n"))
        self.assertNotEqual(receipt["base_commit"], receipt["result_commit"])

    def test_tests_failing(self):
        self.spec["test_command"] = [sys.executable, "-c", "raise SystemExit(2)"]
        result = self.run_task()
        self.assertEqual(result["error_code"], "tests_failed")
        self.assertEqual(result["runner_receipt"]["tests"]["failed"], 1)
        self.assertIsNone(hub_validator()(result["runner_receipt"])[1])

    def test_provider_timeout(self):
        started = time.monotonic()
        result = self.run_task(wait_forever, {"provider_seconds": 0.5})
        self.assertEqual(result["error_code"], "provider_timeout")
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(result["model_call_attempted"])
        self.assertIsNone(result["runner_receipt"])

    def test_test_command_timeout(self):
        self.spec["test_command"] = [sys.executable, "-c", "import time; time.sleep(60)"]
        self.assertEqual(self.run_task(limits={"test_seconds": 0.2})["error_code"], "test_timeout")

    def test_oversized_diff_refused(self):
        self.spec["test_command"] = [sys.executable, "-c", "pass"]
        self.assertEqual(self.run_task(huge, {"diff_bytes": 256})["error_code"], "diff_limit")

    def test_sibling_write_refused(self):
        self.assertEqual(self.run_task(sibling_write)["error_code"], "outside_write")

    def test_caught_escape_still_refused(self):
        self.assertEqual(self.run_task(swallowed_escape)["error_code"], "outside_write")

    def test_external_symlink_refused(self):
        self.assertEqual(self.run_task(outside_link)["error_code"], "unsafe_link")

    def test_exception_always_discards(self):
        result = self.run_task(crash)
        self.assertEqual(result["error_code"], "provider_error")
        self.assertNotIn("private", repr(result))

    def test_two_runs_are_isolated(self):
        first = self.run_task()
        second = self.run_task()
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["status"], "succeeded")
        self.assertNotEqual(first["discard_proof"]["path"], second["discard_proof"]["path"])
        self.assertNotEqual(first["runner_receipt"]["workspace_id"], second["runner_receipt"]["workspace_id"])
        self.assertEqual(first["diff"], second["diff"])

    def test_provider_byte_limit(self):
        self.assertEqual(self.run_task(huge, {"bytes": 1024})["error_code"], "byte_limit")

    def test_base_file_limit_before_provider(self):
        (self.base / "second.txt").write_text("extra")
        result = self.run_task(limits={"files": 1})
        self.assertEqual(result["error_code"], "file_limit")
        self.assertFalse(result["model_call_attempted"])

    def test_test_output_bounded(self):
        self.spec["test_command"] = [sys.executable, "-c", "print('x'*10000)"]
        self.assertEqual(self.run_task(limits={"bytes": 1024})["error_code"], "output_limit")

    def test_tarball_snapshot(self):
        archive = self.folder / "base.tar"
        with tarfile.open(archive, "w") as stream:
            stream.add(self.base / "answer.txt", arcname="answer.txt")
        self.spec["base_snapshot"] = str(archive)
        self.assertEqual(self.run_task()["status"], "succeeded")

    def test_tar_traversal_refused(self):
        archive = self.folder / "base.tar"
        with tarfile.open(archive, "w") as stream:
            info = tarfile.TarInfo("../escaped.txt"); info.size = 1
            stream.addfile(info, io.BytesIO(b"x"))
        self.spec["base_snapshot"] = str(archive)
        result = self.run_task()
        self.assertEqual(result["error_code"], "unsafe_path")
        self.assertFalse((self.folder / "escaped.txt").exists())

    def test_tar_external_link_refused(self):
        archive = self.folder / "base.tar"
        with tarfile.open(archive, "w") as stream:
            info = tarfile.TarInfo("link"); info.type = tarfile.SYMTYPE; info.linkname = "../base"
            stream.addfile(info)
        self.spec["base_snapshot"] = str(archive)
        self.assertEqual(self.run_task()["error_code"], "unsafe_link")

    def test_git_metadata_refused(self):
        self.assertEqual(self.run_task(modify_git)["error_code"], "unsafe_path")

    def test_provider_process_launch_refused(self):
        self.assertEqual(self.run_task(forbidden_process)["error_code"], "forbidden_operation")

    def test_provider_network_refused(self):
        self.assertEqual(self.run_task(forbidden_network)["error_code"], "forbidden_operation")

    def test_additions_deletions_exported(self):
        self.spec["test_command"] = [sys.executable, "-c", "pass"]
        result = self.run_task(remove_file)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["runner_receipt"]["files_changed"], 2)
        self.assertIn(b"deleted file mode", result["diff"])
        self.assertIn(b"new file mode", result["diff"])

    def test_unchanged_tree_has_valid_empty_patch(self):
        self.spec["test_command"] = [sys.executable, "-c", "pass"]
        result = self.run_task(no_changes)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["diff"], b"")
        self.assertEqual(result["runner_receipt"]["files_changed"], 0)

    def test_invalid_limits_create_nothing(self):
        for value in (True, float("nan"), 0, -1):
            with self.subTest(value=value), self.assertRaises(runner.RunnerError):
                runner.run_coding_task(self.spec, edit, root=self.root, limits={"bytes": value})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_incomplete_discard_refuses_export(self):
        original = runner._discard
        def incomplete(path, root):
            proof = original(path, root)
            proof["tree_sha256"] = None
            return proof
        with mock.patch.object(runner, "_discard", side_effect=incomplete):
            result = runner.run_coding_task(self.spec, edit, root=self.root, limits={})
        self.assertEqual(result["error_code"], "discard_unproven")
        self.assertIsNone(result["diff"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_exact_hub_validation_rejects_mutations(self):
        receipt = self.run_task()["runner_receipt"]
        validator = hub_validator()
        for key, value in (("source", "worker_reported"), ("files_changed", True),
                           ("workspace_id", "bad_name"), ("base_commit", "A" * 40)):
            mutated = copy.deepcopy(receipt); mutated[key] = value
            self.assertIsNotNone(validator(mutated)[1])
        mutated = copy.deepcopy(receipt); mutated["tests"]["passed"] = 2
        self.assertEqual(validator(mutated)[1], "invalid_tests_totals")


if __name__ == "__main__":
    unittest.main()
