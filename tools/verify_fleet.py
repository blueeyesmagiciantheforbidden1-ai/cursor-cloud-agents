"""Verify the five-agent RunCrew fleet through the hub HTTP API.

Pass/fail is decided by the coordinator, not by a worker announcing success.
Each room prompt carries a fresh nonce and a per-agent token of the form
``NONCE-AGENTNAME``. An agent validates only when its reply, aside from
surrounding whitespace, is exactly that token. Exit code 0 is recorded as
execution and is not itself a pass.

Gates, in order: capability, roster, fleet, duplicate, load, expiry.
Invoke one as a subcommand, or ``all`` to run that sequence. With no
subcommand the fleet gate runs, so the existing flags keep working.

The manager token is read from HUB_MANAGER_TOKEN and sent as X-Hub-Token.
The Cloud Run identity token is read from HUB_ID_TOKEN and sent as
Authorization: Bearer. Token values, nonces, and expected reply tokens are
never printed or written to the ledger.

Standard library only. This tool does not call gcloud, Cloud Run, or Firestore
itself; it only talks to the hub URL given with --hub. Request bodies keep the
existing room fields; anything newer on the response is optional.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

AGENTS = ("codex", "claude", "cursor", "copilot", "grok")
GATE_ORDER = ("capability", "roster", "fleet", "duplicate", "load", "expiry")
GATES = GATE_ORDER + ("all",)
# 'all' leaves expiry out: its room is an ordinary solve room with no short
# queue deadline, so healthy workers can finish it before it could expire
# (Demand's review). Run `expiry` on its own; it stays advisory.
ALL_GATES = tuple(gate for gate in GATE_ORDER if gate != "expiry")
# Interim campaign rule: 180 s until every worker renews its lease mid-turn.
DEFAULT_ROOM_TIMEOUT = 180
POLL_SECONDS = 10
DEADLINE_SECONDS = 15 * 60
TERMINAL_STATUSES = frozenset({
    "completed", "failed", "stalled",
    "blocked_on_provider", "retry_scheduled", "needs_reconciliation", "expired",
})
ACCEPTABLE_AGENT_STATUS = frozenset({"ready", "restarting"})
MANIFEST_KEYS = ("capability_manifest", "manifest", "capabilities")
VALUE_OPTIONS = frozenset({
    "--hub", "--ledger", "--consecutive", "--max-rooms", "--room-timeout", "--load-rooms",
})
SKIPPED_EXPIRY_REASON = "hub does not report queue_deadline support"


class HubError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never forward the manager or identity token.
        return None


def validate_hub_url(url):
    parsed = urlsplit(url)
    loopback = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or (parsed.scheme != "https" and not loopback)):
        raise ValueError("Use HTTPS except for loopback local testing")
    return url.rstrip("/")


def token_from_env(env, name):
    value = env.get(name) if env is not None else None
    if (not isinstance(value, str) or not value.isascii()
            or any(ord(character) < 33 or ord(character) > 126 for character in value)):
        raise ValueError(f"Set {name} to a single-line ASCII token before calling the hub")
    return value


def redact(text, secrets_to_hide):
    if not isinstance(text, str):
        text = str(text)
    for secret in sorted((item for item in secrets_to_hide if item), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return text


def display_room_id(value):
    if isinstance(value, str) and len(value) == 32 and all(character in "0123456789abcdef" for character in value):
        return value
    return None


def build_fixture(agents):
    """Per-room nonce and per-agent token. The token is NONCE-AGENTNAME."""
    nonce = secrets.token_hex(8)
    tokens = {agent: nonce + "-" + agent.upper() for agent in agents}
    sentences = [
        "Each agent must reply with exactly its assigned token and no other text.",
        "Room nonce: " + nonce + ".",
    ]
    for agent in agents:
        sentences.append(agent + " replies " + tokens[agent] + ".")
    return nonce, tokens, " ".join(sentences)


def messages_by_agent(room):
    grouped = {agent: [] for agent in AGENTS}
    messages = room.get("messages") if isinstance(room, dict) else None
    if not isinstance(messages, list):
        return grouped
    for message in messages:
        if isinstance(message, dict) and message.get("agent") in grouped:
            grouped[message["agent"]].append(message)
    return grouped


def execution_status(messages):
    if not messages:
        return {"exit_code": None, "delivered": False, "ok": False}
    codes = []
    for message in messages:
        code = message.get("exit_code") if isinstance(message, dict) else None
        codes.append(code if type(code) is int else None)
    return {
        "exit_code": codes[0],
        "delivered": True,
        "ok": all(code == 0 for code in codes),
    }


def validation_status(messages, token):
    if not messages:
        return "missing"
    if len(messages) > 1:
        return "duplicate"
    text = messages[0].get("text") if isinstance(messages[0], dict) else None
    if isinstance(text, str) and text.strip() == token:
        return "token_matched"
    return "wrong_token"


def room_status_text(room):
    status = room.get("status") if isinstance(room, dict) else None
    if isinstance(status, str) and status.isascii() and status.isprintable() and 1 <= len(status) <= 64:
        return status
    return "unknown"


def assess_room(room, tokens):
    grouped = messages_by_agent(room)
    agents = {}
    for agent, token in tokens.items():
        found = grouped.get(agent, [])
        agents[agent] = {
            "execution_status": execution_status(found),
            "validation_status": validation_status(found, token),
            "message_count": len(found),
        }
    unexpected = [agent for agent in AGENTS if agent not in tokens and grouped.get(agent)]
    status = room_status_text(room)
    passed = status == "completed" and not unexpected and bool(agents) and all(
        item["message_count"] == 1
        and item["execution_status"]["ok"]
        and item["execution_status"]["delivered"]
        and item["execution_status"]["exit_code"] == 0
        and item["validation_status"] == "token_matched"
        for item in agents.values()
    )
    return {
        "room_status": status,
        "agents": agents,
        "unexpected_agents": unexpected,
        "pass": passed,
        "exit_codes": {agent: item["execution_status"]["exit_code"] for agent, item in agents.items()},
    }


def _created_at(room):
    value = room.get("created_at") if isinstance(room, dict) else None
    if type(value) in (int, float) and type(value) is not bool:
        return value
    return None


def _one_line(text):
    return "".join(character if character.isprintable() else " " for character in text).strip()


def _public_scalar(value):
    if type(value) is bool or value is None or type(value) is int:
        return value
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, str) and value.isascii() and value.isprintable() and len(value) <= 200:
        return value
    return None


def _public_value(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(key, str) and key.isascii() and key.isprintable() and 1 <= len(key) <= 64:
                result[key] = _public_scalar(item) if not isinstance(item, (dict, list)) else _public_value(item)
        return result
    if isinstance(value, list):
        return [_public_scalar(item) if not isinstance(item, (dict, list)) else _public_value(item)
                for item in value[:32]]
    return _public_scalar(value)


def manifest_record(agent):
    """Copy capability manifest fields when a hub sends them. Absence is empty."""
    recorded = {}
    fields = []
    if not isinstance(agent, dict):
        return recorded, fields
    for key in MANIFEST_KEYS:
        if key not in agent:
            continue
        value = _public_value(agent[key])
        recorded[key] = value
        if isinstance(value, dict):
            fields.extend(key + "." + nested for nested in value)
        elif isinstance(value, list):
            fields.append(key)
            fields.extend(item for item in value if isinstance(item, str))
        else:
            fields.append(key)
    return recorded, fields


def _agent_name(item, fallback=None):
    if isinstance(item, dict):
        for key in ("id", "agent_id", "agent", "name"):
            if item.get(key) in AGENTS:
                return item[key]
    if fallback in AGENTS:
        return fallback
    return None


def iter_status_agents(snapshot):
    """Yield (agent, record) from the shapes hubs already return or may add."""
    if not isinstance(snapshot, dict):
        return
    agents = snapshot.get("agents")
    if isinstance(agents, list):
        for item in agents:
            name = _agent_name(item)
            if name is not None:
                yield name, item if isinstance(item, dict) else {}
    elif isinstance(agents, dict):
        for key, item in agents.items():
            name = _agent_name(item, key)
            if name is None:
                continue
            if isinstance(item, dict):
                yield name, item
            elif isinstance(item, str):
                yield name, {"status": item}


def queue_deadline_supported(snapshot):
    """True only when the hub itself reports queue_deadline support."""
    if not isinstance(snapshot, dict):
        return False, None
    if snapshot.get("queue_deadline_supported") is True:
        return True, "queue_deadline_supported"
    feature = snapshot.get("queue_deadline")
    if feature is True:
        return True, "queue_deadline"
    if isinstance(feature, dict) and feature.get("supported") is True:
        return True, "queue_deadline.supported"
    for key in ("features", "capabilities", "supports"):
        value = snapshot.get(key)
        if isinstance(value, list) and "queue_deadline" in value:
            return True, key
        if isinstance(value, dict) and value.get("queue_deadline") is True:
            return True, key + ".queue_deadline"
    return False, None


def effective_roster(room):
    """Prefer the hub's effective_roster. Older hubs only have agents."""
    if not isinstance(room, dict):
        return None, "missing", False
    if "effective_roster" in room:
        value = room.get("effective_roster")
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value), "effective_roster", True
        return None, "effective_roster", True
    agents = room.get("agents")
    if isinstance(agents, list) and all(isinstance(item, str) for item in agents):
        return list(agents), "agents", False
    return None, "missing", False


class HubClient:
    def __init__(self, base_url, manager_token, identity_token, timeout=30):
        self.base_url = validate_hub_url(base_url)
        self.manager_token = manager_token
        self.identity_token = identity_token
        self.timeout = timeout
        self.room_timeout = 300
        self._extra_secrets = []
        self.opener = build_opener(_NoRedirect())

    @property
    def secrets(self):
        return (self.manager_token, self.identity_token, *self._extra_secrets)

    def note_secret(self, value):
        if isinstance(value, str) and value and value not in self._extra_secrets:
            self._extra_secrets.append(value)

    def _headers(self):
        return {
            "X-Hub-Token": self.manager_token,
            "Authorization": "Bearer " + self.identity_token,
            "Content-Type": "application/json",
        }

    def request(self, method, path, data=None):
        body = None if data is None else json.dumps(data).encode("utf-8")
        call = Request(self.base_url + path, data=body, headers=self._headers(), method=method)
        try:
            with self.opener.open(call, timeout=self.timeout) as response:
                raw = response.read(512_001)
        except HTTPError as exc:
            detail = _error_detail(exc)
            raise HubError(redact(detail, self.secrets), exc.code) from None
        except URLError:
            raise HubError("Cannot reach hub; check URL and connectivity") from None
        except (OSError, TimeoutError):
            raise HubError("Cannot reach hub; check URL and connectivity") from None
        if len(raw) > 512_000:
            raise HubError("Hub response exceeds the size limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HubError("Hub returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise HubError("Hub returned invalid JSON")
        return payload

    def create_room(self, agents, prompt):
        # Existing fields only. Deployed hubs ignore nothing they already require,
        # and deployed workers keep reading prompt/agents/timeout_seconds.
        return self.request("POST", "/v1/rooms", {
            "prompt": prompt,
            "agents": list(agents),
            "timeout_seconds": self.room_timeout,
            "workspace": "default",
            "purpose": "project",
        })

    def get_room(self, room_id):
        return self.request("GET", "/v1/rooms/" + room_id)

    def get_status(self):
        return self.request("GET", "/v1/status")


def _error_detail(exc):
    detail = f"HTTP {exc.code}"
    try:
        raw = exc.read(10000)
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            error = payload["error"]
            if error and len(error) <= 300 and error.isprintable():
                detail = f"HTTP {exc.code}: {error}"
    except (OSError, UnicodeDecodeError, ValueError, AttributeError):
        pass
    finally:
        exc.close()
    return detail


def wait_for_rooms(client, room_ids, *, deadline_seconds, poll_seconds, sleep, clock):
    deadline = clock() + deadline_seconds
    latest = {room_id: None for room_id in room_ids}
    pending = list(room_ids)
    while pending:
        still_waiting = []
        for room_id in pending:
            try:
                room = client.get_room(room_id)
            except HubError as exc:
                if exc.status in (401, 403):
                    raise
                if clock() < deadline:
                    still_waiting.append(room_id)
                continue
            latest[room_id] = room
            status = room.get("status") if isinstance(room, dict) else None
            if status not in TERMINAL_STATUSES and clock() < deadline:
                still_waiting.append(room_id)
        pending = still_waiting
        if not pending or clock() >= deadline:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(poll_seconds, remaining))
    return latest


def _emit(line, secrets_to_hide, stream=None):
    print(redact(line, secrets_to_hide), file=stream, flush=True)


def _print_room(entry, streak, consecutive, secrets_to_hide):
    result = "pass" if entry["pass"] else "fail"
    _emit(
        f"room {entry['room_id'] or 'invalid-room-id'} created_at={entry['created_at']} "
        f"room_status={entry['room_status']} result={result}",
        secrets_to_hide,
    )
    for agent, record in entry["agents"].items():
        execution = record["execution_status"]
        code = "missing" if execution["exit_code"] is None else str(execution["exit_code"])
        delivered = "yes" if execution["delivered"] else "no"
        _emit(
            f"  {agent} exit_code={code} delivered={delivered} "
            f"validation={record['validation_status']}",
            secrets_to_hide,
        )
    if consecutive is not None:
        _emit(f"consecutive={streak}/{consecutive}", secrets_to_hide)


def _room_entry(created, final, tokens):
    assessed = assess_room(final if isinstance(final, dict) else {}, tokens)
    room_id = display_room_id(created.get("id") if isinstance(created, dict) else None)
    if room_id is None and isinstance(final, dict):
        room_id = display_room_id(final.get("id"))
    return {
        "room_id": room_id,
        "created_at": _created_at(created) if isinstance(created, dict) else _created_at(final),
        "room_status": assessed["room_status"],
        "status": assessed["room_status"],
        "exit_codes": assessed["exit_codes"],
        "agents": assessed["agents"],
        "unexpected_agents": assessed["unexpected_agents"],
        "pass": assessed["pass"],
    }


def _ledger_document(entries):
    if any(entry["result"] == "fail" for entry in entries):
        result = "fail"
    elif entries and all(entry["result"] == "skipped" for entry in entries):
        result = "skipped"
    else:
        result = "pass"
    return {"result": result, "gates": entries}


def _scrub(value, secrets_to_hide):
    if isinstance(value, str):
        # Room ids are random hex too. Leave an id that already validates untouched.
        if display_room_id(value):
            return value
        return redact(value, secrets_to_hide)
    if isinstance(value, dict):
        return {key: _scrub(item, secrets_to_hide) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, secrets_to_hide) for item in value]
    return value


def write_ledger(path, payload, secrets_to_hide=()):
    destination = Path(path)
    safe = _scrub(payload, secrets_to_hide)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(json.dumps(safe, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    except OSError:
        raise HubError("Could not write the ledger") from None


def _remember(client, nonce, tokens):
    client.note_secret(nonce)
    for token in tokens.values():
        client.note_secret(token)


def _open_room(client, agents):
    nonce, tokens, prompt = build_fixture(agents)
    _remember(client, nonce, tokens)
    created = client.create_room(agents, prompt)
    return created, tokens


def _require_room_id(created):
    room_id = display_room_id(created.get("id") if isinstance(created, dict) else None)
    if room_id is None:
        raise HubError("Hub returned an invalid room id")
    return room_id


def _finish(path, entries, client):
    document = _ledger_document(entries)
    write_ledger(path, document, client.secrets)
    return document


def _failed_gate(name, reason, evidence=None):
    return {
        "gate": name,
        "result": "fail",
        "pass": False,
        "reason": reason,
        "evidence": evidence or {},
    }


def run_capability(client):
    snapshot = client.get_status()
    found = {}
    for name, record in iter_status_agents(snapshot):
        found[name] = record
    agents = {}
    offline = []
    for agent in AGENTS:
        record = found.get(agent)
        status = record.get("status") if isinstance(record, dict) else None
        if not isinstance(status, str) or not status.isascii() or not status.isprintable() or len(status) > 64:
            status = None
        manifests, fields = manifest_record(record)
        if status == "offline":
            offline.append(agent)
        agents[agent] = {
            "status": status,
            "present": record is not None,
            "capability_manifest_present": bool(manifests),
            "capability_manifest_fields": fields,
            "capability_manifest": manifests,
        }
    passed = bool(found) and not offline and all(
        agents[agent]["present"] and agents[agent]["status"] in ACCEPTABLE_AGENT_STATUS
        for agent in AGENTS
    )
    evidence = {"agents": agents, "offline": offline}
    _emit("capability result=" + ("pass" if passed else "fail"), client.secrets)
    for agent in AGENTS:
        info = agents[agent]
        shown = "absent" if not info["capability_manifest_fields"] else ",".join(info["capability_manifest_fields"])
        state = "missing" if info["status"] is None else info["status"]
        _emit(f"  {agent} status={state} manifest_fields={shown}", client.secrets)
    return {
        "gate": "capability",
        "result": "pass" if passed else "fail",
        "pass": passed,
        "evidence": evidence,
    }


def _collect_room(client, agents, *, deadline_seconds, poll_seconds, sleep, clock):
    created, tokens = _open_room(client, agents)
    room_id = _require_room_id(created)
    _emit(f"created room {room_id}", client.secrets)
    final = wait_for_rooms(client, [room_id], deadline_seconds=deadline_seconds,
                           poll_seconds=poll_seconds, sleep=sleep, clock=clock)[room_id]
    if not isinstance(final, dict):
        final = created
    if not final.get("id"):
        final = {**final, "id": room_id}
    return _room_entry(created, final, tokens), final


def _roster_from_view(view, agent, entry):
    roster, field, present = effective_roster(view)
    message_count = entry["agents"].get(agent, {}).get("message_count", 0)
    execution = entry["agents"].get(agent, {}).get("execution_status", {})
    validation = entry["agents"].get(agent, {}).get("validation_status", "missing")
    roster_ok = roster == [agent]
    passed = bool(entry["pass"] and roster_ok and message_count == 1 and not entry["unexpected_agents"])
    entry = dict(entry)
    entry.update({
        "agent": agent,
        "roster_field": field,
        "effective_roster_present": present,
        "effective_roster": roster,
        "message_count": message_count,
        "execution_status": execution,
        "validation_status": validation,
        "pass": passed,
    })
    return entry


def run_roster_gate(client, *, deadline_seconds, poll_seconds, sleep, clock):
    rooms = []
    try:
        for agent in AGENTS:
            entry, final = _collect_room(client, (agent,), deadline_seconds=deadline_seconds,
                                         poll_seconds=poll_seconds, sleep=sleep, clock=clock)
            entry = _roster_from_view(final, agent, entry)
            rooms.append(entry)
            _print_room(entry, None, None, client.secrets)
    except HubError as exc:
        if exc.status in (401, 403):
            raise
        _emit(f"Error: {exc}", client.secrets, sys.stderr)
        return {
            "gate": "roster",
            "result": "fail",
            "pass": False,
            "reason": redact(str(exc), client.secrets),
            "evidence": {"rooms": rooms},
        }
    passed = len(rooms) == len(AGENTS) and all(room["pass"] for room in rooms)
    _emit("roster result=" + ("pass" if passed else "fail"), client.secrets)
    return {
        "gate": "roster",
        "result": "pass" if passed else "fail",
        "pass": passed,
        "evidence": {"rooms": rooms},
    }


def run_fleet(client, *, consecutive, max_rooms, deadline_seconds, poll_seconds, sleep, clock,
              on_progress):
    rooms = []
    streak = 0
    try:
        for _ in range(max_rooms):
            entry, _view = _collect_room(client, AGENTS, deadline_seconds=deadline_seconds,
                                         poll_seconds=poll_seconds, sleep=sleep, clock=clock)
            streak = streak + 1 if entry["pass"] else 0
            rooms.append(entry)
            on_progress(rooms, streak)
            _print_room(entry, streak, consecutive, client.secrets)
            if streak >= consecutive:
                _emit(f"fleet result=pass consecutive={streak}/{consecutive}", client.secrets)
                return _fleet_entry(consecutive, max_rooms, rooms, streak)
    except HubError as exc:
        on_progress(rooms, streak)
        if exc.status in (401, 403):
            raise
        failed = _fleet_entry(consecutive, max_rooms, rooms, streak)
        failed["reason"] = redact(str(exc), client.secrets)
        _emit(f"Error: {exc}", client.secrets, sys.stderr)
        _emit(
            f"fleet result=fail consecutive={streak}/{consecutive} after {len(rooms)} rooms",
            client.secrets,
        )
        return failed
    _emit(
        f"fleet result=fail consecutive={streak}/{consecutive} after {len(rooms)} rooms",
        client.secrets,
    )
    return _fleet_entry(consecutive, max_rooms, rooms, streak)


def _fleet_entry(consecutive, max_rooms, rooms, streak):
    passed = streak >= consecutive and consecutive > 0
    return {
        "gate": "fleet",
        "result": "pass" if passed else "fail",
        "pass": passed,
        "evidence": {
            "consecutive_required": consecutive,
            "max_rooms": max_rooms,
            "streak": streak,
            "rooms": rooms,
        },
    }


def _duplicate_agents(entry):
    return [agent for agent, record in entry["agents"].items()
            if record["validation_status"] == "duplicate" or record["message_count"] > 1]


def run_duplicate(client, *, deadline_seconds, poll_seconds, sleep, clock):
    entry, _view = _collect_room(client, AGENTS, deadline_seconds=deadline_seconds,
                                 poll_seconds=poll_seconds, sleep=sleep, clock=clock)
    duplicates = _duplicate_agents(entry)
    passed = entry["pass"] and not duplicates
    entry = dict(entry)
    entry["pass"] = passed
    _print_room(entry, None, None, client.secrets)
    _emit("duplicate result=" + ("pass" if passed else "fail"), client.secrets)
    return {
        "gate": "duplicate",
        "result": "pass" if passed else "fail",
        "pass": passed,
        "evidence": {"duplicates": duplicates, "rooms": [entry]},
    }


def run_load(client, *, rooms_required, room_timeout, poll_seconds, sleep, clock):
    opened = []
    for _ in range(rooms_required):
        created, tokens = _open_room(client, AGENTS)
        room_id = _require_room_id(created)
        opened.append((created, tokens, room_id))
        _emit(f"created room {room_id}", client.secrets)
    started = clock()
    latest = wait_for_rooms(
        client, [item[2] for item in opened], deadline_seconds=room_timeout,
        poll_seconds=poll_seconds, sleep=sleep, clock=clock,
    )
    elapsed = clock() - started
    rooms = []
    for created, tokens, room_id in opened:
        final = latest.get(room_id)
        if not isinstance(final, dict):
            final = created
        rooms.append(_room_entry(created, final, tokens))
    completed_in_time = elapsed <= room_timeout and all(room["room_status"] == "completed" for room in rooms)
    passed = completed_in_time and len(rooms) == rooms_required and all(room["pass"] for room in rooms)
    for room in rooms:
        _print_room(room, None, None, client.secrets)
    _emit(
        f"load result={'pass' if passed else 'fail'} rooms={len(rooms)}/{rooms_required} "
        f"elapsed={elapsed}",
        client.secrets,
    )
    return {
        "gate": "load",
        "result": "pass" if passed else "fail",
        "pass": passed,
        "evidence": {
            "requested_rooms": rooms_required,
            "room_timeout": room_timeout,
            "elapsed_seconds": elapsed,
            "within_room_timeout": completed_in_time,
            "rooms": rooms,
        },
    }


def _deadline_value(room):
    if not isinstance(room, dict) or "queue_deadline" not in room:
        return None
    value = room.get("queue_deadline")
    if type(value) in (int, float) and type(value) is not bool:
        return value
    if isinstance(value, str) and value.isascii() and value.isprintable() and len(value) <= 64:
        return value
    return {"present": True}


def run_expiry(client, *, deadline_seconds, poll_seconds, sleep, clock):
    snapshot = client.get_status()
    supported, field = queue_deadline_supported(snapshot)
    if not supported:
        _emit("expiry result=skipped reason=" + SKIPPED_EXPIRY_REASON, client.secrets)
        return {
            "gate": "expiry",
            "result": "skipped",
            "pass": None,
            "reason": SKIPPED_EXPIRY_REASON,
            "evidence": {"queue_deadline_supported": False},
        }
    entry, view = _collect_room(client, AGENTS, deadline_seconds=deadline_seconds,
                                poll_seconds=poll_seconds, sleep=sleep, clock=clock)
    passed = entry["room_status"] == "expired"
    _print_room(entry, None, None, client.secrets)
    _emit("expiry result=" + ("pass" if passed else "fail"), client.secrets)
    return {
        "gate": "expiry",
        "result": "pass" if passed else "fail",
        "pass": passed,
        "evidence": {
            "queue_deadline_supported": True,
            "support_field": field,
            "queue_deadline_present": isinstance(view, dict) and "queue_deadline" in view,
            "queue_deadline": _deadline_value(view),
            "rooms": [entry],
        },
    }


def _run_named(name, client, *, consecutive, max_rooms, load_rooms, room_timeout,
               deadline_seconds, poll_seconds, sleep, clock, ledger_path, entries, partial):
    def progress(rooms, streak):
        entry = _fleet_entry(consecutive, max_rooms, rooms, streak)
        partial["fleet"] = entry
        _finish(ledger_path, list(entries) + [entry], client)

    if name == "capability":
        return run_capability(client)
    if name == "roster":
        return run_roster_gate(client, deadline_seconds=deadline_seconds, poll_seconds=poll_seconds,
                               sleep=sleep, clock=clock)
    if name == "fleet":
        return run_fleet(client, consecutive=consecutive, max_rooms=max_rooms,
                         deadline_seconds=deadline_seconds, poll_seconds=poll_seconds,
                         sleep=sleep, clock=clock, on_progress=progress)
    if name == "duplicate":
        return run_duplicate(client, deadline_seconds=deadline_seconds, poll_seconds=poll_seconds,
                             sleep=sleep, clock=clock)
    if name == "load":
        return run_load(client, rooms_required=load_rooms, room_timeout=room_timeout,
                        poll_seconds=poll_seconds, sleep=sleep, clock=clock)
    if name == "expiry":
        return run_expiry(client, deadline_seconds=deadline_seconds, poll_seconds=poll_seconds,
                          sleep=sleep, clock=clock)
    raise ValueError("unknown gate")


def run_gates(client, gates, *, consecutive, max_rooms, load_rooms, room_timeout, ledger_path,
              poll_seconds, deadline_seconds, sleep, clock):
    entries = []
    for name in gates:
        partial = {}
        try:
            entry = _run_named(
                name, client, consecutive=consecutive, max_rooms=max_rooms, load_rooms=load_rooms,
                room_timeout=room_timeout, deadline_seconds=deadline_seconds, poll_seconds=poll_seconds,
                sleep=sleep, clock=clock, ledger_path=ledger_path, entries=entries, partial=partial,
            )
        except HubError as exc:
            if name == "fleet" and "fleet" in partial:
                failed = partial["fleet"]
                failed["result"] = "fail"
                failed["pass"] = False
                failed["reason"] = redact(str(exc), client.secrets)
                entries.append(failed)
            else:
                entries.append(_failed_gate(name, redact(str(exc), client.secrets)))
            _finish(ledger_path, entries, client)
            _emit(f"Error: {exc}", client.secrets, sys.stderr)
            return 1
        entries.append(entry)
        document = _finish(ledger_path, entries, client)
        if entry["result"] == "fail":
            return 1 if document["result"] == "fail" else 0
    document = _finish(ledger_path, entries, client)
    return 0 if document["result"] != "fail" else 1


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def split_gate(argv):
    """Pull a gate subcommand out of argv. Flags keep working in any position."""
    gate = None
    rest = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in VALUE_OPTIONS:
            rest.append(arg)
            if index + 1 < len(argv):
                rest.append(argv[index + 1])
                index += 2
                continue
            index += 1
            continue
        if gate is None and arg in GATES:
            gate = arg
            index += 1
            continue
        rest.append(arg)
        index += 1
    return gate or "fleet", rest


def main(argv=None, *, env=None, sleep=time.sleep, clock=time.monotonic,
         poll_seconds=POLL_SECONDS, deadline_seconds=DEADLINE_SECONDS):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", required=True, help="Hub base URL")
    parser.add_argument("--consecutive", type=_positive_int, default=2,
                        help="Fleet gate: passing rooms in a row required for exit 0 (default 2)")
    parser.add_argument("--max-rooms", type=_positive_int, default=8,
                        help="Fleet gate: stop after this many rooms (default 8)")
    parser.add_argument("--ledger", required=True, help="Path of the JSON ledger to write")
    parser.add_argument("--room-timeout", type=_positive_int, default=DEFAULT_ROOM_TIMEOUT,
                        help="timeout_seconds for each room (default 180; the hub refuses "
                             "less than 120 with codex on the room). Load rooms must finish "
                             "within this many seconds.")
    parser.add_argument("--load-rooms", type=_positive_int, default=3,
                        help="Load gate: rooms created together (default 3, allowed 1-5)")
    source_argv = list(sys.argv[1:] if argv is None else argv)
    gate, parsed_argv = split_gate(source_argv)
    args = parser.parse_args(parsed_argv)
    source = os.environ if env is None else env
    try:
        if poll_seconds <= 0 or deadline_seconds <= 0:
            raise ValueError("poll interval and deadline must be positive")
        if gate in ("fleet", "all") and args.consecutive > args.max_rooms:
            raise ValueError("consecutive must be less than or equal to max-rooms")
        if gate in ("load", "all") and not 1 <= args.load_rooms <= 5:
            raise ValueError("load-rooms must be from 1 to 5")
        if not 120 <= args.room_timeout <= 900:
            raise ValueError("room-timeout must be from 120 to 900 seconds")
        manager = token_from_env(source, "HUB_MANAGER_TOKEN")
        identity = token_from_env(source, "HUB_ID_TOKEN")
        client = HubClient(args.hub, manager, identity)
        client.room_timeout = args.room_timeout
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    gates = ALL_GATES if gate == "all" else (gate,)
    try:
        return run_gates(
            client, gates, consecutive=args.consecutive, max_rooms=args.max_rooms,
            load_rooms=args.load_rooms, room_timeout=args.room_timeout, ledger_path=args.ledger,
            poll_seconds=poll_seconds, deadline_seconds=deadline_seconds, sleep=sleep, clock=clock,
        )
    except HubError as exc:
        print(f"Error: {redact(str(exc), client.secrets)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
