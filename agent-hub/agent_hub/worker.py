"""Outbound-only, standard-library worker for the private collaboration hub.

Local JSON config: cloud_run_auth_mode="metadata" uses the explicitly selected
GCE attached service account with hub_url as its token audience. The hub must
use an HTTPS origin (run.app or an explicitly configured custom audience).
cloud_run_auth_mode="gcloud" uses the local gcloud user login. Legacy
cloud_run_auth=true selects gcloud. With neither setting, no Google identity
endpoint is queried. X-Hub-Token remains required in every mode.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass, replace
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import MappingProxyType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .adapters import (AGENTS, EXECUTION_MODES, MAX_OUTPUT_BYTES, AdapterError, Command, bounded_text,
                       build_command, build_prompt, extract_output, extract_output_state, resolve_executable)
from .billing_policy import (AuthEvidence, BillingPolicy, BillingPolicyError,
                             enforce_billing_policy, parse_billing_policy)
from .subscription_auth import preflight_subscription_route
from .claude_runtime import run_verified_claude


COMPLETION_RESERVE_SECONDS = 15


class WorkerError(RuntimeError):
    pass


class LeaseLost(WorkerError):
    pass


@dataclass(frozen=True)
class Config:
    hub_url: str
    agent_id: str
    token: str
    token_env: str
    workspaces: dict[str, Path]
    executable: str | list[str] | None = None
    poll_seconds: float = 3
    timeout_seconds: int = 300
    cloud_run_auth: bool = False
    gcloud_executable: str | list[str] | None = None
    cloud_run_auth_mode: str | None = None
    worker_id: str = 'default'
    billing_policy: BillingPolicy = BillingPolicy()
    model_policy_required: bool = False
    model_policy_bundle: Path | None = None
    expected_account_ref: str | None = None
    execution_mode: str = 'read_only'
    model_policy_agents: tuple[str, ...] = tuple(AGENTS)


def load_config(path: str | None, *, dry_run: bool = False) -> Config:
    data: dict[str, Any] = {}
    if path:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise WorkerError("Worker config must be a JSON object")
    hub_url = os.environ.get("HUB_URL", data.get("hub_url", "http://127.0.0.1:8080"))
    if not isinstance(hub_url, str):
        raise WorkerError("hub_url must be a URL string")
    parsed = urlsplit(hub_url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise WorkerError("hub_url must be an origin URL without credentials, paths, or queries")
    if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise WorkerError("Remote hubs must use HTTPS")
    agent_id = os.environ.get("HUB_AGENT_ID", data.get("agent_id", ""))
    if agent_id not in AGENTS:
        raise WorkerError("Set HUB_AGENT_ID or agent_id to codex, claude, cursor, or copilot")
    token_env = data.get("token_env", "HUB_AGENT_TOKEN")
    if not isinstance(token_env, str) or not token_env:
        raise WorkerError("token_env must name a local environment variable")
    token = os.environ.get(token_env, "")
    if not dry_run and not token:
        raise WorkerError(f"Set the worker token in environment variable {token_env}")
    if "\r" in token or "\n" in token:
        raise WorkerError("Worker token contains invalid characters")
    configured = data.get("workspaces", {})
    if not isinstance(configured, dict) or not configured:
        raise WorkerError("Configure at least one local workspace alias")
    workspaces: dict[str, Path] = {}
    for alias, value in configured.items():
        if not isinstance(alias, str) or not alias or not isinstance(value, str):
            raise WorkerError("Each workspace must map an alias to an absolute local directory")
        workspace = Path(value).expanduser()
        if not workspace.is_absolute():
            raise WorkerError("Workspace paths must be absolute")
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise WorkerError("Configured workspace is not a directory")
        workspaces[alias] = workspace
    try:
        poll = float(data.get("poll_seconds", 3))
        timeout = int(data.get("timeout_seconds", 300))
    except (TypeError, ValueError) as exc:
        raise WorkerError("Polling and timeout settings must be numbers") from exc
    if not 1 <= poll <= 60 or not 10 <= timeout <= 900:
        raise WorkerError("poll_seconds must be 1..60 and timeout_seconds must be 10..900")
    cloud_run_auth = data.get("cloud_run_auth", False)
    if not isinstance(cloud_run_auth, bool):
        raise WorkerError("cloud_run_auth must be true or false")
    auth_mode = data.get("cloud_run_auth_mode")
    if auth_mode is not None and auth_mode not in ("metadata", "gcloud"):
        raise WorkerError("cloud_run_auth_mode must be metadata or gcloud")
    if auth_mode == "metadata" and parsed.scheme != "https":
        raise WorkerError("Metadata authentication requires an HTTPS hub origin")
    if auth_mode is not None:
        cloud_run_auth = True
    elif cloud_run_auth:
        auth_mode = "gcloud"
    worker_id = data.get('worker_id', 'default')
    if not isinstance(worker_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', worker_id):
        raise WorkerError('worker_id must be a local label using letters, numbers, hyphens, or underscores')
    try:
        billing_policy = parse_billing_policy(data.get('billing_policy'))
    except BillingPolicyError as exc:
        raise WorkerError(str(exc)) from None
    model_required = data.get('model_policy_required', False)
    if type(model_required) is not bool:
        raise WorkerError('model_policy_required must be true or false')
    model_bundle = data.get('model_policy_bundle')
    if model_bundle is not None:
        if not isinstance(model_bundle, str) or not model_bundle or not Path(model_bundle).is_absolute():
            raise WorkerError('model_policy_bundle must be an absolute local path')
        model_bundle = Path(model_bundle)
    account_ref = data.get('expected_account_ref')
    if account_ref is not None and (not isinstance(account_ref, str) or not re.fullmatch(r'[a-f0-9]{64}', account_ref)):
        raise WorkerError('expected_account_ref must be the normalized owner email SHA256')
    if model_required and (model_bundle is None or account_ref is None or billing_policy.mode != 'subscription_only'):
        raise WorkerError('Required model policy needs its local bundle, expected owner, and subscription-only billing')
    execution_mode = data.get('execution_mode', 'read_only')
    if execution_mode not in EXECUTION_MODES:
        raise WorkerError('execution_mode must be read_only or project_work')
    if execution_mode == 'project_work' and (agent_id != 'claude' or not model_required):
        raise WorkerError('Project work requires the verified Claude worker and its local model policy')
    policy_agents = data.get('model_policy_agents', list(AGENTS))
    if (not isinstance(policy_agents, list) or not policy_agents
            or any(not isinstance(agent, str) or agent not in AGENTS for agent in policy_agents)
            or len(set(policy_agents)) != len(policy_agents) or agent_id not in policy_agents):
        raise WorkerError('model_policy_agents must be distinct supported agents including this worker')
    return Config(hub_url.rstrip("/"), agent_id, token, token_env, workspaces,
                  data.get("executable"), poll, timeout, cloud_run_auth,
                  data.get("gcloud_executable"), auth_mode, worker_id, billing_policy,
                  model_required, model_bundle, account_ref, execution_mode, tuple(policy_agents))


class TelemetryReporter:
    """Independent reporting must never keep a task lease alive or delay work."""
    def __init__(self, config):
        self.client = HubClient(config)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.state = {'worker_id': config.worker_id, 'status': 'idle',
                      'auth_status': 'unknown', 'current_room_id': None,
                      'last_exit_code': None, 'usage': []}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def update(self, **changes):
        with self.lock:
            self.state.update(changes)

    def _send(self):
        with self.lock:
            state = dict(self.state)
        try:
            self.client.post('/v1/workers/report', state)
        except (WorkerError, OSError, ValueError):
            # The dashboard will show the last successful observation as stale.
            pass

    def _run(self):
        while not self.stop.is_set():
            self._send()
            self.stop.wait(30)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=12)
        # Best effort; do not race the reporter's client if a request is stuck.
        if not self.thread.is_alive():
            self.update(status='offline', current_room_id=None)
            self._send()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never forward a worker credential to another host.
        return None


class HubClient:
    def __init__(self, config: Config):
        self.config = config
        self.opener = build_opener(NoRedirect())
        self.identity_token: str | None = None
        self.identity_expires = 0.0

    def _identity(self) -> str:
        if self.identity_token and time.monotonic() < self.identity_expires:
            return self.identity_token
        if self.config.cloud_run_auth_mode == "metadata":
            token = self._metadata_identity()
        else:
            token = self._gcloud_identity()
        self.identity_token = token
        self.identity_expires = time.monotonic() + 480
        return token

    def _metadata_identity(self) -> str:
        # Metadata access is opt-in. No redirects or environment-configured
        # proxies may transport this request or the returned service credential.
        origin = urlsplit(self.config.hub_url)
        if (origin.scheme != "https" or not origin.hostname or origin.username
                or origin.password or origin.path not in ("", "/")
                or origin.query or origin.fragment):
            raise WorkerError("Metadata authentication requires an HTTPS hub origin")
        audience = self.config.hub_url.rstrip("/")
        url = ("http://metadata.google.internal/computeMetadata/v1/instance/"
               "service-accounts/default/identity?audience="
               + quote(audience, safe="") + "&format=full")
        request = Request(url, headers={"Metadata-Flavor": "Google"}, method="GET")
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=5) as response:
                if response.headers.get("Metadata-Flavor") != "Google":
                    raise WorkerError("GCE identity response lacked its metadata header")
                encoded = response.read(16_385)
        except (HTTPError, URLError, OSError) as exc:
            raise WorkerError("Could not obtain the configured GCE service identity") from exc
        if len(encoded) > 16_384:
            raise WorkerError("GCE service identity response exceeded the size limit")
        try:
            token = encoded.decode("ascii").strip()
        except UnicodeError as exc:
            raise WorkerError("GCE service identity returned an invalid token") from exc
        if not token or any(char.isspace() for char in token):
            raise WorkerError("GCE service identity returned an invalid token")
        return token

    def _gcloud_identity(self) -> str:
        argv = resolve_executable(self.config.gcloud_executable, "gcloud")
        try:
            result = subprocess.run([*argv, "auth", "print-identity-token"],
                                    check=False, capture_output=True, text=True,
                                    timeout=20, shell=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkerError("Could not obtain a Cloud Run identity token") from exc
        token = result.stdout.strip()
        if result.returncode or not token or "\n" in token or "\r" in token:
            raise WorkerError("gcloud identity authentication failed; check the local gcloud login")
        return token

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        google_auth = self.config.cloud_run_auth or self.config.cloud_run_auth_mode is not None
        for attempt in range(2):
            headers = {"Content-Type": "application/json", "X-Hub-Token": self.config.token,
                       "X-Hub-Agent": self.config.agent_id}
            if google_auth:
                headers["Authorization"] = "Bearer " + self._identity()
            request = Request(self.config.hub_url + path, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                              headers=headers, method="POST")
            try:
                with self.opener.open(request, timeout=10) as response:
                    encoded = response.read(256_001)
                break
            except HTTPError as exc:
                if exc.code == 409:
                    raise LeaseLost("The hub revoked or expired this task lease") from exc
                if google_auth and exc.code in (401, 403):
                    self.identity_expires = 0
                    self.identity_token = None
                    if attempt == 0:
                        exc.close()
                        continue
                raise WorkerError(f"Hub request failed (HTTP {exc.code})") from exc
            except (OSError, URLError) as exc:
                raise WorkerError("Hub connection failed") from exc
        if len(encoded) > 256_000:
            raise WorkerError("Hub response exceeds the size limit")
        try:
            result = json.loads(encoded)
        except ValueError as exc:
            raise WorkerError("Hub returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise WorkerError("Hub response must be a JSON object")
        return result


class BoundedOutput:
    def __init__(self):
        self.value = bytearray()
        self.lock = threading.Lock()
        self.truncated = False

    def drain(self, stream):
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                with self.lock:
                    self.value.extend(chunk)
                    if len(self.value) > 96_000:
                        del self.value[:-96_000]
                        self.truncated = True
        finally:
            stream.close()

    def text(self) -> str:
        with self.lock:
            text = bytes(self.value).decode("utf-8", "replace")
            return ("[Earlier output truncated]\n" if self.truncated else "") + text


class WindowsJob:
    """Kill-on-close job also removes descendants after the CLI itself exits."""
    def __init__(self, process: subprocess.Popen):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_longlong), ("JobTime", ctypes.c_longlong),
                        ("Flags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
                        ("MaxWorkingSet", ctypes.c_size_t), ("ActiveProcesses", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD),
                        ("Scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("Basic", BasicLimits), ("Io", IoCounters),
                        ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t),
                        ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                  ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise WorkerError("Could not create the Windows process cleanup job")
        limits = ExtendedLimits()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise WorkerError("Could not configure the Windows process cleanup job")
        if not kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            self.close()
            raise WorkerError("Could not contain the CLI in a Windows process cleanup job")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def terminate_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        if process.poll() is None:
            # The PID comes exclusively from our Popen object, never from a task.
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            try:
                subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, check=False, shell=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def redact(text: str, config: Config, client: HubClient) -> str:
    secrets = [config.token, client.identity_token or ""]
    for key, value in os.environ.items():
        if any(word in key.upper() for word in ("TOKEN", "API_KEY", "SECRET", "PASSWORD")):
            if len(value) >= 6:
                secrets.append(value)
    for value in sorted(set(secrets), key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED]")
    return bounded_text(text, MAX_OUTPUT_BYTES)


def preflight_auth_route(agent_id: str, command: Command, workspace: Path,
                         environment: Mapping[str, str]) -> AuthEvidence | None:
    """Audit the fixed cloud Claude profile; other providers remain unknown.

    Separate-process status cannot prove effective managed policy. A Claude
    launch with a validated model plan uses claude_runtime's same-process check.
    Neither task data nor an agent's response can supply that proof.
    """
    return preflight_subscription_route(agent_id, command, workspace, environment)


def run_command(command: Command, workspace: Path, timeout: float,
                heartbeat, *, heartbeat_seconds: float = 10,
                private_env: tuple[str, ...] = (),
                billing_policy: BillingPolicy | None = None,
                agent_id: str | None = None, model_plan: dict | None = None,
                execution_mode: str = 'read_only') -> tuple[int, str]:
    """Materialize any task prompt outside argv, and always remove it afterward."""
    if command.prompt_file is not None:
        index = command.prompt_file_argument
        if type(index) is not int or not 0 <= index < len(command.argv):
            raise WorkerError('Prompt-file argument is not configured')
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='runcrew-prompt-') as directory:
            path = Path(directory) / 'prompt.txt'
            path.write_text(command.prompt_file, encoding='utf-8')
            path.chmod(0o600)
            argv = list(command.argv)
            argv[index] = str(path)
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                return 124, 'Task execution skipped: prompt setup exhausted the deadline.'
            actual = replace(command, argv=tuple(argv), prompt_file=None, prompt_file_argument=None)
            return _run_command(actual, workspace, remaining, heartbeat,
                                heartbeat_seconds=heartbeat_seconds, private_env=private_env,
                                billing_policy=billing_policy, agent_id=agent_id, model_plan=model_plan,
                                execution_mode=execution_mode)
    return _run_command(command, workspace, timeout, heartbeat,
                        heartbeat_seconds=heartbeat_seconds, private_env=private_env,
                        billing_policy=billing_policy, agent_id=agent_id, model_plan=model_plan,
                        execution_mode=execution_mode)


def _run_command(command: Command, workspace: Path, timeout: float,
                 heartbeat, *, heartbeat_seconds: float = 10,
                 private_env: tuple[str, ...] = (),
                 billing_policy: BillingPolicy | None = None,
                 agent_id: str | None = None, model_plan: dict | None = None,
                 execution_mode: str = 'read_only') -> tuple[int, str]:
    """Run one CLI and revoke its entire process tree on loss of the task lease."""
    started = time.monotonic()
    deadline = started + timeout
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith("HUB_") or name in private_env:
            del environment[name]
    policy = billing_policy if billing_policy is not None else BillingPolicy()
    if not isinstance(policy, BillingPolicy):
        raise BillingPolicyError('A validated BillingPolicy is required')
    if execution_mode not in EXECUTION_MODES:
        raise WorkerError('Unsupported local execution mode')
    if execution_mode == 'project_work' and (agent_id != 'claude' or model_plan is None):
        raise WorkerError('Project work requires the verified Claude model and authentication handshake')
    if agent_id == 'claude' and model_plan is not None:
        selection = model_plan.get('selections', {}).get('claude')
        # Only execute_task's independently loaded local model policy supplies
        # this object; hub tasks cannot inject a model plan or auth evidence.
        return run_verified_claude(command, workspace, max(0, deadline - time.monotonic()), heartbeat,
                                   environment=MappingProxyType(environment), selection=selection,
                                   billing_policy=policy, terminate_tree=terminate_tree,
                                   contain_process=WindowsJob, heartbeat_seconds=heartbeat_seconds,
                                   transient_errors=(WorkerError,), lease_lost_errors=(LeaseLost,),
                                   plan_expires_at=model_plan.get('expires_at'), mode=execution_mode)
    if policy.mode == 'subscription_only':
        if agent_id not in AGENTS:
            raise BillingPolicyError('Subscription-only execution requires a configured provider')
        evidence = preflight_auth_route(agent_id, command, workspace, MappingProxyType(environment))
        enforce_billing_policy(agent_id, environment, policy, evidence)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    output = BoundedOutput()
    process = subprocess.Popen(command.argv, cwd=workspace, env=environment, shell=False,
                               stdin=subprocess.PIPE if command.stdin is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               creationflags=flags, start_new_session=os.name != "nt")
    try:
        job = WindowsJob(process)
    except BaseException:
        terminate_tree(process)
        if process.stdout:
            process.stdout.close()
        if process.stdin:
            process.stdin.close()
        raise
    reader = threading.Thread(target=output.drain, args=(process.stdout,), daemon=True)
    reader.start()
    reason = None
    last_heartbeat = started
    next_heartbeat = last_heartbeat + heartbeat_seconds
    writer = None
    def write_prompt():
        try:
            process.stdin.write(command.stdin.encode("utf-8"))
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
    try:
        if command.stdin is not None:
            # A CLI that never reads stdin must not block timeout/cancellation.
            writer = threading.Thread(target=write_prompt, daemon=True)
            writer.start()
        while process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                reason = "Task exceeded the local execution timeout."
                break
            if now >= next_heartbeat:
                try:
                    if not heartbeat():
                        raise LeaseLost("Task was cancelled")
                    last_heartbeat = time.monotonic()
                except LeaseLost:
                    reason = "Task stopped because its lease was cancelled or expired."
                    break
                except WorkerError:
                    if time.monotonic() - last_heartbeat >= 30:
                        reason = "Task stopped because the hub could not renew its lease."
                        break
                next_heartbeat = time.monotonic() + heartbeat_seconds
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        if reason:
            terminate_tree(process)
        returncode = 124 if reason else process.wait()
    except BaseException:
        terminate_tree(process)
        raise
    finally:
        if process.poll() is None:
            terminate_tree(process)
        job.close()
        if os.name != "nt":
            terminate_tree(process)
        reader.join(timeout=2)
        if writer:
            writer.join(timeout=2)
    text = output.text()
    if reason:
        text += "\n" + reason
    return returncode, text


def execute_task(config: Config, client: HubClient, task: dict[str, Any]) -> int:
    started = time.monotonic()
    room_id, lease = task.get("room_id"), task.get("lease_token")
    if not isinstance(room_id, str) or not room_id or not isinstance(lease, str) or not lease:
        raise WorkerError("Hub task lacks a room ID or lease token")
    endpoint = "/v1/tasks/" + quote(room_id, safe="")
    try:
        alias = task.get("workspace")
        if not isinstance(alias, str) or alias not in config.workspaces:
            raise WorkerError("Task workspace alias is not allowed on this worker")
        workspace = config.workspaces[alias]
        # Recheck in case a configured directory was replaced after startup.
        if workspace.resolve(strict=True) != workspace or not workspace.is_dir():
            raise WorkerError("Configured workspace changed or is unavailable")
        requested_timeout = task.get("timeout_seconds", config.timeout_seconds)
        if isinstance(requested_timeout, bool) or not isinstance(requested_timeout, int) or requested_timeout <= 0:
            raise WorkerError("Task timeout must be a positive integer")
        if config.execution_mode == 'project_work' and not config.model_policy_required:
            raise WorkerError('Project work requires the local model policy')
        prompt = build_prompt(task, config.agent_id, execution_mode=config.execution_mode)
        command = build_command(config.agent_id, prompt, config.executable,
                                execution_mode=config.execution_mode)
        model_plan = None
        if config.model_policy_required:
            from .model_runtime import select_worker_model
            command, model_plan = select_worker_model(config.agent_id, config.model_policy_bundle,
                                                      config.expected_account_ref, command,
                                                      execution_mode=config.execution_mode,
                                                      active_agents=config.model_policy_agents)
        heartbeat = lambda: client.post(endpoint + "/heartbeat", {"lease_token": lease}).get("active") is True
        heartbeat_started = time.monotonic()
        initial_heartbeat = client.post(endpoint + "/heartbeat", {"lease_token": lease})
        if initial_heartbeat.get("active") is not True:
            raise LeaseLost("Task is no longer active")
        checked_at = time.monotonic()
        elapsed_setup = checked_at - started
        remaining = requested_timeout - elapsed_setup
        claimed_deadline = task.get("deadline")
        server_deadline = initial_heartbeat.get("deadline", claimed_deadline)
        if server_deadline is not None:
            if (type(server_deadline) not in (int, float) or not math.isfinite(server_deadline)
                    or claimed_deadline is not None and server_deadline != claimed_deadline):
                raise WorkerError("Hub returned an invalid or changed task deadline")
            server_time = initial_heartbeat.get("server_time")
            if server_time is not None:
                if type(server_time) not in (int, float) or not math.isfinite(server_time):
                    raise WorkerError("Hub returned an invalid server time")
                # Subtract the full request duration conservatively. This avoids
                # trusting the VPS wall clock to match the controller clock.
                remaining = min(remaining, server_deadline - server_time - (checked_at - heartbeat_started))
            else:
                remaining = min(remaining, server_deadline - time.time())
        reserve = min(COMPLETION_RESERVE_SECONDS, requested_timeout / 2)
        timeout = min(config.timeout_seconds - elapsed_setup, remaining - reserve)
        if timeout <= 0:
            exit_code, raw = 124, "Task execution skipped: insufficient time remains to return its result before the hub deadline."
        else:
            exit_code, raw = run_command(command, workspace, timeout, heartbeat,
                                         private_env=(config.token_env,),
                                         billing_policy=config.billing_policy, agent_id=config.agent_id,
                                         model_plan=model_plan, execution_mode=config.execution_mode)
        text, complete_final = extract_output_state(config.agent_id, raw)
        if config.agent_id == 'grok' and not complete_final:
            exit_code = exit_code or 1
            text = 'Grok response did not contain a complete successful result.\n\n' + text
        if raw.startswith("[Earlier output truncated]") and not complete_final:
            exit_code = exit_code or 1
            text = "CLI output exceeded the capture limit; the result is incomplete.\n\n" + text
        elif len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
            exit_code = exit_code or 1
            text = "CLI reply exceeded the hub output limit; the result is incomplete.\n\n" + text
        if exit_code == 124:
            reason = raw.rsplit("\n", 1)[-1]
            if text != reason:
                text = reason + "\n\n" + text
    except LeaseLost:
        raise
    except (WorkerError, AdapterError, OSError, ValueError) as exc:
        exit_code, text = 1, f"Worker could not execute this task: {exc}"
    payload = {"lease_token": lease, "exit_code": exit_code,
               "output": redact(text, config, client)}
    # Retry only the idempotent completion operation; never rerun the CLI.
    for attempt in range(3):
        try:
            completion = client.post(endpoint + "/complete", payload)
            if completion.get("status") in ("failed", "stalled", "cancelled"):
                return exit_code or 1
            return exit_code
        except LeaseLost:
            raise
        except WorkerError:
            if attempt == 2:
                raise WorkerError("Task ran, but delivery of its result could not be confirmed")
            time.sleep(attempt + 1)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    reporter = None
    def request_stop(signum, frame):
        raise KeyboardInterrupt
    # A service stop must unwind run_command and close its process-cleanup job.
    signal.signal(signal.SIGTERM, request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Local JSON worker configuration")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Poll once; execute at most one task")
    mode.add_argument("--dry-run", action="store_true", help="Offline command preview; does not claim or complete tasks")
    mode.add_argument("--heartbeat-only", action="store_true",
                      help="Publish one completed-job heartbeat; never claim work or contact a model provider")
    parser.add_argument("--workspace", help="Workspace alias for the offline dry run")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, dry_run=args.dry_run)
        # Fail at startup if the installed CLI is missing or a shell launcher.
        command = build_command(config.agent_id, "Offline command profile preview.", config.executable,
                                execution_mode=config.execution_mode)
        if args.dry_run:
            alias = args.workspace or next(iter(config.workspaces))
            if alias not in config.workspaces:
                raise WorkerError("Unknown dry-run workspace alias")
            print(json.dumps({"mode": "offline-dry-run", "agent": config.agent_id,
                              "hub": config.hub_url, "workspace_alias": alias,
                              "execution_mode": config.execution_mode,
                              "model_policy_agents": config.model_policy_agents,
                              "argv": command.preview(), "stdin": "<prompt-redacted>" if command.stdin else None,
                              "executes_agent": False, "claims_or_completes_tasks": False}, indent=2))
            return 0
        client = HubClient(config)
        if args.heartbeat_only:
            # No reporter thread, claim, model catalog, provider auth probe or
            # provider subprocess is started in this commissioning mode.
            receipt = client.post('/v1/workers/report', {
                'worker_id': config.worker_id, 'status': 'offline',
                'auth_status': 'unknown', 'current_room_id': None,
                'last_exit_code': None, 'usage': [],
            })
            if receipt.get('accepted') is not True:
                raise WorkerError('Hub did not acknowledge the commissioning heartbeat')
            print(json.dumps({'mode': 'heartbeat-only', 'telemetry_delivered': True,
                              'worker_status': 'offline', 'provider_login_verified': False,
                              'inference_performed': False, 'claims_or_completes_tasks': False}), flush=True)
            return 0
        reporter = TelemetryReporter(config)
        reporter.start()
        print(f"{config.agent_id} worker ready; outbound polling enabled.", flush=True)
        while True:
            try:
                response = client.post("/v1/tasks/claim", {})
                task = response.get("task")
                if task is not None:
                    if not isinstance(task, dict):
                        raise WorkerError("Hub task must be an object")
                    reporter.update(status='busy', current_room_id=task.get('room_id'))
                    result = execute_task(config, client, task)
                    reporter.update(status='idle', current_room_id=None, last_exit_code=result,
                                    auth_status='verified' if result == 0 else 'unknown')
                    print(f"Task finished with exit code {result}.", flush=True)
                    if args.once:
                        return 0 if result == 0 else 1
                elif args.once:
                    print("No task available.")
                    return 0
            except (WorkerError, AdapterError) as exc:
                reporter.update(status='error', current_room_id=None)
                print(f"Worker: {exc}", file=sys.stderr, flush=True)
                if args.once:
                    return 1
            time.sleep(config.poll_seconds)
    except (WorkerError, AdapterError, OSError, ValueError) as exc:
        print(f"Worker configuration error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Worker stopped.", file=sys.stderr)
        return 130
    finally:
        if reporter is not None:
            reporter.close()


if __name__ == "__main__":
    raise SystemExit(main())
