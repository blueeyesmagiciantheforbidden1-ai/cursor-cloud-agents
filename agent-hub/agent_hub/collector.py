"""Read-only local observations, distinct from authenticated hub worker heartbeats.

Only known executable paths, process image names and selected collaboration
receipts are read. No environment, command line, auth file or process memory is
collected. Use on the operator machine or a dedicated Windows worker VM.
"""
from __future__ import annotations

import csv
import ctypes
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

from .telemetry import LABELS, ROSTER, iso


def bounded_text(path, limit=512_000):
    try:
        # Reject redirected receipt paths, including ancestor junctions.
        if path.resolve() != path.absolute() or any(part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()) for part in (path, *path.parents)):
            return None
        with path.open('rb') as stream:
            encoded = stream.read(limit+1)
        if len(encoded) > limit:
            return None
        return encoded.decode('utf-8-sig')
    except (OSError, UnicodeError):
        return None


def read_json(path):
    try:
        content = bounded_text(path)
        value = json.loads(content) if content is not None else None
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def process_counts():
    if os.name != "nt":
        return None
    try:
        result = subprocess.run([str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/tasklist.exe"),
                                 "/FO", "CSV", "/NH"], capture_output=True, timeout=8,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        counts = {}
        for row in csv.reader(io.StringIO(result.stdout.decode("utf-8", errors="replace"))):
            if row:
                name = row[0].lower()
                counts[name] = counts.get(name, 0) + 1
        return counts
    except (OSError, subprocess.SubprocessError):
        return None


class MemoryStatus(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                ("page_total", ctypes.c_ulonglong), ("page_available", ctypes.c_ulonglong),
                ("virtual_total", ctypes.c_ulonglong), ("virtual_available", ctypes.c_ulonglong),
                ("extended", ctypes.c_ulonglong)]


class LocalCollector:
    def __init__(self, project, *, home=None, clock=time.time, interval=15):
        self.project = Path(project).resolve()
        self.home = Path(home or Path.home())
        self.clock = clock
        self.interval = interval
        self.lock = threading.Lock()
        self.last = None
        self.next_sample = 0
        self.cpu_previous = None

    def inventory(self, observed):
        local = self.home / "AppData/Local"
        paths = {
            "codex": [],
            "claude": [self.home / "AppData/Roaming/Claude/claude-code/2.1.275/claude.exe",
                       local / "Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude/claude-code/2.1.275/claude.exe"],
            "cursor": [local / "Programs/cursor/Cursor.exe"],
            "copilot": [local / "github-copilot-sdk/cli/1.0.84-5/copilot.exe"],
            "grok": [self.home / ".grok/bin/grok.exe"],
        }
        images = {"codex": ("codex.exe",), "claude": ("claude.exe",),
                  "cursor": ("cursor.exe",), "copilot": ("copilot.exe",),
                  "grok": ("grok.exe",)}
        counts = process_counts()
        rows = []
        for agent in ROSTER:
            count = sum(counts.get(image, 0) for image in images[agent]) if counts is not None else None
            installed = any(path.is_file() for path in paths[agent]) or bool(count)
            rows.append({"agent_id": agent, "label": LABELS[agent], "installed": installed,
                         "process_count": count, "runtime_status": "unknown" if count is None else "running" if count else "stopped",
                         "checked_at": observed,
                         "note": "Local app/CLI process observation; does not confirm a connected task worker."})
        return rows

    def machine(self, observed):
        result = {"status": "unknown", "observed_at": observed, "platform": "Windows" if os.name == "nt" else os.name,
                  "logical_cpus": os.cpu_count(), "ram_total_gb": None, "ram_used_gb": None,
                  "ram_percent": None, "disk_free_gb": None, "cpu_percent": None}
        try:
            result["disk_free_gb"] = round(shutil.disk_usage(self.project).free / 2**30, 1)
            if os.name == "nt":
                memory = MemoryStatus()
                memory.length = ctypes.sizeof(memory)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
                    result.update(status="ok", ram_total_gb=round(memory.total / 2**30, 1),
                                  ram_used_gb=round((memory.total-memory.available) / 2**30, 1), ram_percent=memory.load)
                idle, kernel, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
                if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                    values = idle.value, kernel.value + user.value
                    if self.cpu_previous:
                        idle_delta, total_delta = [a-b for a,b in zip(values, self.cpu_previous)]
                        if total_delta > 0:
                            result["cpu_percent"] = round(max(0, min(100, 100*(1-idle_delta/total_delta))), 1)
                    self.cpu_previous = values
        except (OSError, AttributeError):
            pass
        return result

    def activity(self):
        rows = []
        def add(path, identifier, agent, status, title, detail):
            try:
                rows.append({"id": identifier, "agent_id": agent, "status": status, "title": title,
                             "detail": detail, "observed_at": iso(path.stat().st_mtime), "source": "saved collaboration receipt"})
            except OSError:
                pass
        pilot = self.project / "collaboration/live-pilot-result.json"
        receipt = read_json(pilot)
        room = receipt.get("room")
        messages = room.get('messages') if isinstance(room, dict) else None
        if (receipt.get("worker_exit_code") == 0 and isinstance(room, dict) and room.get("status") == "completed"
                and room.get('agents') == ['codex', 'copilot'] and isinstance(messages, list) and len(messages) == 2
                and all(isinstance(message, dict) and message.get('agent') == agent and message.get('exit_code') == 0
                        and isinstance(message.get('text'), str) and bool(message['text'].strip())
                        for message, agent in zip(messages, ('codex', 'copilot')))):
            add(pilot, "copilot-relay", "copilot", "completed", "Codex + Copilot relay completed",
                "Two real contributions completed a local hub room. This was a prompt-only collaboration check.")
        grok = self.project.parent / "grok-architecture-review.md"
        grok_text = bounded_text(grok, 64_000)
        if grok_text and len(grok_text) > 100:
            add(grok, "grok-review", "grok", "completed", "Grok architecture review delivered",
                "Independent recommendations recorded for cloud isolation, worker credentials and recovery.")
        claude = self.project / "CLAUDE-FOLLOWUP.md"
        content = bounded_text(claude, 64_000)
        if content:
            if "CLAUDE-002 acknowledged" in content and "19/19" in content and "stdio bridge" in content:
                add(claude, "claude-mcp", "claude", "completed", "Claude verification report received",
                    "Claude reported real HTTP and stdio client connections plus 19/19 checks in each latest local test suite. Standalone model login still needs setup.")
        cursor = self.project / "collaboration/cursor-sdk-review.json"
        receipt = read_json(cursor)
        if receipt.get("status") == "completed":
            add(cursor, "cursor-sdk", "cursor", "completed", "Cursor SDK review delivered",
                "A separate, bounded Cursor agent returned a real conceptual review. No project files were sent.")
        elif receipt.get("status") == "blocked":
            add(cursor, "cursor-sdk", "cursor", "blocked", "Cursor SDK connection needs repair",
                "The Windows SDK bridge failed before a model request. An open Cursor desktop app does not imply the hub can dispatch to it.")
        return sorted(rows, key=lambda item: item["observed_at"], reverse=True)

    def quota(self):
        # An explicit provider snapshot written by the operator adapter, never an
        # auth/config directory scan. Every public field is validated here.
        data = read_json(self.project / "runtime/codex-usage.json")
        rows = []
        windows = data.get("windows")
        for item in windows[:8] if isinstance(windows, list) else []:
            if not isinstance(item, dict):
                continue
            used, minutes, reset = (item.get(k) for k in ("used_percent", "window_minutes", "resets_at"))
            observed = data.get("observed_at")
            if (type(used) not in (int, float) or not 0 <= used <= 100 or type(minutes) is not int or minutes <= 0
                    or type(reset) not in (int, float) or not 0 < reset < 1e11 or type(observed) not in (int, float)
                    or not 0 < observed <= self.clock()+60 or reset <= observed):
                continue
            rows.append({"provider": "codex", "scope": "account", "metric": "quota_percent",
                         "label": "Weekly allowance" if minutes == 10080 else f"{minutes}-minute allowance",
                         "used": used, "remaining": 100-used, "limit": 100, "unit": "%",
                         "window_minutes": minutes, "resets_at": iso(reset), "observed_at": iso(observed),
                         "source": "provider_api", "status": "available" if self.clock()-observed <= 900 and self.clock()<reset else "stale", "error": None})
        extra, observed = data.get('extra_credits'), data.get('observed_at')
        if (type(extra) in (int, float) and 0 <= extra <= 1e15 and type(observed) in (int, float)
                and 0 < observed <= self.clock()+60):
            rows.append({'provider': 'codex', 'scope': 'account', 'metric': 'credits', 'label': 'Extra credits',
                         'used': None, 'remaining': extra, 'limit': None, 'unit': 'credits', 'source': 'provider_api',
                         'observed_at': iso(observed), 'status': 'available' if self.clock()-observed <= 900 else 'stale',
                         'error': None})
        return rows

    def snapshot(self):
        with self.lock:
            now = self.clock()
            if self.last is not None and now < self.next_sample:
                return self.last
            observed = iso(now)
            activity = self.activity()
            relay_verified = any(row['id'] == 'copilot-relay' for row in activity)
            result = {"inventory": self.inventory(observed), "machine": self.machine(observed),
                      "activity": activity, "local_usage": self.quota(),
                      "collector": {"status": "ok", "observed_at": observed, "interval_seconds": self.interval, "source": "local-machine"},
                      "milestones": [
                          {"id": "local", "title": "Local collaboration hub", "status": "ready" if relay_verified else "in_progress", "detail": "Real Copilot relay completed locally; saved receipt available." if relay_verified else "Waiting for a verified collaboration relay receipt."},
                          {"id": "domain", "title": "New Google organization", "status": "blocked", "detail": "RunCrew Cloud / runcrewcloud.com proposed. Domain registration and accurate owner signup details are still required."},
                          {"id": "cloud", "title": "Google Cloud with login", "status": "blocked", "detail": "Cloud Run, separate identities, Firestore and Google IAP deployment prepared. No cloud service has been deployed."},
                          {"id": "plugin", "title": "Private ChatGPT app · two accounts", "status": "in_progress", "detail": "Plugin package prepared. Live tunnel, account registration and both private installations follow cloud deployment."},
                          {"id": "research", "title": "Measured improvement pipeline", "status": "in_progress", "detail": "Implementing bounded task contracts, context caching and independently evaluated candidate archives."},
                          {"id": "retina", "title": "Retina backup import", "status": "blocked", "detail": "The Stage 1 / Research source has not been located in the checked backup archives."},
                      ]}
            self.last, self.next_sample = result, now+self.interval
            return result

    def enrich_snapshot(self, data):
        local = dict(self.snapshot())
        data = dict(data)
        usage = local.pop("local_usage")
        measured = {(row['provider'], row['scope'], row['metric']) for row in usage}
        data["usage"] = [row for row in data.get('usage', []) if (row.get('provider'), row.get('scope'), row.get('metric')) not in measured] + usage
        data.update(local)
        memory = local["machine"].get("ram_percent")
        if memory is not None and memory >= 90:
            data["alerts"] = list(data.get("alerts", [])) + [{"id": "local-memory", "severity": "warning", "kind": "capacity",
                "title": "Local machine memory is nearly full", "message": f"{memory}% RAM is in use. This can prevent agent processes from starting.",
                "agent_id": None, "observed_at": local["machine"]["observed_at"]}]
        return data
