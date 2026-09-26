"""Disabled, offline MH-003 prototype. Requires Python 3.10+ and git.

Only trusted, importable (subprocess-import compatible) fake providers and
trusted local test commands are supported. Python audit hooks catch ordinary
fake-provider escapes; they are NOT an OS sandbox for hostile Python/native code.
No live adapter imports or network clients. See runcrew/docs/MYHERO_MH003_RUNNER_DESIGN.md.
"""
from __future__ import annotations

import hashlib
import json
import math
import importlib
import threading
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import uuid

ENABLED = False
DEFAULT_LIMITS = {
    "time_seconds": 60, "provider_seconds": 30, "test_seconds": 15,
    "cpu_seconds": 30, "address_space_bytes": 1024 * 1024 * 1024,
    "file_size_bytes": 8 * 1024 * 1024, "processes": 64,
    "bytes": 8 * 1024 * 1024, "files": 1000, "diff_bytes": 1024 * 1024,
}


_USED_IDS = set()
_ALLOCATION_LOCK = threading.Lock()


class RunnerError(Exception):
    """A fixed failure code; raw provider exceptions never leave the child."""


def _need(ok, code):
    if not ok:
        raise RunnerError(code)


def _limits(spec, supplied):
    result = dict(DEFAULT_LIMITS)
    for values in (supplied, spec.get("limits", {})):
        _need(type(values) is dict and not set(values) - set(result), "invalid_limits")
        for key, value in values.items():
            _need(type(value) in (int, float) and math.isfinite(value) and value > 0,
                  "invalid_limits")
            if key in ("bytes", "files", "diff_bytes"):
                _need(type(value) is int, "invalid_limits")
            result[key] = min(result[key], value)
    return result


def _link(st):
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _plain_path(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if os.path.lexists(part):
            _need(not _link(part.lstat()), "unsafe_link")
    return path


def _relative(name):
    # Portable names only: reject Windows drive/ADS paths even on POSIX.
    _need(type(name) is str and name and "\\" not in name and ":" not in name
          and "\x00" not in name, "unsafe_path")
    parts = PurePosixPath(name).parts
    _need(not name.startswith("/") and all(p not in (".", "..") for p in parts)
          and all(p.casefold() != ".git" and not p.endswith((".", " ")) for p in parts)
          and len(parts) <= 32, "unsafe_path")
    return parts


def _snapshot(path, limits):
    """Bounded regular-file content/mode snapshot, with no link following."""
    files, total, entries = {}, 0, 0
    def visit(folder, prefix=""):
        nonlocal total, entries
        with os.scandir(folder) as scan:
            for entry in scan:
                entries += 1
                _need(entries <= limits["files"], "file_limit")
                rel = prefix + entry.name
                _relative(rel)
                st = os.stat(entry.path, follow_symlinks=False)
                _need(not _link(st), "unsafe_link")
                if stat.S_ISDIR(st.st_mode):
                    visit(entry.path, rel + "/")
                else:
                    _need(stat.S_ISREG(st.st_mode) and st.st_nlink == 1, "unsafe_file")
                    _need(total + st.st_size <= limits["bytes"], "byte_limit")
                    with open(entry.path, "rb") as stream:
                        data = stream.read(limits["bytes"] - total + 1)
                    total += len(data)
                    _need(total <= limits["bytes"], "byte_limit")
                    files[rel] = (data, bool(st.st_mode & stat.S_IXUSR))
    visit(path)
    return files


def _materialize(base, workspace, limits):
    base = _plain_path(base)
    if base.is_dir():
        files = _snapshot(base, limits)
    else:
        _need(base.is_file() and base.stat().st_size <= limits["bytes"], "byte_limit")
        files, total, seen = {}, 0, set()
        # Never extractall: validate every member and bound expanded bytes too.
        with tarfile.open(base, mode="r|*") as archive:
            for member in archive:
                parts = _relative(member.name.rstrip("/"))
                name = "/".join(parts)
                _need(name.casefold() not in seen, "duplicate_path")
                seen.add(name.casefold())
                _need(len(seen) <= limits["files"], "file_limit")
                _need(member.isdir() or member.isfile(), "unsafe_link")
                if member.isdir():
                    continue
                _need(0 <= member.size <= limits["bytes"] - total, "byte_limit")
                with archive.extractfile(member) as stream:
                    data = stream.read(member.size + 1)
                _need(len(data) == member.size, "invalid_snapshot")
                total += len(data)
                files[name] = (data, bool(member.mode & stat.S_IXUSR))
    folded = set()
    for name, (data, executable) in files.items():
        _need(name.casefold() not in folded, "duplicate_path")
        folded.add(name.casefold())
        target = workspace.joinpath(*_relative(name))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
        target.chmod(0o755 if executable else 0o644)
    _snapshot(workspace, limits)


def _guard(workspace):
    """Child-only audit guard for cooperative test doubles; sticky violations."""
    refused = []
    def deny(code):
        refused.append(code)
        raise RunnerError(code)
    def writable(value, dir_fd=None):
        if isinstance(value, int) or dir_fd not in (None, -1):
            deny("outside_write")
        target = Path(os.fsdecode(value)).resolve()
        if target != workspace and workspace not in target.parents:
            deny("outside_write")
        if any(p.casefold() == ".git" for p in target.relative_to(workspace).parts):
            deny("unsafe_path")
    def audit(event, args):
        if event == "open":
            mode, flags = args[1], args[2]
            if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                writable(args[0])
        elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"):
            writable(args[0])
            # dir_fd variants are refused, including paths relative to external fds.
            if event in ("os.remove", "os.rmdir") and len(args) > 1 and args[1] != -1:
                deny("outside_write")
            if event in ("os.mkdir", "os.chmod") and len(args) > 2 and args[2] != -1:
                deny("outside_write")
            if event == "os.utime" and len(args) > 3 and args[3] != -1:
                deny("outside_write")
        elif event == "os.rename":
            writable(args[0], args[2]); writable(args[1], args[3])
        elif event in ("os.symlink", "os.link"):
            deny("unsafe_link")
        elif event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn")) or event in (
                "os.system", "os.fork", "os.forkpty", "ctypes.dlopen", "ctypes.dlsym"):
            deny("forbidden_operation")
    sys.addaudithook(audit)
    return refused


def _provider_entry(payload):
    sys.dont_write_bytecode = True
    sys.path[:] = payload["import_paths"]
    provider = importlib.import_module(payload["module"])
    for part in payload["qualname"].split("."):
        provider = getattr(provider, part)
    workspace = Path(payload["workspace"]).resolve()
    os.chdir(workspace)
    refused = _guard(workspace)
    code = None
    try:
        provider(workspace, payload["text"], payload["deadline"])
    except BaseException:
        code = "provider_error"
    codes = [None, "provider_error", "outside_write", "unsafe_path",
             "unsafe_link", "forbidden_operation"]
    return codes.index(refused[0] if refused else code)


def _provider(provider_fn, workspace, text, deadline, env, bound):
    # exec with env= is essential: clearing os.environ after spawn still leaks
    # the original environment through /proc/self/environ on Linux.
    payload = dict(module=provider_fn.__module__, qualname=provider_fn.__qualname__,
                   import_paths=[os.path.abspath(p) for p in sys.path],
                   workspace=str(workspace), text=text, deadline=deadline)
    request = workspace.parent / "control" / "provider-request.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    bootstrap = ("import json,sys; sys.path.insert(0,sys.argv[1]); "
                 "from workspace_runner import _provider_entry; "
                 "sys.exit(_provider_entry(json.load(open(sys.argv[2],encoding='utf-8'))))")
    try:
        code, _ = _command([sys.executable, "-I", "-B", "-c", bootstrap,
                            str(Path(__file__).resolve().parent), str(request)],
                           workspace, env, deadline, bound["bytes"], bound)
    except RunnerError as failure:
        if str(failure) == "command_timeout":
            raise RunnerError("provider_timeout") from None
        raise
    codes = {1: "provider_error", 2: "outside_write", 3: "unsafe_path",
             4: "unsafe_link", 5: "forbidden_operation"}
    _need(code == 0, codes.get(code, "provider_error"))


def _child_environment(workspace, task_env, home=None):
    _need(type(task_env) is dict, "invalid_task_env")
    home = str(workspace if home is None else home)
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": home,
           "TMP": home, "TEMP": home, "TMPDIR": home,
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           # Interpreter caches are build artifacts, never part of a receipt diff.
           "PYTHONDONTWRITEBYTECODE": "1"}
    if os.name == "nt":
        for key in ("SYSTEMROOT", "COMSPEC"):
            if key in os.environ:
                env[key] = os.environ[key]
    for key, value in task_env.items():
        _need(type(key) is str and key and "=" not in key and "\0" not in key
              and type(value) is str and "\0" not in value, "invalid_task_env")
        upper = key.upper()
        _need(not any(word in upper for word in
                      ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL"))
              and not upper.startswith(("HUB_", "GOOGLE_", "CLOUDSDK_", "GCE_",
                                        "AWS_", "AZURE_", "BROKER", "RUNCREW_")),
              "forbidden_task_env")
        # Task variables cannot replace the supervisor's fixed environment or
        # inject interpreter/loader configuration.
        _need(upper not in env and upper not in ("SYSTEMROOT", "COMSPEC", "WINDIR", "USERPROFILE")
              and not upper.startswith(("PYTHON", "LD_", "DYLD_", "GIT_")),
              "forbidden_task_env")
        env[key] = value
    return env


def _credential_policy(root, directories):
    _need(isinstance(directories, (list, tuple)), "invalid_credential_dirs")
    for directory in directories:
        credential = Path(directory).resolve()
        _need(credential != root and root not in credential.parents
              and credential not in root.parents, "credential_overlap")
    # Fail closed on all links/reparse points: this also refuses indirect links
    # to ancestors of a credential directory, cycles, and broken links.
    def unreadable(error):
        raise RunnerError("credential_policy_unreadable") from None
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        for name in dirs + files:
            _need(not _link((Path(folder) / name).lstat()), "unsafe_link")


def _apply_rlimits(bound):
    if os.name != "posix":
        return
    try:
        import resource
    except ImportError:
        return
    for name, key in (("RLIMIT_CPU", "cpu_seconds"), ("RLIMIT_AS", "address_space_bytes"),
                      ("RLIMIT_FSIZE", "file_size_bytes"), ("RLIMIT_NPROC", "processes")):
        if hasattr(resource, name):
            limit = getattr(resource, name)
            _, hard = resource.getrlimit(limit)
            value = math.ceil(bound[key])
            if hard != resource.RLIM_INFINITY:
                value = min(value, hard)
            resource.setrlimit(limit, (value, value))


def _windows_job(process):
    """Assign the suspended child before it can spawn, then resume it.

    TerminateJobObject is the taskkill /T equivalent, including descendants
    whose immediate parent already exited. No breakaway flag is enabled.
    """
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    try:
        _need(job and kernel.AssignProcessToJobObject(job, int(process._handle)),
              "command_stop_uncertain")
        native = ctypes.WinDLL("ntdll")
        native.NtResumeProcess.argtypes = [wintypes.HANDLE]
        native.NtResumeProcess.restype = ctypes.c_long
        _need(native.NtResumeProcess(int(process._handle)) == 0, "command_stop_uncertain")
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        if job:
            kernel.CloseHandle(job)
        raise
    return kernel, job


def _kill_tree(process, job=None):
    if os.name == "posix":
        import signal
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif job is not None:
        kernel, handle = job
        try:
            _need(kernel.TerminateJobObject(handle, 1), "command_stop_uncertain")
        finally:
            kernel.CloseHandle(handle)


def _environment(control):
    # No inherited Git overrides, credentials, shell config, or provider settings.
    env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "COMSPEC") if k in os.environ}
    env.update(HOME=str(control), USERPROFILE=str(control), TMP=str(control),
               TEMP=str(control), TMPDIR=str(control), GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_NAME="offline-runner", GIT_AUTHOR_EMAIL="runner@localhost",
               GIT_COMMITTER_NAME="offline-runner", GIT_COMMITTER_EMAIL="runner@localhost",
               GIT_AUTHOR_DATE="2000-01-01T00:00:00Z", GIT_COMMITTER_DATE="2000-01-01T00:00:00Z",
               PYTHONDONTWRITEBYTECODE="1")
    return env


def _command(argv, cwd, env, deadline, output_limit, bound=None):
    """Drain stdout concurrently, cap memory, discard stderr, never invoke a shell."""
    remaining = deadline - time.monotonic()
    _need(remaining > 0, "command_timeout")
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=(os.name != "nt"),
                               creationflags=0x00000004 if os.name == "nt" else 0,
                               preexec_fn=(lambda: _apply_rlimits(bound or DEFAULT_LIMITS))
                               if os.name == "posix" else None)
    job = _windows_job(process) if os.name == "nt" else None
    data = bytearray()
    overflow = threading.Event()
    def drain():
        while True:
            chunk = process.stdout.read1(min(65536, output_limit + 1))
            if not chunk:
                break
            if len(data) + len(chunk) > output_limit:
                overflow.set()
                break
            data.extend(chunk)
    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    try:
        while process.poll() is None:
            if overflow.wait(min(0.02, max(0, deadline - time.monotonic()))):
                raise RunnerError("output_limit")
            _need(time.monotonic() < deadline, "command_timeout")
        thread.join(max(0, deadline - time.monotonic()))
        _need(not thread.is_alive(), "command_timeout")
        _need(not overflow.is_set(), "output_limit")
        return process.returncode, bytes(data)
    except subprocess.TimeoutExpired:
        raise RunnerError("command_timeout") from None
    finally:
        _kill_tree(process, job)
        process.wait(timeout=5)
        thread.join(1)
        if not thread.is_alive():
            process.stdout.close()
        _need(not thread.is_alive(), "command_stop_uncertain")


def _tree_hash(path):
    """Hash the actual run tree including trusted git metadata; never follow links.

    Streaming hash includes empty directories, file modes and link targets. It
    still works after a size refusal without retaining oversized content in RAM.
    """
    digest = hashlib.sha256()
    def visit(folder):
        for entry in sorted(os.scandir(folder), key=lambda e: e.name):
            target = Path(entry.path)
            st = entry.stat(follow_symlinks=False)
            row = [str(target.relative_to(path)).replace("\\", "/"), st.st_mode]
            if _link(st):
                row += ["link", os.readlink(target)]
            elif stat.S_ISREG(st.st_mode):
                content = hashlib.sha256()
                with target.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        content.update(chunk)
                row += ["file", st.st_size, content.hexdigest()]
            elif stat.S_ISDIR(st.st_mode):
                row += ["directory"]
            else:
                row += ["special"]
            digest.update(json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode() + b"\n")
            if stat.S_ISDIR(st.st_mode) and not _link(st):
                visit(target)
    visit(path)
    return digest.hexdigest()


def _discard(path, root):
    proof = {"path": str(path), "tree_sha256": None, "directory_absent": False}
    try:
        proof["tree_sha256"] = _tree_hash(path)
    except (OSError, ValueError, RecursionError):
        pass  # An incomplete hash is explicitly not a complete discard proof.
    try:
        _need(path.parent == root and path.resolve().parent == root, "unsafe_discard")
        def writable_retry(function, name, error):
            os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
            function(name)
        shutil.rmtree(path, onerror=writable_retry)
    except (OSError, RunnerError):
        pass
    proof["directory_absent"] = not os.path.lexists(path)
    return proof


def run_coding_task(spec, provider_fn, *, root, limits, credential_dirs=None, task_env=None):
    """Run one offline experiment, even though live enablement stays False.

    spec: {base_snapshot: directory/tar path, task_text: str,
           test_command: nonempty argv list, limits: optional tightening dict}.
    credential_dirs must be disjoint from root; links below root are refused.
    task_env explicitly passes non-secret variables to provider and tests only.
    limits use DEFAULT_LIMITS keys; neither caller nor spec can raise hard caps.
    provider_fn must be a trusted importable top-level callable (no lambdas or
    closures). Its return value is ignored. test counts represent ONE command
    check, not parsed/fabricated framework case counts. Nonzero tests return a
    valid non-green receipt and no patch. Every failure refuses patch export.
    Invalid inputs raise RunnerError before allocation; allocated runs return
    status/error_code/model_call_attempted/runner_receipt/diff/discard_proof.
    """
    _need(type(spec) is dict, "invalid_spec")
    bound = _limits(spec, limits)
    text, argv = spec.get("task_text"), spec.get("test_command")
    _need(type(text) is str and len(text.encode("utf-8")) <= bound["bytes"], "invalid_task")
    _need(type(argv) is list and argv and all(type(x) is str and x and "\x00" not in x for x in argv),
          "invalid_test_command")
    command = subprocess.list2cmdline(argv)
    _need(len(command) <= 200, "invalid_test_command")
    command.encode("utf-8")
    _need(callable(provider_fn) and "base_snapshot" in spec, "invalid_spec")
    root = _plain_path(root)
    _need(root.is_dir(), "invalid_root")
    _credential_policy(root, [] if credential_dirs is None else credential_dirs)
    _child_environment(root, {} if task_env is None else task_env)
    deadline = time.monotonic() + bound["time_seconds"]
    run = root / ("workspace-" + uuid.uuid4().hex)
    with _ALLOCATION_LOCK:
        _need(run.name not in _USED_IDS and not os.path.lexists(run), "workspace_reuse")
        try:
            run.mkdir()
        except FileExistsError:
            raise RunnerError("workspace_reuse") from None
        _USED_IDS.add(run.name)
    workspace, control = run / "work", run / "control"
    result = {"status": "failed", "error_code": None, "model_call_attempted": False,
              "runner_receipt": None, "diff": None, "discard_proof": None}
    patch = None
    try:
        workspace.mkdir(); control.mkdir()
        _materialize(spec["base_snapshot"], workspace, bound)
        env = _environment(control)
        child_env = _child_environment(workspace, {} if task_env is None else task_env)
        # Test scratch (HOME/TMP) sits beside the workspace, not in it, so test
        # side files never enter the result commit; it is discarded with the run.
        test_home = run / "test-home"
        test_home.mkdir()
        test_env = _child_environment(workspace, {} if task_env is None else task_env, home=test_home)
        git = shutil.which("git")
        _need(git is not None, "git_unavailable")
        def git_run(*args, cap=None):
            code, output = _command([git, "--git-dir=" + str(control / "git"),
                "--work-tree=" + str(workspace), "-c", "core.autocrlf=false",
                "-c", "core.hooksPath=" + str(control / "no-hooks"),
                "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false",
                "-c", "core.attributesFile=" + os.devnull, *args],
                workspace, env, deadline, cap if cap is not None else bound["bytes"])
            _need(code == 0, "git_error")
            return output
        git_run("init", "--object-format=sha1", "--template=")
        # Git metadata is outside the provider worktree, like runner-cert's
        # trusted verification repository. Never trust a provider-created .git.
        git_run("add", "--all", "--force")
        git_run("commit", "--allow-empty", "-m", "base")
        base_commit = git_run("rev-parse", "HEAD").decode().strip()
        result["model_call_attempted"] = True
        _provider(provider_fn, workspace, text,
                  min(deadline, time.monotonic() + bound["provider_seconds"]), child_env, bound)
        _snapshot(workspace, bound)
        try:
            code, _ = _command(argv, workspace, test_env,
                               min(deadline, time.monotonic() + bound["test_seconds"]), bound["bytes"], bound)
        except RunnerError as failure:
            if str(failure) == "command_timeout":
                raise RunnerError("test_timeout") from None
            raise
        _snapshot(workspace, bound)
        git_run("add", "--all", "--force")
        git_run("commit", "--allow-empty", "-m", "result")
        result_commit = git_run("rev-parse", "HEAD").decode().strip()
        try:
            patch = git_run("diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames",
                            "--unified=3", base_commit, result_commit, "--", cap=bound["diff_bytes"])
        except RunnerError as failure:
            if str(failure) == "output_limit":
                raise RunnerError("diff_limit") from None
            raise
        changed = git_run("diff", "--name-only", "-z", "--no-renames", base_commit, result_commit, "--")
        result["runner_receipt"] = {
            "base_commit": base_commit, "result_commit": result_commit,
            "diff_sha256": hashlib.sha256(patch).hexdigest(),
            "files_changed": len(changed.rstrip(b"\0").split(b"\0")) if changed else 0,
            "tests": {"command": command, "ran": 1, "passed": int(code == 0),
                      "failed": int(code != 0), "errors": 0}, "workspace_id": run.name,
        }
        _need(code == 0, "tests_failed")
        result["status"] = "succeeded"
    except BaseException as failure:
        result["error_code"] = str(failure) if isinstance(failure, RunnerError) else "runner_error"
    finally:
        result["discard_proof"] = _discard(run, root)
    proof = result["discard_proof"]
    if not proof["directory_absent"] or proof["tree_sha256"] is None:
        result.update(status="failed", error_code="discard_unproven")
    if result["status"] == "succeeded":
        result["diff"] = patch
    return result
