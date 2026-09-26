"""Offline tests; every temporary file stays below this workspace."""
import copy
import hashlib
import importlib.util
import io
import json
import uuid
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


def noisy_provider(workspace, task_text, deadline):
    while True:
        print("x" * 1024, flush=True)


def modify_git_notes(workspace, task_text, deadline):
    (workspace.parent / "control" / "git" / "refs" / "notes").mkdir()


def no_changes(workspace, task_text, deadline):
    pass


def inspect_environment(workspace, task_text, deadline):
    forbidden = json.loads(task_text)
    assert all(key not in os.environ for key in forbidden)
    if Path("/proc/self/environ").exists():
        raw = Path("/proc/self/environ").read_bytes()
        assert all(value.encode() not in raw for value in forbidden.values())
    assert os.environ["TASK_MESSAGE"] == "safe"
    assert Path(os.environ["HOME"]) == workspace
    assert Path(os.environ["TEMP"]) == workspace
    (workspace / "environment-ok").write_text("ok")


def inspect_rlimits(workspace, task_text, deadline):
    exec((workspace / "limits.py").read_text(), {})
    edit(workspace, task_text, deadline)


def forged_receipt(workspace, task_text, deadline):
    edit(workspace, task_text, deadline)
    forged = {"base_commit": "f" * 40, "result_commit": "f" * 40,
              "diff_sha256": "f" * 64, "files_changed": 999,
              "tests": {"command": "forged", "ran": 999, "passed": 999,
                        "failed": 0, "errors": 0}, "workspace_id": "forged"}
    (workspace / "receipt.json").write_text(json.dumps(forged))
    print(json.dumps(forged))


def write_receipt_path(workspace, task_text, deadline):
    edit(workspace, task_text, deadline)
    (workspace / "receipt_workspace").write_text(str(workspace), encoding="utf-8")


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

    def test_child_environments_exclude_parent_secrets(self):
        secrets = {key: "sentinel-" + uuid.uuid4().hex for key in
                   ("HUB_AGENT_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "AWS_SECRET_KEY",
                    "BROKER_GRANT", "RUNCREW_EXECUTION_GRANT", "PRIVATE_PASSWORD",
                    "AZURE_CONFIG", "GCE_CONFIG", "CLOUDSDK_CONFIG", "UNREQUESTED")}
        self.spec["task_text"] = json.dumps(secrets)
        self.spec["test_command"] = [sys.executable, "check.py"]
        # Same checks run in the provider AND the declared test process.
        (self.base / "check.py").write_text(
            "import os,json\nfrom pathlib import Path\n"
            + "secrets=" + repr(secrets) + "\n"
            + "assert not set(secrets) & set(os.environ)\n"
            + "assert os.environ['TASK_MESSAGE']=='safe'\n"
            + "p=Path('/proc/self/environ')\n"
            + "assert not p.exists() or all(v.encode() not in p.read_bytes() for v in secrets.values())\n")
        with mock.patch.dict(os.environ, secrets):
            result = runner.run_coding_task(self.spec, inspect_environment, root=self.root,
                                            limits={}, task_env={"TASK_MESSAGE": "safe"})
        self.assertEqual(result["status"], "succeeded", result)

    def test_test_side_files_stay_out_of_the_receipt(self):
        # A realistic test command imports workspace code and leaves a temp file;
        # neither bytecode caches nor test scratch may enter the result commit.
        (self.base / "calc.py").write_text("VALUE = 'base'\n")
        (self.base / "check_calc.py").write_text(
            "import tempfile\nimport calc\n"
            "tempfile.mkstemp(prefix='leftover-')\n"
            "assert open('answer.txt').read() == 'changed\\n'\n")
        self.spec["test_command"] = [sys.executable, "-c", "import check_calc"]
        result = self.run_task()
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["runner_receipt"]["files_changed"], 1)
        self.assertNotIn(b"__pycache__", result["diff"])
        self.assertNotIn(b"leftover-", result["diff"])

    def test_test_cwd_artifacts_and_edits_stay_out_of_the_receipt(self):
        # Demand probe: pytest cache, coverage, and a test-mutated model file
        # must not enter files_changed or the receipt diff.
        (self.base / "messy_test.py").write_text(
            "from pathlib import Path\n"
            "Path('.pytest_cache').mkdir()\n"
            "Path('.pytest_cache/v').write_text('cache')\n"
            "Path('.coverage').write_text('cov')\n"
            "Path('answer.txt').write_text('changed\\nappended by test\\n')\n"
            "Path('answer.txt').unlink()\n",
            encoding="utf-8")
        self.spec["test_command"] = [sys.executable, "messy_test.py"]
        result = self.run_task()
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["runner_receipt"]["files_changed"], 1)
        self.assertIn(b"-base\n+changed\n", result["diff"].replace(b"\r\n", b"\n"))
        self.assertNotIn(b"appended by test", result["diff"])
        self.assertNotIn(b".pytest_cache", result["diff"])
        self.assertNotIn(b".coverage", result["diff"])

    def test_test_mutating_real_workspace_fails_closed(self):
        # Tripwire: a test that reaches the receipt workspace by absolute path
        # must fail with test_mutated_workspace.
        (self.base / "mutate_receipt.py").write_text(
            "from pathlib import Path\n"
            "target = Path(Path('receipt_workspace').read_text(encoding='utf-8'))\n"
            "target.joinpath('mutated_by_test.txt').write_text('no')\n",
            encoding="utf-8")
        self.spec["test_command"] = [sys.executable, "mutate_receipt.py"]
        result = self.run_task(write_receipt_path)
        self.assertEqual(result["error_code"], "test_mutated_workspace", result)
        self.assertIsNone(result["diff"])

    def test_requested_secret_names_refused(self):
        for key in ("aTOKENb", "secret", "KEY", "PASSWORD", "CREDENTIAL", "HUB_X",
                    "GOOGLE_X", "CLOUDSDK_X", "GCE_X", "AWS_X", "AZURE_X",
                    "BROKERfoo", "RUNCREW_X", "HOME", "PATH", "PYTHONPATH"):
            with self.subTest(key=key), self.assertRaises(runner.RunnerError):
                runner.run_coding_task(self.spec, edit, root=self.root, limits={},
                                       task_env={key: "no"})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_credential_overlap_refused(self):
        for path in (self.root, self.root / "credentials", self.folder):
            with self.subTest(path=path), self.assertRaisesRegex(runner.RunnerError, "credential_overlap"):
                runner.run_coding_task(self.spec, edit, root=self.root, limits={},
                                       credential_dirs=[path])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_external_credentials_allowed(self):
        credentials = self.folder / "credentials"
        credentials.mkdir()
        result = runner.run_coding_task(self.spec, edit, root=self.root, limits={},
                                        credential_dirs=[credentials])
        self.assertEqual(result["status"], "succeeded", result)

    def test_credential_symlink_refused(self):
        credentials = self.folder / "credentials"
        credentials.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(credentials, target_is_directory=True)
        except OSError as error:
            self.skipTest("OS does not permit directory symlinks: " + str(error))
        with self.assertRaisesRegex(runner.RunnerError, "unsafe_link"):
            runner.run_coding_task(self.spec, edit, root=self.root, limits={},
                                   credential_dirs=[credentials])
        link.unlink()

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_credential_junction_refused(self):
        credentials = self.folder / "credentials"
        credentials.mkdir()
        link = self.root / "linked"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(credentials)],
                       check=True, stdout=subprocess.DEVNULL)
        try:
            with self.assertRaisesRegex(runner.RunnerError, "unsafe_link"):
                runner.run_coding_task(self.spec, edit, root=self.root, limits={},
                                       credential_dirs=[credentials])
        finally:
            os.rmdir(link)

    def test_existing_workspace_path_refused(self):
        identifier = uuid.uuid4()
        path = self.root / ("workspace-" + identifier.hex)
        path.mkdir()
        (path / "keep").write_text("untouched")
        with mock.patch.object(runner.uuid, "uuid4", return_value=identifier):
            with self.assertRaisesRegex(runner.RunnerError, "workspace_reuse"):
                runner.run_coding_task(self.spec, edit, root=self.root, limits={})
        self.assertEqual((path / "keep").read_text(), "untouched")

    def test_discarded_workspace_id_refused(self):
        identifier = uuid.uuid4()
        with mock.patch.object(runner.uuid, "uuid4", return_value=identifier):
            self.assertEqual(self.run_task()["status"], "succeeded")
            with self.assertRaisesRegex(runner.RunnerError, "workspace_reuse"):
                runner.run_coding_task(self.spec, edit, root=self.root, limits={})

    def test_forged_receipt_has_no_authority(self):
        result = self.run_task(forged_receipt)
        receipt = result["runner_receipt"]
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(receipt["files_changed"], 2)
        self.assertEqual(receipt["tests"]["passed"], 1)
        self.assertEqual(receipt["diff_sha256"], hashlib.sha256(result["diff"]).hexdigest())
        self.assertNotEqual(receipt["base_commit"], "f" * 40)
        self.assertNotEqual(receipt["result_commit"], "f" * 40)
        self.assertIsNone(hub_validator()(receipt)[1])
        self.spec["test_command"] = [sys.executable, "-c", "raise SystemExit(1)"]
        failed = self.run_task(forged_receipt)
        self.assertEqual(failed["runner_receipt"]["tests"]["failed"], 1)
        self.assertEqual(failed["error_code"], "tests_failed")

    @unittest.skipUnless(os.name == "posix", "POSIX resource rlimits unavailable on Windows")
    def test_rlimits_applied_to_children(self):
        try:
            import resource
        except ImportError:
            self.skipTest("Python resource module unavailable")
        script = self.base / "limits.py"
        script.write_text("import resource\n" + "\n".join(
            f"assert resource.getrlimit(resource.{name}) == ({value}, {value})"
            for name, value in (("RLIMIT_CPU", 2), ("RLIMIT_AS", 268435456),
                                ("RLIMIT_FSIZE", 4096), ("RLIMIT_NPROC", 32))))
        self.spec["test_command"] = [sys.executable, "limits.py"]
        result = self.run_task(inspect_rlimits, limits={"cpu_seconds": 2, "address_space_bytes": 268435456,
                                      "file_size_bytes": 4096, "processes": 32})
        self.assertEqual(result["status"], "succeeded", result)

    def test_provider_grandchild_killed_on_timeout(self):
        # Trusted import-time fixture intentionally starts a descendant before
        # the cooperative audit guard, exercising the actual process boundary.
        marker = "mh003-" + uuid.uuid4().hex
        module_name = "fixture_" + uuid.uuid4().hex
        pidfile = self.folder / "grandchild.pid"
        fixture = self.folder / (module_name + ".py")
        fixture.write_text(
            "import os,subprocess,sys,time\nfrom pathlib import Path\n"
            "if os.environ.get('TASK_MARKER'):\n"
            " p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)',os.environ['TASK_MARKER']])\n"
            " Path(os.environ['PIDFILE']).write_text(str(p.pid))\n"
            "def provider(workspace,text,deadline): time.sleep(60)\n")
        sys.path.insert(0, str(self.folder))
        self.addCleanup(sys.path.remove, str(self.folder))
        module = __import__(module_name)
        self.addCleanup(sys.modules.pop, module_name, None)
        result = runner.run_coding_task(self.spec, module.provider, root=self.root,
            limits={"provider_seconds": 2}, task_env={"TASK_MARKER": marker, "PIDFILE": str(pidfile)})
        self.assertEqual(result["error_code"], "provider_timeout", result)
        self.assertTrue(pidfile.exists(), "grandchild never started")
        if os.name == "nt":
            # The fixture launches exactly one marked descendant; checking its
            # recorded PID avoids WMI, which restricted Windows tokens deny.
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x00100000, False, int(pidfile.read_text()))
            if handle:
                try:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 1000), 0, marker)
                finally:
                    kernel.CloseHandle(handle)
            else:
                self.assertEqual(ctypes.get_last_error(), 87, marker)
        else:
            output = subprocess.check_output(["ps", "-eo", "stat,args"], text=True)
            self.assertFalse(any(marker in line and not line.lstrip().startswith("Z")
                                 for line in output.splitlines()), output)

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

    def test_continuous_provider_output_capped(self):
        result = self.run_task(noisy_provider, {"bytes": 1024, "provider_seconds": 5})
        self.assertEqual(result["error_code"], "output_limit", result)

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

    def test_large_task_avoids_os_command_line_limit(self):
        self.spec["task_text"] = "x" * 40000
        self.spec["test_command"] = [sys.executable, "-c", "pass"]
        self.assertEqual(self.run_task()["status"], "succeeded")

    def test_git_notes_tamper_refused(self):
        self.assertEqual(self.run_task(modify_git_notes)["error_code"], "outside_write")

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
