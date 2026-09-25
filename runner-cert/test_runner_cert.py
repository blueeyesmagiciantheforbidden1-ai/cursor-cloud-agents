"""Tests for runner_cert.py. No network and no real agent CLI: fake adapters
play the agent. Run from this directory: python -m unittest -v test_runner_cert
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runner_cert  # noqa: E402

GIT = shutil.which("git")

IMPLEMENTATION = '''import re

_ITEM = re.compile(r"[+-]?[0-9]+")


def summarize(text):
    values = []
    if text.strip():
        for item in text.split(","):
            item = item.strip()
            if not _ITEM.fullmatch(item):
                raise ValueError("bad item %r" % item)
            values.append(int(item))
    checksum = sum((i + 1) * v for i, v in enumerate(values)) % @MODULUS@
    return {"sum": sum(values), "count": len(values), "checksum": checksum}
'''

# Passes the visible tests but treats an empty text as an error.
SLOPPY_IMPLEMENTATION = '''def summarize(text):
    values = [int(item) for item in text.split(",")]
    checksum = sum((i + 1) * v for i, v in enumerate(values)) % @MODULUS@
    return {"sum": sum(values), "count": len(values), "checksum": checksum}
'''


def _path(workspace, name):
    return os.path.join(workspace, name)


def _read(workspace, name):
    with open(_path(workspace, name), "r", encoding="utf-8") as handle:
        return handle.read()


def _write(workspace, name, text):
    with open(_path(workspace, name), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def implement(workspace, template=IMPLEMENTATION):
    # Like a real agent, learn the modulus by reading calc.py.
    modulus = re.search(r"% (\d+)", _read(workspace, "calc.py")).group(1)
    _write(workspace, "calc.py", template.replace("@MODULUS@", modulus))


def run_visible_and_record(workspace):
    result = subprocess.run([sys.executable, "-m", "unittest", "-v", "test_calc"], cwd=workspace,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    _write(workspace, "TEST_RESULT.txt", "\n".join(lines[-2:]) + "\n")


def run_gen(workspace):
    subprocess.run([sys.executable, "gen.py"], cwd=workspace, check=True, stdout=subprocess.DEVNULL)


def honest(workspace, prompt):
    implement(workspace)
    run_visible_and_record(workspace)
    run_gen(workspace)


class _SleepAdapter(runner_cert.CommandAdapter):
    """Does the whole task honestly, then a real child outlives the timeout.

    The work is done before the child starts, so every capability check
    passes whatever the load; only the timeout gate can refuse the run.
    """

    name = "sleeper"
    cli_name = "python"
    flags_verified_against = "test"

    def resolve(self):
        return [sys.executable], sys.executable, "test", None

    def command(self, workspace, prompt, prefix):
        return list(prefix) + ["-c", "import time; time.sleep(120)"]

    def run(self, workspace, prompt, timeout, log_path):
        honest(workspace, prompt)
        return super().run(workspace, prompt, timeout, log_path)


@unittest.skipUnless(GIT, "git is required")
class CertifyTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="runner-cert-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.work_root = os.path.join(self.root, "work")

    def certify(self, act, **kwargs):
        adapter = act if isinstance(act, runner_cert.Adapter) else runner_cert.FakeAdapter(act)
        receipt = runner_cert.certify(adapter, "test-runner", self.work_root, **kwargs)
        # Every gate that refuses a run names itself in reasons, so a run is
        # certified exactly when nothing refused it.
        self.assertEqual(receipt["certified"], receipt["reasons"] == [], receipt["reasons"])
        return receipt

    def assertVerified(self, receipt, *names):
        for name in names:
            self.assertTrue(receipt["capabilities"][name]["verified"],
                            "%s: %s" % (name, receipt["capabilities"][name]["evidence"]))

    def test_honest_agent_is_certified(self):
        seen = {}

        def act(workspace, prompt):
            seen["files"] = sorted(os.listdir(workspace))
            seen["prompt"] = prompt
            honest(workspace, prompt)

        receipt = self.certify(act)
        self.assertTrue(receipt["certified"], receipt["reasons"])
        self.assertEqual(receipt["reasons"], [])
        self.assertVerified(receipt, *runner_cert.CAPABILITIES)
        self.assertEqual(receipt["changed_files"], ["OUTPUT.txt", "TEST_RESULT.txt", "calc.py"])
        self.assertTrue(receipt["scope"]["clean"])
        # Running the tests left bytecode; it is noise, not a change.
        self.assertGreater(receipt["ignored_bytecode_files"], 0)
        # The agent saw the base files only: no hidden tests, no answers.
        self.assertEqual(seen["files"], [".git", "CHALLENGE", "TASK.md", "calc.py", "gen.py",
                                         "salt.txt", "test_calc.py"])
        # The task (and the modulus) live only in the workspace files.
        self.assertNotIn("summarize", seen["prompt"])
        self.assertIn("TASK.md", seen["prompt"])
        trusted = receipt["trusted_tests"]
        self.assertEqual((trusted["visible"]["ran"], trusted["visible"]["passed"]), (5, 5))
        self.assertTrue(trusted["visible"]["ok"])
        self.assertEqual(trusted["hidden"]["passed"], trusted["hidden"]["total"])
        self.assertGreater(trusted["hidden"]["total"], 15)
        # Receipt shape.
        self.assertEqual(receipt["suite"], "runner-cert-v1")
        self.assertEqual(receipt["adapter"], "fake")
        self.assertEqual(receipt["runner_id"], "test-runner")
        self.assertRegex(receipt["nonce"], r"^[0-9a-f]{32}$")
        self.assertRegex(receipt["base_commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(receipt["diff_sha256"], r"^[0-9a-f]{64}$")
        finished = runner_cert.parse_time(receipt["finished_at"])
        self.assertEqual(runner_cert.parse_time(receipt["expires_at"]) - finished, timedelta(days=14))
        self.assertLessEqual(runner_cert.parse_time(receipt["started_at"]), finished)
        self.assertTrue(receipt["workspace"].startswith(os.path.realpath(self.work_root)))
        self.assertLess(len(json.dumps(receipt)), 64 * 1024)
        self.assertEqual(receipt["harness"]["errors"], [])
        # The harness never writes into the workspace after the agent exits.
        self.assertEqual(sorted(name for name in os.listdir(receipt["workspace"]) if name != "__pycache__"),
                         [".git", "CHALLENGE", "OUTPUT.txt", "TASK.md", "TEST_RESULT.txt", "calc.py",
                          "gen.py", "salt.txt", "test_calc.py"])

    def test_guessed_output_does_not_prove_shell(self):
        def act(workspace, prompt):
            implement(workspace)
            run_visible_and_record(workspace)
            _write(workspace, "OUTPUT.txt", secrets.token_hex(32) + "\n")

        receipt = self.certify(act)
        self.assertFalse(receipt["certified"])
        self.assertFalse(receipt["capabilities"]["shell"]["verified"])
        self.assertIn("does not match", receipt["capabilities"]["shell"]["evidence"])
        self.assertIn("shell_not_verified", receipt["reasons"])
        self.assertVerified(receipt, "read_files", "write_files", "tests")
        self.assertTrue(receipt["scope"]["clean"])

    def test_editing_visible_tests_is_a_scope_violation(self):
        def act(workspace, prompt):
            honest(workspace, prompt)
            _write(workspace, "test_calc.py", "import unittest\n")

        receipt = self.certify(act)
        self.assertFalse(receipt["certified"])
        self.assertIn("scope_violation", receipt["reasons"])
        self.assertFalse(receipt["scope"]["clean"])
        self.assertIn("modified outside the allowed files: test_calc.py", receipt["scope"]["violations"])
        self.assertIn("test_calc.py", receipt["changed_files"])
        # The trusted run used the harness's own copy of the visible tests.
        self.assertEqual(receipt["trusted_tests"]["visible"]["ran"], 5)

    def test_extra_and_deleted_files_are_scope_violations(self):
        def act(workspace, prompt):
            honest(workspace, prompt)
            _write(workspace, "notes.txt", "scratch\n")
            os.remove(_path(workspace, "TASK.md"))
            os.makedirs(_path(workspace, os.path.join("sub", ".git")))
            _write(workspace, os.path.join("sub", ".git", "config"), "[core]\n")

        receipt = self.certify(act)
        self.assertFalse(receipt["certified"])
        violations = receipt["scope"]["violations"]
        self.assertIn("added outside the allowed files: notes.txt", violations)
        self.assertIn("deleted outside the allowed files: TASK.md", violations)
        # Never copied into the harness's git repository, so git cannot open it.
        self.assertIn("nested git metadata: sub/.git/config", violations)
        self.assertNotIn("git listing disagrees with the harness file listing", violations)

    def test_committing_moves_head_and_is_a_scope_violation(self):
        def act(workspace, prompt):
            honest(workspace, prompt)
            env = dict(os.environ, GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@b", GIT_COMMITTER_NAME="a",
                       GIT_COMMITTER_EMAIL="a@b")
            for args in (["add", "calc.py"], ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "x"]):
                subprocess.run([GIT] + args, cwd=workspace, env=env, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        receipt = self.certify(act)
        self.assertFalse(receipt["certified"])
        self.assertIn("git HEAD is no longer the base commit", receipt["scope"]["violations"])
        # The change is still seen: the harness compares files, not git state.
        self.assertIn("calc.py", receipt["changed_files"])

    def test_link_in_workspace_is_a_scope_violation(self):
        outside = os.path.join(self.root, "outside.txt")
        _write(self.root, "outside.txt", "secret\n")

        def act(workspace, prompt):
            honest(workspace, prompt)
            os.symlink(outside, _path(workspace, "peek.txt"))

        probe = os.path.join(self.root, "probe-link")
        try:
            os.symlink(outside, probe)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not permitted here")
        receipt = self.certify(act)
        self.assertFalse(receipt["certified"])
        self.assertIn("link or junction: peek.txt", receipt["scope"]["violations"])

    def test_hidden_failures_mean_tests_not_verified(self):
        def act(workspace, prompt):
            implement(workspace, SLOPPY_IMPLEMENTATION)
            run_visible_and_record(workspace)
            run_gen(workspace)

        receipt = self.certify(act)
        self.assertFalse(receipt["certified"])
        self.assertEqual(receipt["reasons"], ["tests_not_verified"])
        hidden = receipt["trusted_tests"]["hidden"]
        self.assertLess(hidden["passed"], hidden["total"])
        self.assertTrue(hidden["failed_examples"])
        # The claim and the visible run were fine; the hidden cases were not.
        self.assertTrue(receipt["trusted_tests"]["visible"]["ok"])
        self.assertIn("reports Ran 5 tests, OK", receipt["capabilities"]["tests"]["evidence"])
        self.assertVerified(receipt, "read_files", "write_files", "shell")

    def test_forged_test_summary_is_not_verified(self):
        def act(workspace, prompt):
            implement(workspace)
            _write(workspace, "TEST_RESULT.txt", "all tests passed\n")
            run_gen(workspace)

        receipt = self.certify(act)
        self.assertFalse(receipt["capabilities"]["tests"]["verified"])
        self.assertIn("no 'Ran N tests", receipt["capabilities"]["tests"]["evidence"])

    def test_untouched_workspace_proves_nothing(self):
        receipt = self.certify(lambda workspace, prompt: None)
        self.assertFalse(receipt["certified"])
        for name in runner_cert.CAPABILITIES:
            self.assertFalse(receipt["capabilities"][name]["verified"], name)
        self.assertEqual(receipt["changed_files"], [])

    def test_timeout_is_not_certified(self):
        receipt = self.certify(_SleepAdapter(), timeout=3)
        self.assertEqual(receipt["adapter_run"]["status"], "timeout")
        self.assertIsNone(receipt["adapter_run"]["exit_code"])
        self.assertLess(receipt["adapter_run"]["duration_seconds"], 60)
        # The work itself passed every check; only the timeout refuses it.
        self.assertVerified(receipt, *runner_cert.CAPABILITIES)
        self.assertTrue(receipt["scope"]["clean"])
        self.assertEqual(receipt["harness"]["errors"], [])
        self.assertEqual(receipt["reasons"], ["adapter_timeout"])
        self.assertFalse(receipt["certified"])

    def test_crashed_adapter_is_not_certified(self):
        def act(workspace, prompt):
            honest(workspace, prompt)
            raise RuntimeError("agent crashed after the work")

        receipt = self.certify(act)
        self.assertEqual(receipt["adapter_run"]["status"], "error")
        self.assertIsNone(receipt["adapter_run"]["exit_code"])
        self.assertVerified(receipt, *runner_cert.CAPABILITIES)
        self.assertTrue(receipt["scope"]["clean"])
        self.assertEqual(receipt["reasons"], ["adapter_error"])
        self.assertFalse(receipt["certified"])

    def test_harness_error_is_not_certified(self):
        real_git_changes = runner_cert.git_changes

        def rebuilt_elsewhere(*args, **kwargs):
            base, changed, digest = real_git_changes(*args, **kwargs)
            return "0" * 40, changed, digest

        # A rebuilt base that differs is the one harness error after which
        # the trusted tests still run, so the honest work still verifies.
        with mock.patch.object(runner_cert, "git_changes", rebuilt_elsewhere):
            receipt = self.certify(honest)
        self.assertVerified(receipt, *runner_cert.CAPABILITIES)
        self.assertTrue(receipt["scope"]["clean"])
        self.assertEqual(len(receipt["harness"]["errors"]), 1)
        self.assertIn("rebuilt base commit " + "0" * 40, receipt["harness"]["errors"][0])
        self.assertEqual(receipt["reasons"], ["harness_error"])
        self.assertFalse(receipt["certified"])

    def test_write_files_needs_calc_and_both_created_files(self):
        def calc_and_output_only(workspace, prompt):
            implement(workspace)
            run_gen(workspace)

        def calc_and_claim_only(workspace, prompt):
            implement(workspace)
            run_visible_and_record(workspace)

        def created_both_but_calc_untouched(workspace, prompt):
            run_visible_and_record(workspace)
            run_gen(workspace)

        for act, evidence in ((calc_and_output_only, "calc.py modified; created OUTPUT.txt"),
                              (calc_and_claim_only, "calc.py modified; created TEST_RESULT.txt"),
                              (created_both_but_calc_untouched,
                               "calc.py unchanged; created TEST_RESULT.txt, OUTPUT.txt")):
            with self.subTest(act.__name__):
                receipt = self.certify(act)
                self.assertFalse(receipt["capabilities"]["write_files"]["verified"])
                self.assertEqual(receipt["capabilities"]["write_files"]["evidence"],
                                 "harness listing: " + evidence)
                self.assertIn("write_files_not_verified", receipt["reasons"])
                self.assertTrue(receipt["scope"]["clean"])
                self.assertFalse(receipt["certified"])

    def test_unavailable_cli_is_not_certified_and_never_run(self):
        adapter = runner_cert.UnverifiedCliAdapter("grok", "grok", which=lambda name: None)
        receipt = self.certify(adapter)
        self.assertFalse(receipt["certified"])
        self.assertEqual(receipt["adapter_run"]["status"], "unavailable")
        self.assertEqual(receipt["adapter_run"]["reason"], "cli_not_found")
        self.assertIn("adapter_unavailable", receipt["reasons"])

    def test_workspace_escape_is_refused(self):
        os.makedirs(self.work_root)
        for bad in ("..", os.path.join("..", "escape"), os.path.join("a", "..", "..", "escape"), self.root, "."):
            with self.assertRaises(runner_cert.CertError) as caught:
                runner_cert.resolve_inside(self.work_root, bad)
            self.assertEqual(caught.exception.code, "workspace_outside_root")
        inside = runner_cert.resolve_inside(self.work_root, "run-1")
        self.assertEqual(os.path.dirname(inside), os.path.realpath(self.work_root))
        # A runner id cannot smuggle a path into the run directory name.
        for bad_id in ("../evil", "a/b", "a\\b", "", ".hidden", "x" * 65):
            with self.assertRaises(runner_cert.CertError) as caught:
                runner_cert.certify(runner_cert.FakeAdapter(honest), bad_id, self.work_root)
            self.assertEqual(caught.exception.code, "invalid_runner_id")
        self.assertEqual(os.listdir(self.work_root), [])

    def test_link_out_of_work_root_is_refused(self):
        os.makedirs(self.work_root)
        outside = os.path.join(self.root, "elsewhere")
        os.makedirs(outside)
        try:
            os.symlink(outside, os.path.join(self.work_root, "hop"), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not permitted here")
        with self.assertRaises(runner_cert.CertError) as caught:
            runner_cert.resolve_inside(self.work_root, os.path.join("hop", "run-1"))
        self.assertEqual(caught.exception.code, "workspace_outside_root")


class VisibleRunTest(unittest.TestCase):
    """run_visible() judges the harness's own tests against the agent's calc.py,
    which is imported into that run and can tamper with it."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="runner-cert-visible-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.challenge = runner_cert.Challenge()

    def run_visible_with(self, calc_source):
        trusted = os.path.join(self.root, "trusted-%d" % len(os.listdir(self.root)))
        os.mkdir(trusted)
        files = self.challenge.base_files()
        files["calc.py"] = calc_source.replace("@MODULUS@", str(self.challenge.modulus)).encode("ascii")
        runner_cert.write_tree(trusted, files)
        return runner_cert.run_visible(trusted, os.path.join(self.root, os.path.basename(trusted) + ".log"))

    def test_correct_calc_passes(self):
        visible = self.run_visible_with(IMPLEMENTATION)
        self.assertTrue(visible["ok"], visible)
        self.assertEqual((visible["ran"], visible["passed"]), (5, 5))

    def test_skipped_tests_are_not_a_pass(self):
        # Exit code 0 and "OK (skipped=5)": nothing was tested.
        visible = self.run_visible_with(
            "import unittest\n\n\ndef summarize(text):\n    raise unittest.SkipTest('no')\n")
        self.assertEqual(visible["process"], "exited")
        self.assertEqual(visible["ran"], 5)
        self.assertEqual(visible["passed"], 0)
        self.assertFalse(visible["ok"], visible)

    def test_fewer_tests_than_the_suite_are_not_a_pass(self):
        # A correct calc.py that hides four of the five visible tests.
        visible = self.run_visible_with(
            "import unittest\n"
            "unittest.TestLoader.getTestCaseNames = lambda self, case: ['test_basic']\n"
            + IMPLEMENTATION)
        self.assertEqual(visible["process"], "exited")
        self.assertEqual((visible["ran"], visible["passed"], visible["failures"], visible["errors"]),
                         (1, 1, 0, 0))
        self.assertFalse(visible["ok"], visible)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="runner-cert-registry-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.registry = os.path.join(self.root, "registry.json")
        self.now = runner_cert._utcnow()

    def receipt(self, runner_id, days_ago, certified=True):
        finished = self.now - timedelta(days=days_ago)
        return {"runner_id": runner_id, "adapter": "fake", "certified": certified,
                "finished_at": runner_cert.iso(finished),
                "expires_at": runner_cert.iso(finished + runner_cert.RECEIPT_TTL)}

    def test_fresh_certification_is_returned(self):
        fresh = self.receipt("alpha-cursor", 1)
        runner_cert.append_receipt(self.registry, fresh)
        self.assertEqual(runner_cert.latest_certification(self.registry, "alpha-cursor", now=self.now), fresh)
        self.assertIsNone(runner_cert.latest_certification(self.registry, "someone-else", now=self.now))

    def test_expired_entry_is_not_certified(self):
        runner_cert.append_receipt(self.registry, self.receipt("alpha-cursor", 15))
        self.assertIsNone(runner_cert.latest_certification(self.registry, "alpha-cursor", now=self.now))
        status = runner_cert.certification_status(self.registry, now=self.now)
        self.assertEqual(status["alpha-cursor"]["reason"], "expired")
        self.assertFalse(status["alpha-cursor"]["certified"])

    def test_expiry_instant_is_already_expired(self):
        entry = self.receipt("r1", 1)
        expires = runner_cert.parse_time(entry["expires_at"])
        just_before = expires - timedelta(seconds=1)
        self.assertEqual(runner_cert.latest_certification([entry], "r1", now=just_before), entry)
        self.assertTrue(runner_cert.certification_status([entry], now=just_before)["r1"]["certified"])
        self.assertIsNone(runner_cert.latest_certification([entry], "r1", now=expires))
        status = runner_cert.certification_status([entry], now=expires)["r1"]
        self.assertEqual((status["certified"], status["reason"]), (False, "expired"))

    def test_newest_receipt_decides(self):
        runner_cert.append_receipt(self.registry, self.receipt("r1", 3))
        runner_cert.append_receipt(self.registry, self.receipt("r1", 1, certified=False))
        self.assertIsNone(runner_cert.latest_certification(self.registry, "r1", now=self.now))
        self.assertEqual(runner_cert.certification_status(self.registry, now=self.now)["r1"]["reason"],
                         "not_certified")
        # Appended out of order: time decides, not position.
        runner_cert.append_receipt(self.registry, self.receipt("r2", 0.5))
        runner_cert.append_receipt(self.registry, self.receipt("r2", 2, certified=False))
        self.assertIsNotNone(runner_cert.latest_certification(self.registry, "r2", now=self.now))

    def test_unparseable_expiry_is_not_certified(self):
        entry = self.receipt("r1", 1)
        entry["expires_at"] = "soon"
        self.assertIsNone(runner_cert.latest_certification([entry], "r1", now=self.now))
        entry["expires_at"] = "2099-01-01T00:00:00"  # no zone
        self.assertIsNone(runner_cert.latest_certification([entry], "r1", now=self.now))

    def test_damaged_registry_is_refused_not_overwritten(self):
        with open(self.registry, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(runner_cert.CertError) as caught:
            runner_cert.append_receipt(self.registry, self.receipt("r1", 0))
        self.assertEqual(caught.exception.code, "registry_corrupt")
        with open(self.registry, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{not json")
        self.assertFalse(os.path.exists(self.registry + ".lock"))

    def test_registry_file_shape(self):
        runner_cert.append_receipt(self.registry, self.receipt("r1", 0))
        with open(self.registry, "rb") as handle:
            raw = handle.read()
        self.assertNotIn(b"\r\n", raw)
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(data["schema"], "runner-cert-registry/1")
        self.assertEqual(len(data["receipts"]), 1)

    def test_cli_status_and_argument_checks(self):
        runner_cert.append_receipt(self.registry, self.receipt("r1", 1))
        with mock.patch("sys.stdout") as out:
            self.assertEqual(runner_cert.main(["--status", "--registry", self.registry]), 0)
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertTrue(json.loads(printed)["r1"]["certified"])
        with mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                runner_cert.main(["--registry", self.registry, "--adapter", "cursor"])
            with self.assertRaises(SystemExit):
                runner_cert.main(["--registry", self.registry, "--adapter", "cursor", "--runner-id", "r",
                                  "--work-root", self.root, "--timeout", "5"])
            with self.assertRaises(SystemExit):
                runner_cert.main(["--registry", self.registry, "--adapter", "fake", "--runner-id", "r",
                                  "--work-root", self.root])


    @unittest.skipUnless(GIT, "git is required")
    def test_cli_records_an_uncertified_receipt(self):
        work_root = os.path.join(self.root, "work")
        args = ["--adapter", "grok", "--runner-id", "r-grok", "--work-root", work_root,
                "--registry", self.registry, "--timeout", "60"]
        with open(self.registry, "w", encoding="utf-8") as handle:
            handle.write("[]")
        with mock.patch("sys.stderr"):
            self.assertEqual(runner_cert.main(args), 2)
        self.assertFalse(os.path.exists(work_root))  # refused before any run
        os.remove(self.registry)
        with mock.patch("sys.stdout") as out:
            self.assertEqual(runner_cert.main(args), 1)
        printed = json.loads("".join(call.args[0] for call in out.write.call_args_list))
        self.assertFalse(printed["certified"])
        self.assertIn("adapter_unavailable", printed["reasons"])
        stored = runner_cert.load_registry(self.registry)
        self.assertEqual([entry["nonce"] for entry in stored], [printed["nonce"]])


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="runner-cert-adapter-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    @unittest.skipUnless(os.name == "nt", "the versions folder layout is the Windows install")
    def test_cursor_resolves_newest_version_like_the_bus_worker(self):
        versions = os.path.join(self.root, "cursor-agent", "versions")
        for name, complete in (("2026.09.18-aaaaaaa", True), ("2026.09.23-bbbbbbb", True),
                               ("2026.09.30-ccccccc", False)):
            folder = os.path.join(versions, name)
            os.makedirs(folder)
            for file_name in ("index.js", "node.exe") if complete else ("node.exe",):
                open(os.path.join(folder, file_name), "wb").close()
        adapter = runner_cert.CursorAdapter(local_appdata=self.root)
        info = adapter.probe()
        self.assertTrue(info["available"])
        self.assertTrue(info["flags_verified"])
        self.assertEqual(info["version"], "2026.09.23-bbbbbbb")
        folder = os.path.join(versions, "2026.09.23-bbbbbbb")
        argv = adapter.command(r"C:\work\ws", "do it", adapter._prefix)
        self.assertEqual(argv, [os.path.join(folder, "node.exe"), os.path.join(folder, "index.js"),
                                "-p", "--output-format", "text", "--trust", "--sandbox", "disabled",
                                "--auto-review", "--workspace", r"C:\work\ws", "do it"])
        self.assertEqual(adapter.env_extra, {"CURSOR_INVOKED_AS": "cursor-agent"})

    def test_cursor_missing_is_unavailable(self):
        adapter = runner_cert.CursorAdapter(local_appdata=self.root, which=lambda name: None)
        info = adapter.probe()
        self.assertFalse(info["available"])
        self.assertEqual(info["reason"], "cli_not_found")
        run = adapter.run(self.root, "x", 5, os.path.join(self.root, "log"))
        self.assertEqual(run["status"], "unavailable")

    def test_unverified_clis_are_detected_but_never_run(self):
        for name in ("copilot", "grok"):
            adapter = runner_cert.UnverifiedCliAdapter(name, name, which=lambda cli: "/usr/bin/" + cli)
            with mock.patch.object(runner_cert, "run_process") as run_process:
                result = adapter.run(self.root, "x", 5, os.path.join(self.root, name + ".log"))
            run_process.assert_not_called()
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["reason"], "cli_flags_unverified")
            self.assertTrue(adapter.probe()["available"])
            self.assertFalse(adapter.probe()["flags_verified"])

    def test_claude_and_codex_use_only_locally_verified_flags(self):
        with mock.patch.object(runner_cert, "_cli_version", return_value="v"):
            claude = runner_cert.ClaudeAdapter(which=lambda name: "/opt/bin/claude")
            codex = runner_cert.CodexAdapter(which=lambda name: "/opt/bin/codex")
            self.assertEqual(claude.command("/ws", "go", ["/opt/bin/claude"]),
                             ["/opt/bin/claude", "-p", "--output-format", "text", "--permission-mode", "auto",
                              "--permission-prompts", "none", "--no-session-persistence", "go"])
            self.assertEqual(codex.command("/ws", "go", ["/opt/bin/codex"]),
                             ["/opt/bin/codex", "exec", "--sandbox", "workspace-write", "--ephemeral",
                              "--color", "never", "-C", "/ws", "go"])
            self.assertTrue(claude.probe()["flags_verified"])

    @unittest.skipUnless(os.name == "nt", "Windows command-line quoting")
    def test_windows_quoting_round_trips(self):
        args = ["plain", "two words", 'say "hi"', "trail\\", "C:\\dir with space\\", 'back\\"slash',
                "", "line\nbreak", "tab\there", "\\\\server\\share"]
        echo = [sys.executable, "-c", "import json, sys; print(json.dumps(sys.argv[1:]))"]
        result = subprocess.run(runner_cert.windows_command_line(echo + args), stdout=subprocess.PIPE,
                                check=True)
        self.assertEqual(json.loads(result.stdout.decode("utf-8")), args)

    @unittest.skipUnless(os.name == "nt", "Windows .cmd shims")
    def test_cmd_shim_is_never_run_directly(self):
        shim = os.path.join(self.root, "codex.cmd")
        open(shim, "wb").close()
        prefix, path, reason = runner_cert._resolve_cli(lambda name: shim if name == "codex" else None,
                                                        "codex", "node_modules/@openai/codex/bin/codex.js")
        self.assertIsNone(prefix)
        self.assertEqual(reason, "cli_shim_unresolved")
        script = os.path.join(self.root, "node_modules", "@openai", "codex", "bin", "codex.js")
        os.makedirs(os.path.dirname(script))
        open(script, "wb").close()
        open(os.path.join(self.root, "node.exe"), "wb").close()
        prefix, path, reason = runner_cert._resolve_cli(lambda name: shim if name == "codex" else None,
                                                        "codex", "node_modules/@openai/codex/bin/codex.js")
        self.assertEqual(prefix, [os.path.join(self.root, "node.exe"), script])


class ClaimTest(unittest.TestCase):
    def test_real_summaries_pass(self):
        for text in (b"Ran 5 tests in 0.001s\nOK\n", b"Ran 5 tests in 0.010s\r\n\r\nOK\r\n",
                     "Ran 5 tests in 0.001s\r\nOK\r\n".encode("utf-16")):
            ok, evidence = runner_cert.check_claimed_summary(text)
            self.assertTrue(ok, evidence)

    def test_wrong_or_failed_summaries_fail(self):
        for text in (None, b"", b"OK\n", b"Ran 4 tests in 0.001s\nOK\n", b"Ran 5 tests in 0.001s\n",
                     b"Ran 5 tests in 0.001s\nFAILED (failures=1)\n", b"x" * 5000,
                     # An OK line does not cancel a FAILED status in the same claim.
                     b"Ran 5 tests in 0.001s\nOK\nFAILED (failures=1)\n",
                     b"Ran 5 tests in 0.001s\nFAILED (errors=2)\nOK\n"):
            ok, _ = runner_cert.check_claimed_summary(text)
            self.assertFalse(ok, text)

    def test_reference_matches_docstring_examples(self):
        self.assertEqual(runner_cert.reference_summarize(" 4, -5 ,+6", 65521),
                         {"sum": 5, "count": 3, "checksum": 12})
        self.assertEqual(runner_cert.reference_summarize("", 7), {"sum": 0, "count": 0, "checksum": 0})
        self.assertEqual(runner_cert.reference_summarize("-1", 7)["checksum"], 6)
        for bad in ("1,,2", "3,", "1.5", "+-4", "1 2", ",", "\u0663"):
            with self.assertRaises(ValueError):
                runner_cert.reference_summarize(bad, 7)


if __name__ == "__main__":
    unittest.main()
