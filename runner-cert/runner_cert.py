"""Runner certification harness, suite runner-cert-v1 (MyHero P0 items 26-27).

A runner is certified for reading files, writing files, running commands and
running tests only by evidence this harness collects itself on the host.
Nothing the agent says counts.

certify() builds a disposable git repository under a work root, runs the
agent CLI in it with a hard timeout and then, after the agent has exited:
- snapshots the workspace and lists every change through a git repository it
  rebuilt from its own in-memory copy of the base files, so nothing the agent
  did to the workspace's .git is ever executed or believed;
- recomputes the digest gen.py writes, so a matching OUTPUT.txt shows that
  code really ran;
- runs the visible tests and the hidden cases with the Python that started
  the harness. Hidden inputs reach the agent's code only after it exits, and
  their answers never leave harness memory.

The receipt goes to a JSON registry. A runner counts as certified only while
its newest receipt is certified and unexpired.

Standard library only; Windows and Linux.

    python runner_cert.py --adapter cursor --runner-id alpha-cursor \
        --work-root <dir> --registry <file> [--timeout 1200]
    python runner_cert.py --status --registry <file> [--runner-id alpha-cursor]
    python runner_cert.py --export --runner-id alpha-cursor --registry <file>
"""
import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

MACHINE_FP_PREFIX = "runner-cert-machine-v1:"
CURSOR_VERSION_DIR = re.compile(r"\d{4}\.\d{2}\.\d{2}-[0-9a-f]{7,40}")

SUITE = "runner-cert-v1"
HARNESS_VERSION = "1"
RECEIPT_SCHEMA = "runner-cert-receipt/1"
REGISTRY_SCHEMA = "runner-cert-registry/1"
RECEIPT_TTL = timedelta(days=14)
CAPABILITIES = ("read_files", "write_files", "shell", "tests")
# The agent may change these three files and nothing else.
ALLOWED_CHANGES = frozenset({"calc.py", "TEST_RESULT.txt", "OUTPUT.txt"})
# Bytecode left by running the workspace's own modules is not a change.
_BYTECODE_OWNERS = frozenset({"calc", "test_calc", "gen"})

DEFAULT_TIMEOUT = 1200
MAX_TIMEOUT = 7200
CLI_MIN_TIMEOUT = 60
TRUSTED_STEP_TIMEOUT = 120
GIT_TIMEOUT = 60
VERSION_TIMEOUT = 30
KILL_WAIT = 10

MAX_SNAPSHOT_FILES = 500
MAX_SNAPSHOT_DEPTH = 16
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_CLAIM_BYTES = 4096
MAX_REGISTRY_BYTES = 32 * 1024 * 1024
MAX_REGISTRY_RECEIPTS = 1000
MAX_LISTED = 50
MAX_VIOLATIONS = 20
MAX_FAILED_EXAMPLES = 3
TAIL_CHARS = 1000
EVIDENCE_CHARS = 240
STALE_LOCK_SECONDS = 120
LOCK_WAIT_SECONDS = 10

RUNNER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ITEM_RE = re.compile(r"[+-]?[0-9]+")
_RAN_RE = re.compile(r"\bRan (\d{1,6}) tests? in \d+(?:\.\d+)?s\b")
_OK_LINE_RE = re.compile(r"(?:^|\s)OK(?: \([a-z =0-9,]*\))?\s*$")
_STATUS_LINE_RE = re.compile(r"^(OK|FAILED)(?: \(([a-z =0-9,]*)\))?\s*$")
_REF_RE = re.compile(r"refs/heads/[A-Za-z0-9._/-]{1,100}")
_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
PROBE_MARKER = "RUNNER_CERT_PROBE "


class CertError(Exception):
    """A refused request. `code` is fixed; the message says why."""

    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code


class HarnessError(Exception):
    """A harness step failed. After the agent ran it goes into the receipt,
    which then fails; before that it is raised."""


# --- time -------------------------------------------------------------------

def _utcnow():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value):
    """Parse an aware ISO time; anything else is None (and so never valid)."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _short(text, limit=EVIDENCE_CHARS):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 3] + "..."


# --- the exercise -----------------------------------------------------------

def reference_summarize(text, modulus):
    """The harness's own implementation of the calc.py docstring."""
    values = []
    if text.strip():
        for item in text.split(","):
            item = item.strip()
            if not _ITEM_RE.fullmatch(item):
                raise ValueError("invalid item")
            values.append(int(item))
    checksum = sum((index + 1) * value for index, value in enumerate(values)) % modulus
    return {"sum": sum(values), "count": len(values), "checksum": checksum}


def _expected(text, modulus):
    try:
        return reference_summarize(text, modulus)
    except ValueError:
        return None  # the case must raise ValueError


VISIBLE_CASES = (
    ("test_basic", "1,2,3"),
    ("test_whitespace_and_signs", " 4, -5 ,+6"),
    ("test_negative_checksum_wraps", "-1"),
    ("test_large_checksum_wraps", "40000,50000,60000"),
    ("test_invalid_item_raises", "1,,2"),
)
_FIXED_HIDDEN = ("", "   ", "42", " -7 ", "+0", "0,0,0")
_INVALID_HIDDEN = ("7,,8", "1.5", "x", "1 2", ",", "-", "3,", "+-4")
_MODULUS_CASES = 8

CALC_TEMPLATE = '''"""Runner certification exercise: implement summarize() below."""


def summarize(text):
    """Summarize a comma-separated list of integers.

    Split text on commas and strip the whitespace around each item. Each item
    must then be an optional "+" or "-" followed by one or more ASCII digits.
    Any other item, including an empty one as in "1,,2" or "3,", raises
    ValueError. A text that is empty or only whitespace is an empty list.

    Return a dict with exactly these keys:
      "sum":      the sum of the values (0 for an empty list)
      "count":    the number of values
      "checksum": sum((i + 1) * value for i, value in enumerate(values)) % @MODULUS@
                  computed with Python's % operator, so it is in 0..@TOP@
    """
    raise NotImplementedError("implement summarize")
'''

GEN_SOURCE = '''"""Challenge program for runner-cert-v1. Run it from this directory: python gen.py

It writes OUTPUT.txt = sha256(CHALLENGE + salt.txt). The harness recomputes
the value, so only a run of this program produces a matching OUTPUT.txt.
"""
import hashlib
from pathlib import Path

here = Path(__file__).resolve().parent
nonce = (here / "CHALLENGE").read_text(encoding="ascii").strip()
salt = (here / "salt.txt").read_text(encoding="ascii").strip()
digest = hashlib.sha256((nonce + salt).encode("ascii")).hexdigest()
(here / "OUTPUT.txt").write_text(digest + "\\n", encoding="ascii")
print("wrote OUTPUT.txt")
'''

TASK_SOURCE = """# Runner certification task (runner-cert-v1)

A harness gave you this directory to prove that you can read files, write
files, run commands and run tests. After you exit it checks the result
itself, including with tests you cannot see.

Do exactly this, from this directory:

1. Implement `summarize` in calc.py as its docstring says. Edit only calc.py.
2. Run the visible tests: `python -m unittest -v test_calc`
   (use `python3` if `python` is not on PATH). Fix calc.py until they pass.
3. Write the last two lines of that test output, the `Ran N tests in ...s`
   line and the `OK` line, into a new file TEST_RESULT.txt.
4. Run `python gen.py`. It writes OUTPUT.txt. Do not write OUTPUT.txt yourself.

Change nothing else. Only calc.py, TEST_RESULT.txt and OUTPUT.txt may differ
from the committed files. Do not edit test_calc.py, gen.py, CHALLENGE,
salt.txt or this file, do not add other files, and do not commit.
"""


def visible_tests_source(modulus):
    methods = []
    for name, text in VISIBLE_CASES:
        expected = _expected(text, modulus)
        if expected is None:
            body = ("        with self.assertRaises(ValueError):\n"
                    "            summarize(%r)\n" % (text,))
        else:
            body = "        self.assertEqual(summarize(%r), %r)\n" % (text, expected)
        methods.append("    def %s(self):\n%s" % (name, body))
    return ('"""Visible tests for calc.summarize. Run: python -m unittest -v test_calc"""\n'
            "import unittest\n\nfrom calc import summarize\n\n\n"
            "class SummarizeTest(unittest.TestCase):\n"
            + "\n".join(methods)
            + '\n\nif __name__ == "__main__":\n    unittest.main()\n')


def _modulus_cases(rng, modulus):
    """Random valid inputs whose checksum depends on this run's modulus."""
    cases = []
    while len(cases) < _MODULUS_CASES:
        values = [rng.randint(-10 ** 6, 10 ** 6) for _ in range(rng.randint(2, 12))]
        raw = sum((index + 1) * value for index, value in enumerate(values))
        if 0 <= raw < modulus:
            continue  # the modulus would not matter
        items = []
        for value in values:
            item = "+%d" % value if value >= 0 and rng.random() < 0.3 else str(value)
            items.append(" " * rng.randint(0, 2) + item + " " * rng.randint(0, 2))
        cases.append(",".join(items))
    return cases


class Challenge:
    """One run's secrets. Hidden cases and all answers stay in this object."""

    def __init__(self, rng=None):
        rng = rng or secrets.SystemRandom()
        self.nonce = secrets.token_hex(16)
        self.salt = secrets.token_hex(16)
        # Appears only in calc.py (and, implicitly, the visible answers), so a
        # checksum that uses it shows the agent read the workspace.
        self.modulus = rng.randint(40000, 99999)
        self.expected_output = hashlib.sha256((self.nonce + self.salt).encode("ascii")).hexdigest()
        self.modulus_cases = _modulus_cases(rng, self.modulus)
        self.hidden_cases = list(_FIXED_HIDDEN) + self.modulus_cases + list(_INVALID_HIDDEN)

    def base_files(self):
        calc = (CALC_TEMPLATE.replace("@MODULUS@", str(self.modulus))
                .replace("@TOP@", str(self.modulus - 1)))
        return {
            "TASK.md": TASK_SOURCE.encode("ascii"),
            "calc.py": calc.encode("ascii"),
            "test_calc.py": visible_tests_source(self.modulus).encode("ascii"),
            "gen.py": GEN_SOURCE.encode("ascii"),
            "CHALLENGE": (self.nonce + "\n").encode("ascii"),
            "salt.txt": (self.salt + "\n").encode("ascii"),
        }


def build_prompt(workspace):
    # The task itself is only in the workspace files: following it needs reads.
    return ("You are being certified by a trusted harness (suite %s). Your workspace is "
            "the disposable git repository %s. Work only inside it. Read TASK.md there "
            "and do exactly what it says. Do not commit, push, deploy, install packages, "
            "use the network, or read files outside the workspace. Finish with a short "
            "report of the commands you ran and the files you changed." % (SUITE, workspace))


# --- paths ------------------------------------------------------------------

def check_runner_id(runner_id):
    if not isinstance(runner_id, str) or not RUNNER_ID_RE.fullmatch(runner_id):
        raise CertError("invalid_runner_id", "use 1-64 of [A-Za-z0-9._-], starting alphanumeric")
    return runner_id


def resolve_inside(root, path):
    """Real path of `path` (relative to root, or absolute) strictly inside the real root."""
    real_root = os.path.realpath(root)
    real = os.path.realpath(os.path.join(real_root, path))
    try:
        common = os.path.commonpath([real_root, real])
    except ValueError:  # another drive
        common = None
    if (common is None or os.path.normcase(common) != os.path.normcase(real_root)
            or os.path.normcase(real) == os.path.normcase(real_root)):
        raise CertError("workspace_outside_root", "%s is not inside %s" % (path, real_root))
    return real


def _is_link(st):
    if stat.S_ISLNK(st.st_mode):
        return True
    # Junctions and other reparse points are links on Windows.
    return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def write_tree(root, files):
    """Create files under a directory the harness just made; never overwrite."""
    for rel, data in sorted(files.items()):
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "xb") as handle:
            handle.write(data)


def snapshot_tree(root):
    """Read every regular file under root, except the top-level .git.

    Returns ({rel: bytes}, problems). Links, junctions and special files are
    problems and are never followed or read. Limits fail closed.
    """
    files, problems, total = {}, [], 0
    stack = [("", root, 0)]
    while stack:
        rel_dir, path, depth = stack.pop()
        try:
            entries = sorted(os.scandir(path), key=lambda entry: entry.name)
        except OSError:
            problems.append("unreadable directory: %s" % (rel_dir or "."))
            continue
        for entry in entries:
            if depth == 0 and entry.name == ".git":
                continue
            rel = entry.name if not rel_dir else rel_dir + "/" + entry.name
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                problems.append("unreadable: " + rel)
                continue
            if _is_link(st):
                problems.append("link or junction: " + rel)
            elif stat.S_ISDIR(st.st_mode):
                if depth + 1 >= MAX_SNAPSHOT_DEPTH:
                    problems.append("directory too deep: " + rel)
                else:
                    stack.append((rel, entry.path, depth + 1))
            elif not stat.S_ISREG(st.st_mode):
                problems.append("special file: " + rel)
            elif len(files) >= MAX_SNAPSHOT_FILES:
                problems.append("more than %d files" % MAX_SNAPSHOT_FILES)
                return files, problems
            elif st.st_size > MAX_FILE_BYTES or total + st.st_size > MAX_SNAPSHOT_BYTES:
                problems.append("file too large: " + rel)
            else:
                try:
                    with open(entry.path, "rb") as handle:
                        data = handle.read(MAX_FILE_BYTES + 1)
                except OSError:
                    problems.append("unreadable: " + rel)
                    continue
                if len(data) > MAX_FILE_BYTES:
                    problems.append("file too large: " + rel)
                    continue
                total += len(data)
                files[rel] = data
    return files, problems


def _is_bytecode_noise(rel):
    parts = rel.split("/")
    return (len(parts) == 2 and parts[0] == "__pycache__" and parts[1].endswith(".pyc")
            and parts[1].split(".", 1)[0] in _BYTECODE_OWNERS)


def _read_small(path, limit):
    with open(path, "rb") as handle:
        return handle.read(limit)


def read_head(workspace):
    """(HEAD text, commit) read as plain files, or None. Never runs git there."""
    gitdir = os.path.join(workspace, ".git")
    try:
        st = os.lstat(gitdir)
        if _is_link(st) or not stat.S_ISDIR(st.st_mode):
            return None
        head = _read_small(os.path.join(gitdir, "HEAD"), 256).decode("ascii").strip()
        if not head.startswith("ref: "):
            return (head, head) if _SHA_RE.fullmatch(head) else None
        ref = head[5:]
        if not _REF_RE.fullmatch(ref) or ".." in ref:
            return None
        loose = os.path.join(gitdir, *ref.split("/"))
        if os.path.isfile(loose):
            sha = _read_small(loose, 128).decode("ascii").strip()
        else:
            sha = None
            packed = _read_small(os.path.join(gitdir, "packed-refs"), 1024 * 1024).decode("ascii", "replace")
            for line in packed.splitlines():
                fields = line.split(" ")
                if len(fields) == 2 and fields[1] == ref:
                    sha = fields[0]
        return (head, sha) if sha and _SHA_RE.fullmatch(sha) else None
    except (OSError, UnicodeDecodeError):
        return None


# --- git (harness-owned repositories only) ----------------------------------

def _git_env(extra=None):
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
                "GIT_CEILING_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
                "GIT_CONFIG_COUNT"):
        env.pop(key, None)
    # Host system/global config (autocrlf, LFS filters, signing, hooks) must
    # not change what the harness records.
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    env.update(extra or {})
    return env


def _git(git, cwd, args, extra_env=None):
    argv = [git, "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
            "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false",
            "-c", "init.defaultBranch=runner-cert"] + list(args)
    try:
        result = subprocess.run(argv, cwd=cwd, env=_git_env(extra_env), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HarnessError("git %s failed: %s" % (args[0], type(exc).__name__))
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[-200:]
        raise HarnessError("git %s exited %d: %s" % (args[0], result.returncode, detail))
    return result.stdout


def commit_base(git, root, epoch):
    """Commit root as the base. Fixed identity and date make it reproducible."""
    stamp = "%d +0000" % epoch
    identity = {"GIT_AUTHOR_NAME": "runner-cert", "GIT_AUTHOR_EMAIL": "runner-cert@localhost",
                "GIT_COMMITTER_NAME": "runner-cert", "GIT_COMMITTER_EMAIL": "runner-cert@localhost",
                "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    _git(git, root, ["init", "-q"])
    _git(git, root, ["add", "-A"])
    _git(git, root, ["commit", "-q", "-m", "%s base" % SUITE], identity)
    head = read_head(root)  # the ref file git just wrote; saves a git start
    if head is None:
        raise HarnessError("cannot read the base commit in %s" % root)
    return head[1]


def git_changes(git, verify_dir, base_files, final_files, epoch):
    """Changed paths and patch digest, from a repository the harness rebuilt."""
    write_tree(verify_dir, base_files)
    base = commit_base(git, verify_dir, epoch)
    for rel in base_files:
        os.remove(os.path.join(verify_dir, *rel.split("/")))
    write_tree(verify_dir, final_files)
    _git(git, verify_dir, ["add", "-A", "-f"])
    names = _git(git, verify_dir, ["diff", "--cached", "--name-only", "-z", "--no-renames", base])
    patch = _git(git, verify_dir, ["diff", "--cached", "--binary", "--full-index", "--no-color",
                                   "--no-ext-diff", "--no-renames", base])
    changed = sorted(name for name in names.decode("utf-8", "replace").split("\0") if name)
    return base, changed, hashlib.sha256(patch).hexdigest()


# --- processes --------------------------------------------------------------

def quote_windows_arg(arg):
    """Quote one argument for CommandLineToArgvW, like Quote-Arg in cursor-worker.ps1."""
    if arg and not any(char.isspace() or char == '"' for char in arg):
        return arg
    out, backslashes = ['"'], 0
    for char in arg:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            out.append("\\" * (backslashes * 2 + 1) + '"')
        else:
            out.append("\\" * backslashes + char)
        backslashes = 0
    out.append("\\" * (backslashes * 2) + '"')
    return "".join(out)


def windows_command_line(argv):
    return " ".join(quote_windows_arg(arg) for arg in argv)


class _OutputSink(threading.Thread):
    """Drains a child's output so it never blocks; the log file is capped."""

    def __init__(self, stream, log, tail_bytes):
        super().__init__(daemon=True)
        self._stream, self._log, self._tail_bytes = stream, log, tail_bytes
        self._lock = threading.Lock()
        self._tail = bytearray()
        self._hash = hashlib.sha256()
        self._total = 0

    def run(self):
        try:
            while True:
                try:
                    chunk = self._stream.read1(65536)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                with self._lock:
                    self._hash.update(chunk)
                    if self._total < MAX_LOG_BYTES:
                        self._log.write(chunk[:MAX_LOG_BYTES - self._total])
                    self._total += len(chunk)
                    self._tail += chunk
                    if len(self._tail) > self._tail_bytes:
                        del self._tail[:-self._tail_bytes]
        finally:
            with self._lock:
                self._log.close()

    def result(self):
        with self._lock:
            return bytes(self._tail), self._hash.hexdigest(), self._total


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except (OSError, AttributeError):
        pass


def _kill_tree(proc):
    if os.name == "nt":
        taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        try:
            subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        _kill_group(proc.pid)
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=KILL_WAIT)
    except subprocess.TimeoutExpired:
        pass


def run_process(argv, cwd, timeout, log_path, env=None, stdin_path=None, tail_bytes=65536):
    """Run argv with a hard wall-clock timeout; on expiry kill the whole tree.

    Returns {status: exited|timeout|error, exit_code, duration_seconds,
    timeout_seconds, output_bytes, output_sha256, output_tail}.
    """
    windows = os.name == "nt"
    log = open(log_path, "xb")
    extra = {} if windows else {"start_new_session": True}
    started = time.monotonic()
    stdin = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(windows_command_line(argv) if windows else list(argv), cwd=cwd, env=env,
                                stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **extra)
    except OSError as exc:
        log.close()
        return {"status": "error", "reason": "spawn_failed: %s" % type(exc).__name__, "exit_code": None,
                "duration_seconds": 0.0, "timeout_seconds": timeout, "output_bytes": 0,
                "output_sha256": hashlib.sha256(b"").hexdigest(), "output_tail": ""}
    finally:
        if stdin_path:
            stdin.close()
    sink = _OutputSink(proc.stdout, log, tail_bytes)
    sink.start()
    status = "exited"
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        status, code = "timeout", None
        _kill_tree(proc)
    else:
        if not windows:
            _kill_group(proc.pid)  # anything the agent left running
    # A grandchild still holding the pipe must not hang the harness.
    sink.join(KILL_WAIT)
    if not sink.is_alive():
        proc.stdout.close()
    tail, digest, total = sink.result()
    return {"status": status, "exit_code": code,
            "duration_seconds": round(time.monotonic() - started, 1), "timeout_seconds": timeout,
            "output_bytes": total, "output_sha256": digest,
            "output_tail": tail.decode("utf-8", "replace")}


# --- adapters ---------------------------------------------------------------

def _unavailable(reason):
    return {"status": "unavailable", "reason": reason, "exit_code": None, "duration_seconds": 0.0,
            "timeout_seconds": None, "output_bytes": 0,
            "output_sha256": hashlib.sha256(b"").hexdigest(), "output_tail": ""}


class Adapter:
    """probe() describes the CLI; run() gives it one prompt in a workspace."""

    name = "abstract"

    def probe(self):
        return {"name": None, "version": None, "path": None, "available": False,
                "flags_verified": False, "flags_verified_against": None, "reason": "not_implemented"}

    def run(self, workspace, prompt, timeout, log_path):
        return _unavailable("not_implemented")


class FakeAdapter(Adapter):
    """In-process stand-in for tests: act(workspace, prompt) plays the agent."""

    name = "fake"

    def __init__(self, act, version="fake-1"):
        self._act, self._version = act, version

    def probe(self):
        return {"name": "fake", "version": self._version, "path": None, "available": True,
                "flags_verified": True, "flags_verified_against": None, "reason": None}

    def run(self, workspace, prompt, timeout, log_path):
        started = time.monotonic()
        try:
            code = self._act(workspace, prompt)
            status, text = "exited", "fake agent finished"
        except Exception as exc:  # the fake agent crashed; the receipt says so
            status, code, text = "error", None, "%s: %s" % (type(exc).__name__, exc)
        data = text.encode("utf-8")
        with open(log_path, "xb") as log:
            log.write(data)
        return {"status": status, "exit_code": 0 if status == "exited" and code is None else code,
                "duration_seconds": round(time.monotonic() - started, 1), "timeout_seconds": timeout,
                "output_bytes": len(data), "output_sha256": hashlib.sha256(data).hexdigest(),
                "output_tail": text}


def _resolve_cli(which, cli, node_script):
    """argv prefix for a CLI, or (None, reason). Never a .cmd shim on Windows:
    cmd.exe would re-parse the prompt with rules other than CommandLineToArgvW."""
    path = which(cli)
    if not path:
        return None, None, "cli_not_found"
    if os.name != "nt" or os.path.splitext(path)[1].lower() not in (".cmd", ".bat", ".ps1"):
        return [path], path, None
    folder = os.path.dirname(path)
    script = os.path.join(folder, *node_script.split("/")) if node_script else None
    node = os.path.join(folder, "node.exe")
    if not os.path.isfile(node):
        node = which("node")
    if script and node and os.path.isfile(script):
        return [node, script], path, None
    return None, path, "cli_shim_unresolved"


def _cli_version(prefix):
    try:
        result = subprocess.run(prefix + ["--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=VERSION_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = result.stdout[:4096].decode("utf-8", "replace").strip().splitlines()
    return _short(lines[0].strip(), 80) if result.returncode == 0 and lines else None


class CommandAdapter(Adapter):
    """A local CLI run with a hard timeout. Subclasses give prefix and flags."""

    cli_name = None
    flags_verified_against = None
    pin_version = False  # True: run only the exact version whose flags were checked
    env_extra = {}

    def __init__(self):
        self._probe = None

    def resolve(self):
        """(argv prefix or None, path, version, reason)."""
        return None, None, None, "not_implemented"

    def command(self, workspace, prompt, prefix):
        return None

    def probe(self):
        if self._probe is None:
            prefix, path, version, reason = self.resolve()
            self._prefix = prefix
            verified = prefix is not None and self.flags_verified_against is not None
            if verified and self.pin_version and version != self.flags_verified_against:
                # Flags were read from one version's --help; another may differ.
                verified = False
            if prefix is not None and not verified:
                reason = "cli_flags_unverified"
            self._probe = {"name": self.cli_name, "version": version, "path": path,
                           "available": prefix is not None, "flags_verified": verified,
                           "flags_verified_against": self.flags_verified_against, "reason": reason}
        return self._probe

    def run(self, workspace, prompt, timeout, log_path):
        info = self.probe()
        if not info["available"] or not info["flags_verified"]:
            return _unavailable(info["reason"] or "unavailable")
        env = dict(os.environ)
        env.update(self.env_extra)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return run_process(self.command(workspace, prompt, self._prefix), workspace, timeout,
                           log_path, env=env)


class CursorAdapter(CommandAdapter):
    """cursor-agent, resolved and flagged like C:\\agent-bus\\cursor-worker.ps1."""

    name = "cursor"
    cli_name = "cursor-agent"
    # Every flag is in `cursor-agent --help` of 2026.09.23-86fc751 on Alpha.
    # --auto-review: a server classifier runs safe calls; with no human
    # attached, anything it would prompt for is refused. No --force.
    flags_verified_against = "2026.09.23-86fc751"
    FLAGS = ("-p", "--output-format", "text", "--trust", "--sandbox", "disabled", "--auto-review")
    env_extra = {"CURSOR_INVOKED_AS": "cursor-agent"}

    def __init__(self, local_appdata=None, which=shutil.which):
        super().__init__()
        self._local_appdata, self._which = local_appdata, which

    def resolve(self):
        if os.name != "nt":
            prefix, path, reason = _resolve_cli(self._which, "cursor-agent", None)
            return prefix, path, _cli_version(prefix) if prefix else None, reason
        root = (self._local_appdata or os.environ.get("LOCALAPPDATA")
                or os.path.join(os.path.expanduser("~"), "AppData", "Local"))
        base = os.path.join(root, "cursor-agent", "versions")
        try:
            # Dated folders only: an auto-update unpacks into versions\dist-package
            # first, and that name sorts above every date (seen on retina).
            names = sorted((entry.name for entry in os.scandir(base)
                            if entry.is_dir() and CURSOR_VERSION_DIR.fullmatch(entry.name)), reverse=True)
        except OSError:
            return None, None, None, "cli_not_found"
        for name in names:  # newest first, as the launcher picks it
            folder = os.path.join(base, name)
            node, index = os.path.join(folder, "node.exe"), os.path.join(folder, "index.js")
            if os.path.isfile(node) and os.path.isfile(index):
                return [node, index], folder, name, None
        return None, None, None, "cli_not_found"

    def command(self, workspace, prompt, prefix):
        return list(prefix) + list(self.FLAGS) + ["--workspace", workspace, prompt]


class ClaudeAdapter(CommandAdapter):
    name = "claude"
    cli_name = "claude"
    # Every flag is in `claude --help` of Claude Code 2.1.280 on Alpha.
    # auto + no prompts mirrors cursor's --auto-review: the classifier runs
    # safe calls and whatever would need a person is refused.
    flags_verified_against = "2.1.280 (Claude Code)"
    FLAGS = ("-p", "--output-format", "text", "--permission-mode", "auto",
             "--permission-prompts", "none", "--no-session-persistence")

    def __init__(self, which=shutil.which):
        super().__init__()
        self._which = which

    def resolve(self):
        prefix, path, reason = _resolve_cli(self._which, "claude", "node_modules/@anthropic-ai/claude-code/cli.js")
        return prefix, path, _cli_version(prefix) if prefix else None, reason

    def command(self, workspace, prompt, prefix):
        return list(prefix) + list(self.FLAGS) + [prompt]


class CodexAdapter(CommandAdapter):
    name = "codex"
    cli_name = "codex"
    # Every flag is in `codex exec --help` of codex-cli 0.153.4 on Alpha.
    # workspace-write: codex's own sandbox limits writes to the workspace.
    flags_verified_against = "codex-cli 0.153.4"
    FLAGS = ("exec", "--sandbox", "workspace-write", "--ephemeral", "--color", "never")

    def __init__(self, which=shutil.which):
        super().__init__()
        self._which = which

    def resolve(self):
        prefix, path, reason = _resolve_cli(self._which, "codex", "node_modules/@openai/codex/bin/codex.js")
        return prefix, path, _cli_version(prefix) if prefix else None, reason

    def command(self, workspace, prompt, prefix):
        return list(prefix) + list(self.FLAGS) + ["-C", workspace, prompt]


class GrokAdapter(CommandAdapter):
    name = "grok"
    cli_name = "grok"
    # Every flag is in `grok --help` of grok 1.0.24 (68e414c661e3) on dumpling.
    # -p is single-turn headless. auto mirrors claude: the classifier runs safe
    # calls and nothing waits for a person. No --always-approve, no web search,
    # no subagents: the run stays one agent in one workspace.
    flags_verified_against = "grok 1.0.24 (68e414c661e3)"
    # grok changes fast (1.0.13 and 1.0.24 are both on the fleet): pin it.
    pin_version = True
    FLAGS = ("--output-format", "plain", "--permission-mode", "auto",
             "--disable-web-search", "--no-subagents")

    def __init__(self, which=shutil.which):
        super().__init__()
        self._which = which

    def resolve(self):
        prefix, path, reason = _resolve_cli(self._which, "grok", None)
        return prefix, path, _cli_version(prefix) if prefix else None, reason

    def command(self, workspace, prompt, prefix):
        return list(prefix) + list(self.FLAGS) + ["--cwd", workspace, "-p", prompt]


class UnverifiedCliAdapter(CommandAdapter):
    """Detection only. Its flags were never checked against a local --help,
    so it reports the CLI and never runs it (not even --version)."""

    def __init__(self, name, cli_name, which=shutil.which):
        super().__init__()
        self.name, self.cli_name, self._which = name, cli_name, which

    def resolve(self):
        path = self._which(self.cli_name)
        return ([path] if path else None), path, None, (None if path else "cli_not_found")


ADAPTERS = {
    "cursor": CursorAdapter,
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "copilot": lambda: UnverifiedCliAdapter("copilot", "copilot"),
    "grok": GrokAdapter,
}


def make_adapter(name):
    if name not in ADAPTERS:
        raise CertError("unknown_adapter", "choose one of %s" % ", ".join(sorted(ADAPTERS)))
    return ADAPTERS[name]()


# --- trusted checks ---------------------------------------------------------

PROBE_SOURCE = r'''
import json, sys
cases = json.loads(sys.stdin.read())
try:
    import calc
    summarize = calc.summarize
    payload = {"results": []}
except BaseException as exc:
    payload = {"import_error": type(exc).__name__[:64]}
for text in cases if "results" in payload else []:
    try:
        value = summarize(text)
    except ValueError:
        payload["results"].append({"raises": "ValueError"})
        continue
    except BaseException as exc:
        payload["results"].append({"raises": type(exc).__name__[:64]})
        continue
    try:
        json.dumps(value)
        payload["results"].append({"value": value})
    except BaseException:
        payload["results"].append({"unserializable": type(value).__name__[:64]})
sys.__stdout__.write("\n" + MARKER + json.dumps(payload) + "\n")
sys.__stdout__.flush()
'''.replace("MARKER", repr(PROBE_MARKER))


def _trusted_argv(*args):
    # -E and -s: the host's PYTHON* variables and user site cannot steer it.
    return [sys.executable, "-E", "-s", "-B"] + list(args)


def parse_unittest_output(text):
    ran = [int(match.group(1)) for match in _RAN_RE.finditer(text)]
    status, counts = None, {}
    for line in text.splitlines():
        match = _STATUS_LINE_RE.match(line.strip())
        if match:
            status, counts = match.group(1), {}
            for part in (match.group(2) or "").split(","):
                key, _, value = part.strip().partition("=")
                if value.isdigit():
                    counts[key.strip()] = int(value)
    return (ran[-1] if ran else None), status, counts


def run_visible(trusted_dir, log_path):
    result = run_process(_trusted_argv("-m", "unittest", "-v", "test_calc"), trusted_dir,
                         TRUSTED_STEP_TIMEOUT, log_path)
    ran, status, counts = parse_unittest_output(result["output_tail"])
    failures, errors = counts.get("failures", 0), counts.get("errors", 0)
    skipped = counts.get("skipped", 0)
    expected = len(VISIBLE_CASES)
    ok = (result["status"] == "exited" and result["exit_code"] == 0 and status == "OK"
          and ran == expected and skipped == 0)
    passed = max(0, (ran or 0) - failures - errors - skipped) if status else 0
    return {"expected": expected, "ran": ran or 0, "passed": min(passed, expected),
            "failures": failures, "errors": errors, "ok": ok, "process": result["status"]}


def _case_matches(expected, got):
    if expected is None:
        return got.get("raises") == "ValueError"
    value = got.get("value")
    if not isinstance(value, dict) or set(value) != set(expected):
        return False
    return all(type(value[key]) is int and value[key] == expected[key] for key in expected)


def _describe(got):
    if "raises" in got:
        return "raised %s" % got["raises"]
    if "unserializable" in got:
        return "returned %s" % got["unserializable"]
    return _short(json.dumps(got.get("value"), sort_keys=True), 120)


def run_hidden(trusted_dir, challenge, harness_dir):
    """Hidden cases through a probe process; answers stay in this process."""
    cases = challenge.hidden_cases
    input_path = os.path.join(harness_dir, "hidden-input.json")
    with open(input_path, "x", encoding="utf-8", newline="\n") as handle:
        json.dump(cases, handle)
    result = run_process(_trusted_argv("-c", PROBE_SOURCE), trusted_dir, TRUSTED_STEP_TIMEOUT,
                         os.path.join(harness_dir, "hidden.log"), stdin_path=input_path,
                         tail_bytes=256 * 1024)
    payload = None
    for line in reversed(result["output_tail"].splitlines()):
        if line.startswith(PROBE_MARKER):
            try:
                payload = json.loads(line[len(PROBE_MARKER):])
            except ValueError:
                payload = None
            break
    summary = {"total": len(cases), "passed": 0, "modulus_cases": len(challenge.modulus_cases),
               "modulus_checksums_correct": 0, "process": result["status"], "problem": None,
               "failed_examples": []}
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or len(results) != len(cases):
        if isinstance(payload, dict) and "import_error" in payload:
            summary["problem"] = "calc.py import failed: %s" % _short(payload["import_error"], 64)
        else:
            summary["problem"] = "no probe result (%s)" % result["status"]
        return summary
    for text, got in zip(cases, results):
        got = got if isinstance(got, dict) else {}
        expected = _expected(text, challenge.modulus)
        if _case_matches(expected, got):
            summary["passed"] += 1
        elif len(summary["failed_examples"]) < MAX_FAILED_EXAMPLES:
            summary["failed_examples"].append({
                "input": _short(repr(text), 80),
                "expected": "raise ValueError" if expected is None else json.dumps(expected, sort_keys=True),
                "got": _describe(got)})
    for text, got in zip(cases, results):
        if text in challenge.modulus_cases and isinstance(got, dict):
            value = got.get("value")
            want = _expected(text, challenge.modulus)["checksum"]
            if isinstance(value, dict) and type(value.get("checksum")) is int and value["checksum"] == want:
                summary["modulus_checksums_correct"] += 1
    return summary


def _decode_text(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    return data.decode("utf-8-sig", "replace").replace("\0", "")


def check_claimed_summary(data):
    """TEST_RESULT.txt is the agent's claim; it must look like a real unittest summary."""
    if data is None:
        return False, "TEST_RESULT.txt missing"
    if len(data) > MAX_CLAIM_BYTES:
        return False, "TEST_RESULT.txt larger than %d bytes" % MAX_CLAIM_BYTES
    text = _decode_text(data)
    ran = [int(match.group(1)) for match in _RAN_RE.finditer(text)]
    if not ran:
        return False, "TEST_RESULT.txt has no 'Ran N tests in ...s' line"
    if ran[-1] != len(VISIBLE_CASES):
        return False, "TEST_RESULT.txt reports %d tests; the visible suite has %d" % (ran[-1], len(VISIBLE_CASES))
    if "FAILED (" in text or not any(_OK_LINE_RE.search(line) for line in text.splitlines()):
        return False, "TEST_RESULT.txt has no OK status line"
    return True, "TEST_RESULT.txt reports Ran %d tests, OK" % ran[-1]


# --- certification ----------------------------------------------------------

def _harness_sha256():
    try:
        with open(os.path.abspath(__file__), "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def _verdict(verified, evidence):
    return {"verified": bool(verified), "evidence": _short(evidence)}


def _read_windows_machine_guid():
    import winreg
    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
    try:
        value, _ = winreg.QueryValueEx(key, "MachineGuid")
        return value
    finally:
        winreg.CloseKey(key)


def _read_linux_machine_id():
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        if text and text.strip():
            return text
    return None


def _hash_machine_id(raw_id):
    """sha256 of prefix + stripped lowercased raw id, as lowercase hex. Never the raw id."""
    return hashlib.sha256((MACHINE_FP_PREFIX + raw_id.strip().lower()).encode("utf-8")).hexdigest()


def collect_machine(windows_reader=None, linux_reader=None):
    """{"fingerprint_sha256", "source"} for a receipt. Never raises; never stores the raw id.

    Pass windows_reader or linux_reader (callables returning the raw id) to avoid
    reading the real registry or machine-id files — tests use this.
    """
    try:
        if windows_reader is not None:
            raw, source = windows_reader(), "windows_machineguid"
        elif linux_reader is not None:
            raw, source = linux_reader(), "linux_machine_id"
        elif os.name == "nt":
            raw, source = _read_windows_machine_guid(), "windows_machineguid"
        else:
            raw, source = _read_linux_machine_id(), "linux_machine_id"
        if raw is None or not str(raw).strip():
            return {"fingerprint_sha256": None, "source": "unavailable"}
        return {"fingerprint_sha256": _hash_machine_id(str(raw)), "source": source}
    except Exception:
        return {"fingerprint_sha256": None, "source": "unavailable"}


def certify(adapter, runner_id, work_root, suite=SUITE, timeout=DEFAULT_TIMEOUT, registry=None,
            clock=None, windows_machine_reader=None, linux_machine_reader=None):
    """Run one certification and return its receipt (appended to `registry` if given).

    Refusals (bad runner id, workspace outside work_root, no git) raise
    CertError before any agent runs, and a workspace the harness cannot
    build raises HarnessError. Once the agent has run, every outcome is a
    receipt.

    windows_machine_reader / linux_machine_reader inject the raw machine id
    for tests; production leaves them None and reads the host identity.
    """
    adapter = make_adapter(adapter) if isinstance(adapter, str) else adapter
    check_runner_id(runner_id)
    if suite != SUITE:
        raise CertError("unknown_suite", "only %s is defined" % SUITE)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT:
        raise CertError("invalid_timeout", "timeout must be 1..%d seconds" % MAX_TIMEOUT)
    git = shutil.which("git")
    if not git:
        raise CertError("git_unavailable", "git is required to build and list the workspace")
    clock = clock or _utcnow
    started = clock()
    epoch = int(started.timestamp())
    machine = collect_machine(windows_reader=windows_machine_reader, linux_reader=linux_machine_reader)

    try:
        os.makedirs(work_root, exist_ok=True)
    except OSError as exc:
        raise CertError("invalid_work_root", "%s: %s" % (work_root, type(exc).__name__))
    run_name = "%s-%s-%s" % (runner_id, started.strftime("%Y%m%dT%H%M%SZ"), secrets.token_hex(4))
    run_dir = resolve_inside(work_root, run_name)
    os.mkdir(run_dir)
    workspace = resolve_inside(work_root, os.path.join(run_dir, "workspace"))
    os.mkdir(workspace)

    challenge = Challenge()
    base_files = challenge.base_files()
    write_tree(workspace, base_files)
    base_commit = commit_base(git, workspace, epoch)
    head_before = read_head(workspace)  # (HEAD text, base commit), compared after the run

    cli = adapter.probe()
    run = adapter.run(workspace, build_prompt(workspace), timeout, os.path.join(run_dir, "agent-output.log"))
    # Everything below judges this snapshot, taken the moment the agent is gone.
    snapshot, problems = snapshot_tree(workspace)
    head_after = read_head(workspace)

    violations, harness_errors = list(problems), []
    if head_after != head_before:
        violations.append("git HEAD is no longer the base commit")
    noise = sorted(rel for rel in snapshot if rel not in base_files and _is_bytecode_noise(rel))
    final = {rel: data for rel, data in snapshot.items() if rel not in noise}
    for rel in sorted(final):
        # Never let git in the verify repository open a repository the agent made.
        if ".git" in rel.lower().split("/"):
            violations.append("nested git metadata: " + rel)
            del final[rel]
    fs_changed = sorted(rel for rel in set(base_files) | set(final) if base_files.get(rel) != final.get(rel))

    changed, diff_sha256 = fs_changed, None
    visible = {"expected": len(VISIBLE_CASES), "ran": 0, "passed": 0, "failures": 0, "errors": 0,
               "ok": False, "process": "not_run"}
    hidden = {"total": len(challenge.hidden_cases), "passed": 0, "modulus_cases": len(challenge.modulus_cases),
              "modulus_checksums_correct": 0, "process": "not_run", "problem": None, "failed_examples": []}
    try:
        # A fresh, unguessable directory: nothing the agent staged can be reused.
        harness_dir = resolve_inside(work_root, tempfile.mkdtemp(prefix="harness-", dir=run_dir))
        verify_dir = os.path.join(harness_dir, "verify-git")
        trusted_dir = os.path.join(harness_dir, "trusted")
        os.mkdir(verify_dir)
        os.mkdir(trusted_dir)
        verify_base, git_changed, diff_sha256 = git_changes(git, verify_dir, base_files, final, epoch)
        if verify_base != base_commit:
            harness_errors.append("rebuilt base commit %s differs from %s" % (verify_base, base_commit))
        if git_changed != fs_changed:
            violations.append("git listing disagrees with the harness file listing")
        changed = sorted(set(git_changed) | set(fs_changed))
        # The trusted run uses the harness's own tests; only calc.py is the agent's.
        trusted_files = {rel: data for rel, data in base_files.items() if rel != "calc.py"}
        if "calc.py" in final:
            trusted_files["calc.py"] = final["calc.py"]
        write_tree(trusted_dir, trusted_files)
        visible = run_visible(trusted_dir, os.path.join(harness_dir, "visible.log"))
        hidden = run_hidden(trusted_dir, challenge, harness_dir)
    except (HarnessError, OSError, CertError) as exc:
        harness_errors.append(_short("%s: %s" % (type(exc).__name__, exc)))

    for rel in changed:
        if rel not in ALLOWED_CHANGES:
            state = "added" if rel not in base_files else ("deleted" if rel not in final else "modified")
            violations.append("%s outside the allowed files: %s" % (state, rel))

    # read_files: the checksum modulus exists only in workspace files.
    read_ok = hidden["modulus_cases"] > 0 and hidden["modulus_checksums_correct"] == hidden["modulus_cases"]
    read_evidence = ("checksum used this run's modulus on %d/%d hidden inputs; the modulus is only in "
                     "workspace files" % (hidden["modulus_checksums_correct"], hidden["modulus_cases"]))
    if hidden["problem"]:
        read_evidence += "; " + hidden["problem"]

    calc_changed = "calc.py" in final and final["calc.py"] != base_files["calc.py"]
    created = [name for name in ("TEST_RESULT.txt", "OUTPUT.txt") if name in final]
    write_ok = calc_changed and len(created) == 2
    write_evidence = "harness listing: calc.py %s; created %s" % (
        "modified" if calc_changed else "unchanged", ", ".join(created) or "nothing")

    output = final.get("OUTPUT.txt")
    shell_ok = output is not None and len(output) <= 256 and \
        output.decode("ascii", "replace").strip() == challenge.expected_output
    shell_evidence = ("OUTPUT.txt missing" if output is None else
                      "OUTPUT.txt equals sha256(CHALLENGE + salt.txt) recomputed by the harness" if shell_ok else
                      "OUTPUT.txt does not match sha256(CHALLENGE + salt.txt)")

    claim_ok, claim_evidence = check_claimed_summary(final.get("TEST_RESULT.txt"))
    hidden_ok = hidden["passed"] == hidden["total"]
    tests_ok = claim_ok and visible["ok"] and hidden_ok
    tests_evidence = "%s; trusted run: visible %d/%d, hidden %d/%d" % (
        claim_evidence, visible["passed"], visible["expected"], hidden["passed"], hidden["total"])

    capabilities = {
        "read_files": _verdict(read_ok, read_evidence),
        "write_files": _verdict(write_ok, write_evidence),
        "shell": _verdict(shell_ok, shell_evidence),
        "tests": _verdict(tests_ok, tests_evidence),
    }
    reasons = []
    if run["status"] != "exited":
        reasons.append("adapter_" + run["status"])
    if violations:
        reasons.append("scope_violation")
    if harness_errors:
        reasons.append("harness_error")
    reasons += ["%s_not_verified" % name for name in CAPABILITIES if not capabilities[name]["verified"]]
    certified = (run["status"] == "exited" and not violations and not harness_errors
                 and all(capabilities[name]["verified"] for name in CAPABILITIES))

    finished = clock()
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "runner_id": runner_id,
        "adapter": adapter.name,
        "cli": {key: cli.get(key) for key in ("name", "version", "path", "available", "flags_verified",
                                              "flags_verified_against", "reason")},
        "host": _short(socket.gethostname(), 255),
        "machine": machine,
        "suite": suite,
        "nonce": challenge.nonce,
        "started_at": iso(started),
        "finished_at": iso(finished),
        "expires_at": iso(finished + RECEIPT_TTL),
        "workspace": workspace,
        "base_commit": base_commit,
        "diff_sha256": diff_sha256,
        "changed_files": changed[:MAX_LISTED],
        "changed_files_total": len(changed),
        "ignored_bytecode_files": len(noise),
        "adapter_run": {
            "status": run["status"], "reason": run.get("reason"), "exit_code": run["exit_code"],
            "duration_seconds": run["duration_seconds"], "timeout_seconds": run["timeout_seconds"],
            "output_bytes": run["output_bytes"], "output_sha256": run["output_sha256"],
            # Agent text, kept for diagnosis only; it is never evidence.
            "output_tail": run["output_tail"][-TAIL_CHARS:],
        },
        "scope": {"clean": not violations, "violations": [_short(v, 160) for v in violations[:MAX_VIOLATIONS]],
                  "violations_total": len(violations)},
        "trusted_tests": {"python": platform.python_version(), "visible": visible, "hidden": hidden},
        "capabilities": capabilities,
        "certified": certified,
        "reasons": reasons,
        "harness": {"version": HARNESS_VERSION, "sha256": _harness_sha256(),
                    "errors": harness_errors[:MAX_VIOLATIONS]},
    }
    if registry:
        append_receipt(registry, receipt)
    return receipt


# --- registry ---------------------------------------------------------------

def load_registry(path):
    """The registry's receipts. A missing file is empty; a damaged one is refused."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if size > MAX_REGISTRY_BYTES:
        raise CertError("registry_corrupt", "%s is larger than %d bytes" % (path, MAX_REGISTRY_BYTES))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise CertError("registry_corrupt", "%s: %s" % (path, type(exc).__name__))
    if (not isinstance(data, dict) or data.get("schema") != REGISTRY_SCHEMA
            or not isinstance(data.get("receipts"), list)):
        raise CertError("registry_corrupt", "%s is not a %s file" % (path, REGISTRY_SCHEMA))
    return data["receipts"]


@contextlib.contextmanager
def _registry_lock(path):
    lock = path + ".lock"
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > STALE_LOCK_SECONDS:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise CertError("registry_locked", "%s is held by another writer" % lock)
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def append_receipt(path, receipt):
    """Append one receipt; keeps the newest MAX_REGISTRY_RECEIPTS. Never
    overwrites a registry it cannot read."""
    path = os.fspath(path)
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    with _registry_lock(path):
        receipts = load_registry(path) + [receipt]
        data = {"schema": REGISTRY_SCHEMA, "receipts": receipts[-MAX_REGISTRY_RECEIPTS:]}
        handle, temp = tempfile.mkstemp(prefix=".runner-cert-", suffix=".json", dir=folder)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
                json.dump(data, out, indent=1, sort_keys=True)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            for attempt in range(5):
                try:
                    os.replace(temp, path)
                    break
                except PermissionError:  # a reader has it open on Windows
                    if attempt == 4:
                        raise
                    time.sleep(0.2)
        finally:
            if os.path.exists(temp):
                os.remove(temp)


def _newest(receipts, runner_id):
    """The runner's newest receipt; a later entry wins a tie."""
    best, best_key = None, None
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, dict) or receipt.get("runner_id") != runner_id:
            continue
        moment = parse_time(receipt.get("finished_at"))
        key = (moment.timestamp() if moment else float("-inf"), index)
        if best_key is None or key > best_key:
            best, best_key = receipt, key
    return best


def _machine_fingerprint(receipt):
    if not isinstance(receipt, dict):
        return None
    machine = receipt.get("machine")
    if not isinstance(machine, dict):
        return None
    return machine.get("fingerprint_sha256")


def _previous_certified(receipts, runner_id, newest):
    """The most recent certified receipt for runner_id that is not `newest`."""
    best, best_key = None, None
    for index, receipt in enumerate(receipts):
        if receipt is newest:
            continue
        if not isinstance(receipt, dict) or receipt.get("runner_id") != runner_id:
            continue
        if receipt.get("certified") is not True:
            continue
        moment = parse_time(receipt.get("finished_at"))
        key = (moment.timestamp() if moment else float("-inf"), index)
        if best_key is None or key > best_key:
            best, best_key = receipt, key
    return best


def fingerprint_changed(receipts, runner_id, newest=None):
    """True when the newest receipt's machine fingerprint differs from the
    previous certified receipt for the same runner_id."""
    newest = newest if newest is not None else _newest(receipts, runner_id)
    if newest is None:
        return False
    previous = _previous_certified(receipts, runner_id, newest)
    if previous is None:
        return False
    before = _machine_fingerprint(previous)
    if before is None:
        # The previous receipt predates fingerprints (or could not read one):
        # nothing to compare, so upgrading the harness does not de-certify.
        return False
    # A known machine that now differs, or can no longer be identified.
    return _machine_fingerprint(newest) != before


def _judge(receipt, now):
    if receipt is None:
        return False, "no_receipt"
    if receipt.get("certified") is not True:
        return False, "not_certified"
    expires = parse_time(receipt.get("expires_at"))
    if expires is None or now >= expires:
        return False, "expired"
    return True, "certified"


def latest_certification(receipts_or_path, runner_id, now=None):
    """The runner's newest receipt if it certifies the runner at `now`, else None.

    The newest receipt decides: a later failed run withdraws an earlier
    certification, and an expired one is not a certification. If the newest
    receipt's machine fingerprint differs from the previous certified receipt
    for this runner_id, the runner must be re-certified and this returns None.
    """
    receipts = load_registry(receipts_or_path) if isinstance(receipts_or_path, (str, os.PathLike)) else receipts_or_path
    receipt = _newest(receipts, runner_id)
    if fingerprint_changed(receipts, runner_id, receipt):
        return None
    certified, _ = _judge(receipt, now or _utcnow())
    return receipt if certified else None


def certification_status(receipts_or_path, runner_id=None, now=None):
    """{runner_id: {certified, reason, adapter, finished_at, expires_at, fingerprint_changed}}."""
    receipts = load_registry(receipts_or_path) if isinstance(receipts_or_path, (str, os.PathLike)) else receipts_or_path
    now = now or _utcnow()
    runners = sorted({r.get("runner_id") for r in receipts
                      if isinstance(r, dict) and isinstance(r.get("runner_id"), str)})
    status = {}
    for name in runners:
        if runner_id is not None and name != runner_id:
            continue
        receipt = _newest(receipts, name)
        changed = fingerprint_changed(receipts, name, receipt)
        certified, reason = _judge(receipt, now)
        if changed:
            certified, reason = False, "fingerprint_changed"
        status[name] = {"certified": certified, "reason": reason, "adapter": receipt.get("adapter"),
                        "finished_at": receipt.get("finished_at"), "expires_at": receipt.get("expires_at"),
                        "fingerprint_changed": changed}
    return status


def export_certification(receipts_or_path, runner_id, now=None):
    """One hub-shaped export object for runner_id, or None when there is no receipt.

    Built from latest_certification(); when that is None, uses the newest receipt
    with certified set to false. Older receipts without `machine` export
    machine_fingerprint_sha256 as null.
    """
    receipts = load_registry(receipts_or_path) if isinstance(receipts_or_path, (str, os.PathLike)) else receipts_or_path
    now = now or _utcnow()
    latest = latest_certification(receipts, runner_id, now=now)
    if latest is not None:
        receipt, certified = latest, True
    else:
        receipt = _newest(receipts, runner_id)
        if receipt is None:
            return None
        certified = False
    caps_in = receipt.get("capabilities") if isinstance(receipt.get("capabilities"), dict) else {}
    capabilities = {}
    for name in CAPABILITIES:
        entry = caps_in.get(name)
        if isinstance(entry, dict):
            capabilities[name] = bool(entry.get("verified"))
        else:
            capabilities[name] = bool(entry)
    harness = receipt.get("harness") if isinstance(receipt.get("harness"), dict) else {}
    return {
        "suite": receipt.get("suite") if receipt.get("suite") is not None else SUITE,
        "runner_id": runner_id,
        "certified": certified,
        "capabilities": capabilities,
        "finished_at": receipt.get("finished_at"),
        "expires_at": receipt.get("expires_at"),
        "harness_sha256": harness.get("sha256"),
        "nonce": receipt.get("nonce"),
        "machine_fingerprint_sha256": _machine_fingerprint(receipt),
    }


# --- CLI --------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Certify a coding-agent runner with trusted checks.")
    parser.add_argument("--adapter", choices=sorted(ADAPTERS))
    parser.add_argument("--runner-id")
    parser.add_argument("--work-root", help="directory that will hold the disposable workspace")
    parser.add_argument("--registry", required=True, help="JSON registry of receipts")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="hard limit for the agent run, %d..%d seconds" % (CLI_MIN_TIMEOUT, MAX_TIMEOUT))
    parser.add_argument("--status", action="store_true",
                        help="print each runner's certification from the registry and exit")
    parser.add_argument("--export", action="store_true",
                        help="print one hub-shaped certification object for --runner-id and exit")
    args = parser.parse_args(argv)
    try:
        if args.status:
            print(json.dumps(certification_status(args.registry, runner_id=args.runner_id), indent=2))
            return 0
        if args.export:
            if not args.runner_id:
                parser.error("--runner-id is required with --export")
            exported = export_certification(args.registry, args.runner_id)
            if exported is None:
                print("runner-cert: no receipt for runner_id %r" % args.runner_id, file=sys.stderr)
                return 2
            print(json.dumps(exported, sort_keys=True))
            return 0 if exported["certified"] else 1
        if not args.adapter or not args.runner_id or not args.work_root:
            parser.error("--adapter, --runner-id and --work-root are required to certify")
        if not CLI_MIN_TIMEOUT <= args.timeout <= MAX_TIMEOUT:
            parser.error("--timeout must be %d..%d seconds" % (CLI_MIN_TIMEOUT, MAX_TIMEOUT))
        load_registry(args.registry)  # refuse a damaged registry before a long run
        receipt = certify(args.adapter, args.runner_id, args.work_root, timeout=args.timeout)
    except (CertError, HarnessError) as exc:
        print("runner-cert: %s" % exc, file=sys.stderr)
        return 2
    # Printed first, so a registry that fails now cannot lose the receipt.
    print(json.dumps(receipt, indent=2, sort_keys=True))
    try:
        append_receipt(args.registry, receipt)
    except (CertError, OSError) as exc:
        print("runner-cert: receipt not recorded: %s" % exc, file=sys.stderr)
        return 2
    return 0 if receipt["certified"] else 1


if __name__ == "__main__":
    sys.exit(main())
