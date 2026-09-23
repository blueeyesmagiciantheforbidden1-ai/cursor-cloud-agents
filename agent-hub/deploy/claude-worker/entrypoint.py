"""Run one private Claude worker job with a constructed, minimal environment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile

APP = Path("/opt/runcrew/app")
EXECUTABLE = "/opt/runcrew/claude/claude"
HUB_URL = "https://runcrew-hub-kdhodumsza-uc.a.run.app"
CONFIG = Path("/run/config/worker.json")
CATALOG = "/run/config/model-catalog.json"
FIXED = {
    "hub_url": HUB_URL,
    "agent_id": "claude",
    "executable": EXECUTABLE,
    "token_env": "HUB_AGENT_TOKEN",
    "workspaces": {"default": "/workspace/default"},
    "cloud_run_auth_mode": "metadata",
    "billing_policy": {"mode": "subscription_only"},
    "model_policy_required": True,
    "model_policy_bundle": CATALOG,
    "execution_mode": "project_work",
}


def prepare_config(value: object) -> dict:
    allowed = set(FIXED) | {"expected_account_ref", "timeout_seconds", "worker_id", "model_policy_agents"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Worker configuration contains unsupported fields")
    for key, expected in FIXED.items():
        if key in value and value[key] != expected:
            raise ValueError("Worker configuration differs from the dedicated cloud contract")
    account = value.get("expected_account_ref")
    if not isinstance(account, str) or re.fullmatch(r"[a-f0-9]{64}", account) is None:
        raise ValueError("A verified intended-account reference is required")
    timeout = value.get("timeout_seconds", 180)
    if type(timeout) is not int or not 30 <= timeout <= 300:
        raise ValueError("Worker deadline must be 30..300 seconds")
    worker_id = value.get("worker_id", "claude-cloud")
    if not isinstance(worker_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,48}", worker_id) is None:
        raise ValueError("Worker identifier is invalid")
    # The deployed fleet is trusted operator configuration, never a task field.
    # All five remain the default; staged bring-up must be explicitly configured.
    supported = ("codex", "claude", "cursor", "copilot", "grok")
    agents = value.get("model_policy_agents", list(supported))
    if (not isinstance(agents, list) or not 1 <= len(agents) <= len(supported)
            or any(not isinstance(agent, str) or agent not in supported for agent in agents)
            or len(set(agents)) != len(agents) or "claude" not in agents):
        raise ValueError("Model policy fleet must include Claude and distinct supported agents")
    return {**FIXED, "expected_account_ref": account, "timeout_seconds": timeout,
            "worker_id": worker_id, "model_policy_agents": list(agents)}


def private_environment(inherited: dict[str, str], *, heartbeat_only: bool = False) -> dict[str, str]:
    if heartbeat_only:
        # Connectivity commissioning has no reason to receive provider secrets.
        # Metadata identity requires no GOOGLE_* environment or key file.
        environment = {"HOME": "/home/worker", "PATH": "/usr/local/bin:/usr/bin:/bin",
                       "LANG": "C.UTF-8", "TMPDIR": "/tmp"}
    else:
        from agent_hub.subscription_auth import claude_environment
        environment = claude_environment(inherited.get("CLAUDE_CODE_OAUTH_TOKEN", ""))
    hub_token = inherited.get("HUB_AGENT_TOKEN", "")
    if not hub_token or len(hub_token) > 16_384 or any(char.isspace() for char in hub_token):
        raise ValueError("The dedicated Claude hub credential is missing or malformed")
    environment["HUB_AGENT_TOKEN"] = hub_token
    return environment


def worker_arguments(config_path: Path, *, heartbeat_only: bool) -> list[str]:
    return ["--config", str(config_path), "--heartbeat-only" if heartbeat_only else "--once"]


def immutable_image_check() -> dict:
    from agent_hub.subscription_auth import _profile_matches
    if sys.platform != "linux" or os.geteuid() == 0 or not _profile_matches():
        raise ValueError("Image native profile requires a verified rootless Linux runtime")
    for directory in (Path("/home/worker"), Path("/home/worker/.claude")):
        metadata = directory.stat()
        if directory.resolve() != directory or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise ValueError("Private Claude home ownership or permissions do not match")
    return {"image_profile_verified": True, "inference_performed": False,
            "provider_login_verified": False, "hub_connected": False}


def eligibility_probe(inherited: dict[str, str]) -> int:
    from agent_hub.subscription_auth import _strict_json, claude_environment
    path = Path('/run/probe/authorization.json')
    metadata = path.stat()
    if (path.is_symlink() or not path.is_file() or metadata.st_size > 49_152
            or metadata.st_uid != 0 or metadata.st_mode & 0o022 or os.access(path, os.W_OK)):
        raise ValueError('A protected bounded commissioning authorization is required')
    authorization = _strict_json(path.read_bytes())
    if not isinstance(authorization, dict) or set(authorization) != {'operator', 'billing', 'enrollment'}:
        raise ValueError('Commissioning authorization has an unsupported shape')
    environment = claude_environment(inherited.get('CLAUDE_CODE_OAUTH_TOKEN', ''))
    os.environ.clear()
    os.environ.update(environment)
    os.umask(0o077)
    os.chdir('/workspace/default')
    sys.path.insert(0, '/opt/runcrew')
    from eligibility_probe import run_probe
    report = run_probe(authorization['operator'], authorization['billing'], authorization['enrollment'],
                       environment=environment, timeout_seconds=180)
    print(json.dumps(report), flush=True)
    return 0 if report.get('outcome') == 'verified_eligibility' else 1


def room_review(inherited: dict[str, str]) -> int:
    """An operator-selected single-room review; no general queue worker fallback."""
    from agent_hub.subscription_auth import _strict_json, claude_environment
    from agent_hub.worker import Config, HubClient
    path = Path('/run/probe/authorization.json')
    metadata = path.stat()
    if (path.is_symlink() or not path.is_file() or metadata.st_size > 49_152
            or metadata.st_uid != 0 or metadata.st_mode & 0o022 or os.access(path, os.W_OK)):
        raise ValueError('A protected bounded room authorization is required')
    authorization = _strict_json(path.read_bytes())
    if not isinstance(authorization, dict) or set(authorization) != {'operator', 'billing', 'enrollment', 'room'}:
        raise ValueError('Room authorization has an unsupported shape')
    environment = claude_environment(inherited.get('CLAUDE_CODE_OAUTH_TOKEN', ''))
    hub_token = inherited.get('HUB_AGENT_TOKEN', '')
    if not hub_token or len(hub_token) > 16_384 or any(c.isspace() for c in hub_token):
        raise ValueError('A dedicated Claude hub credential is required')
    config = Config(HUB_URL, 'claude', hub_token, 'HUB_AGENT_TOKEN',
                    {'default': Path('/workspace/default')}, cloud_run_auth_mode='metadata')
    client = HubClient(config)
    # Only the hub client object retains the controller token. Native child sees
    # exactly the previously audited provider environment, no Google/hub secrets.
    os.environ.clear()
    os.environ.update(environment)
    os.umask(0o077)
    os.chdir('/workspace/default')
    sys.path.insert(0, '/opt/runcrew')
    from room_review import run_room_review
    report = run_room_review(authorization, environment=environment, client=client)
    print(json.dumps(report), flush=True)
    return 0 if report.get('outcome') == 'completed' else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Offline image integrity check only")
    mode.add_argument("--heartbeat-only", action="store_true",
                      help="Test private hub connectivity only, without provider credentials or task claims")
    mode.add_argument('--eligibility-probe', action='store_true',
                      help='Explicitly authorized, one-shot tool-free provider eligibility check')
    mode.add_argument('--room-review', action='store_true',
                      help='One protected, digest-bound Claude room review with all tools disabled')
    args = parser.parse_args()
    sys.path.insert(0, str(APP))
    try:
        checked = immutable_image_check()
        if args.check:
            print(json.dumps(checked))
            return 0
        if args.eligibility_probe:
            return eligibility_probe(dict(os.environ))
        if args.room_review:
            return room_review(dict(os.environ))
        if CONFIG.is_symlink() or CONFIG.stat().st_size > 16_384:
            raise ValueError("Worker configuration file is not an approved small JSON document")
        config = prepare_config(json.loads(CONFIG.read_text(encoding="utf-8")))
        environment = private_environment(dict(os.environ), heartbeat_only=args.heartbeat_only)
        os.environ.clear()
        os.environ.update(environment)
        sys.dont_write_bytecode = True
        os.umask(0o077)
        os.chdir("/workspace/default")
        from agent_hub.worker import main as worker_main
        with tempfile.TemporaryDirectory(prefix="runcrew-worker-", dir="/tmp") as temporary:
            path = Path(temporary) / "worker.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            path.chmod(0o600)
            return worker_main(worker_arguments(path, heartbeat_only=args.heartbeat_only))
    except (OSError, ValueError) as exc:
        # Never include configuration values or provider/hub credentials in logs.
        print(f"Claude worker startup rejected: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
