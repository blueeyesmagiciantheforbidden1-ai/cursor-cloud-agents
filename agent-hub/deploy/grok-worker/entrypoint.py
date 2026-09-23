"""Commission an immutable Linux package without claiming provider tasks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

AGENT = "grok"
VERSION = "1.0.40"
EXPECTED_SHA256 = "92c997dfd109c0672d40d5ae6fbd15835d53ffaf12cf9ea124d22aaef3ff23fc"
ROOT = Path("/opt/runcrew")
NATIVE = ROOT / AGENT / AGENT
CONFIG = Path("/run/config/worker.json")
HUB_URL = "https://runcrew-hub-kdhodumsza-uc.a.run.app"
EXPECTED_ACCOUNT_REF = "9ddbfe0cce4b6653b86b2057f45c398360541f21a100c1589a67a01cbc80aadc"
FIXED = {
    "hub_url": HUB_URL, "agent_id": AGENT, "executable": str(NATIVE),
    "token_env": "HUB_AGENT_TOKEN", "workspaces": {"default": "/workspace/default"},
    "cloud_run_auth_mode": "metadata", "billing_policy": {"mode": "subscription_only"},
    "model_policy_required": True, "model_policy_bundle": "/run/config/model-catalog.json",
    "execution_mode": "read_only", "expected_account_ref": EXPECTED_ACCOUNT_REF,
}
REQUIRED_HELP = ("--model", "--effort", "--permission-mode", "--sandbox", "--tools", "--no-subagents")

def provider_environment(home):
    env = {"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin",
           "LANG": "C.UTF-8", "TMPDIR": "/tmp"}
    env.update({"GROK_HOME": str(home / ".grok"), "GROK_DISABLE_AUTOUPDATER": "1",
                "GROK_CRASH_HANDLER": "0", "GROK_SUBAGENTS": "0", "GROK_MEMORY": "0"})
    return env

def immutable(path, regular=True):
    if path.resolve(strict=True) != path:
        raise ValueError("Linked native profile")
    for entry in (path, *path.parents):
        metadata = entry.lstat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022 or entry.is_symlink() or os.access(entry, os.W_OK):
            raise ValueError("Mutable native profile")
    if regular and not path.is_file():
        raise ValueError("Native profile file missing")

def image_check():
    if sys.platform != "linux" or os.geteuid() == 0:
        raise ValueError("Rootless Linux required")
    immutable(NATIVE)
    profile = NATIVE.parent / "runtime-profile.json"
    immutable(profile)
    if profile.stat().st_size > 4096:
        raise ValueError("Invalid native profile")
    body = json.loads(profile.read_text(encoding="utf-8"))
    with NATIVE.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if body.get("cli_version") != VERSION or body.get("sha256") != EXPECTED_SHA256 or digest != EXPECTED_SHA256:
        raise ValueError("Native image digest mismatch")
    return {"agent": AGENT, "version": VERSION, "image_profile_verified": True,
            "provider_login_verified": False, "provider_task_ready": False,
            "hub_connected": False, "model_calls": 0}

def native_readonly_check():
    """No provider credential, existing home, prompt, session, or model request."""
    with tempfile.TemporaryDirectory(prefix="runcrew-native-check-") as temporary:
        home = Path(temporary)
        environment = provider_environment(home)
        (home / ("." + AGENT)).mkdir(mode=0o700)
        for args in (["--version"], ["--help"]):
            # Fixed read-only commands cannot create descendants intentionally.
            process = subprocess.Popen([str(NATIVE), *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=temporary,
                env=environment, start_new_session=True)
            try:
                stdout, _ = process.communicate(timeout=20)
                if process.returncode != 0 or len(stdout) > 512_000:
                    raise ValueError("Native read-only command failed")
                text = stdout.decode("utf-8", errors="replace")
                if args == ["--version"] and VERSION not in text:
                    raise ValueError("Native version mismatch")
                if args == ["--help"] and any(flag not in text for flag in REQUIRED_HELP):
                    raise ValueError("Required native flags not available")
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
    return {"native_version_and_help_verified": True, "model_calls": 0}

def worker_config(value):
    allowed = set(FIXED) | {"worker_id", "timeout_seconds", "model_policy_agents"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unsupported commissioning configuration")
    if any(key in value and value[key] != expected for key, expected in FIXED.items()):
        raise ValueError("Commissioning configuration changed")
    worker_id = value.get("worker_id", AGENT + "-cloud")
    timeout = value.get("timeout_seconds", 180)
    fleet = value.get("model_policy_agents", [AGENT])
    if (not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", worker_id)
            or type(timeout) is not int or not 30 <= timeout <= 300
            or not isinstance(fleet, list) or AGENT not in fleet
            or any(not isinstance(item, str) or item not in ("codex", "claude", "cursor", "copilot", "grok") for item in fleet)
            or len(set(fleet)) != len(fleet)):
        raise ValueError("Invalid commissioning configuration")
    return {**FIXED, "worker_id": worker_id, "timeout_seconds": timeout, "model_policy_agents": fleet}

def heartbeat(value, inherited):
    hub_token = inherited.get("HUB_AGENT_TOKEN", "")
    if (not isinstance(hub_token, str) or not 20 <= len(hub_token) <= 16_384
            or any(c.isspace() or ord(c) < 32 for c in hub_token)):
        raise ValueError("Dedicated hub token required")
    environment = {"HOME": "/home/worker", "PATH": "/usr/local/bin:/usr/bin:/bin",
                   "LANG": "C.UTF-8", "TMPDIR": "/tmp", "HUB_AGENT_TOKEN": hub_token}
    os.environ.clear()
    os.environ.update(environment)
    sys.path.insert(0, str(ROOT / "app"))
    from agent_hub.worker import main as worker_main
    with tempfile.TemporaryDirectory(prefix="runcrew-heartbeat-") as temporary:
        config = Path(temporary) / "worker.json"
        config.write_text(json.dumps(worker_config(value)), encoding="utf-8")
        config.chmod(0o600)
        return worker_main(["--config", str(config), "--heartbeat-only"])

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--native-check", action="store_true")
    mode.add_argument("--heartbeat-only", action="store_true")
    mode.add_argument("--metadata-only", action="store_true")
    mode.add_argument("--once", action="store_true")
    args = parser.parse_args()
    # Fail before imports, credentials, task claim or provider process creation.
    if not (args.check or args.native_check or args.heartbeat_only or args.metadata_only):
        print(json.dumps({"agent": AGENT, "status": "blocked",
              "reason": "same_process_auth_model_gate_and_durable_credential_broker_required",
              "task_claimed": False, "model_calls": 0}))
        return 2
    try:
        os.umask(0o077)
        checked = image_check()
        if args.metadata_only:
            sys.path.insert(0, str(ROOT / "app"))
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from metadata import cloud_main
            result = cloud_main()
            print(json.dumps({"agent": AGENT, "version": VERSION,
                              "image_profile_verified": True, **result}, separators=(",", ":")))
            return 0
        if args.native_check:
            checked.update(native_readonly_check())
        if not args.heartbeat_only:
            print(json.dumps(checked))
            return 0
        if CONFIG.is_symlink() or CONFIG.stat().st_size > 16_384:
            raise ValueError("Invalid worker configuration")
        return heartbeat(json.loads(CONFIG.read_text(encoding="utf-8")), dict(os.environ))
    except (OSError, ValueError, RuntimeError, ImportError, TypeError, subprocess.SubprocessError):
        print(json.dumps({"agent": AGENT, "status": "rejected", "model_calls": 0}), file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
