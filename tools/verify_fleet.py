"""Verify the five-agent RunCrew fleet through the hub HTTP API.

Creates one room at a time (codex, claude, cursor, copilot, grok) and polls
GET /v1/rooms/<id> until the room is completed, failed, or stalled, or until
15 minutes pass. A room passes only when every agent exits 0 and its text is
exactly ``NAME | OK``. Exits 0 only after ``--consecutive`` passing rooms in a
row.

The manager token is read from HUB_MANAGER_TOKEN and sent as X-Hub-Token.
The Cloud Run identity token is read from HUB_ID_TOKEN and sent as
Authorization: Bearer. Token values are never printed.

Standard library only. This tool does not call gcloud, Cloud Run, or Firestore
itself; it only talks to the hub URL given with --hub.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

AGENTS = ("codex", "claude", "cursor", "copilot", "grok")
POLL_SECONDS = 10
DEADLINE_SECONDS = 15 * 60
TERMINAL_STATUSES = frozenset({"completed", "failed", "stalled"})
PROMPT = (
    "Each agent must reply with exactly its uppercase name followed by ' | OK' "
    "and no other text. "
    "codex replies CODEX | OK. "
    "claude replies CLAUDE | OK. "
    "cursor replies CURSOR | OK. "
    "copilot replies COPILOT | OK. "
    "grok replies GROK | OK."
)


class HubError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never forward the manager or identity token.
        return None


def expected_reply(agent):
    return agent.upper() + " | OK"


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


def redact(text, secrets):
    if not isinstance(text, str):
        text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def display_room_id(value):
    if isinstance(value, str) and len(value) == 32 and all(character in "0123456789abcdef" for character in value):
        return value
    return None


def _message_index(room):
    found = {}
    messages = room.get("messages") if isinstance(room, dict) else None
    if not isinstance(messages, list):
        return found
    for message in messages:
        if isinstance(message, dict) and message.get("agent") in AGENTS:
            found[message["agent"]] = message
    return found


def exit_codes(room):
    found = _message_index(room)
    codes = {}
    for agent in AGENTS:
        message = found.get(agent)
        code = message.get("exit_code") if isinstance(message, dict) else None
        codes[agent] = code if type(code) is int else None
    return codes


def five_for_five(room):
    if not isinstance(room, dict) or room.get("status") != "completed":
        return False
    found = _message_index(room)
    for agent in AGENTS:
        message = found.get(agent)
        if message is None or message.get("exit_code") != 0 or type(message.get("exit_code")) is not int:
            return False
        text = message.get("text")
        if not isinstance(text, str) or text.strip() != expected_reply(agent):
            return False
    return True


def _one_line(text):
    return "".join(character if character.isprintable() else " " for character in text).strip()


def _created_at(room):
    value = room.get("created_at") if isinstance(room, dict) else None
    if type(value) in (int, float) and type(value) is not bool:
        return value
    return None


class HubClient:
    def __init__(self, base_url, manager_token, identity_token, timeout=30):
        self.base_url = validate_hub_url(base_url)
        self.manager_token = manager_token
        self.identity_token = identity_token
        self.timeout = timeout
        self.opener = build_opener(_NoRedirect())

    @property
    def secrets(self):
        return (self.manager_token, self.identity_token)

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

    def create_room(self):
        return self.request("POST", "/v1/rooms", {
            "prompt": PROMPT,
            "agents": list(AGENTS),
            "timeout_seconds": 300,
            "workspace": "default",
            "purpose": "project",
        })

    def get_room(self, room_id):
        return self.request("GET", "/v1/rooms/" + room_id)


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


def wait_for_room(client, room_id, *, deadline_seconds, poll_seconds, sleep, clock):
    deadline = clock() + deadline_seconds
    latest = None
    while True:
        try:
            latest = client.get_room(room_id)
        except HubError as exc:
            if exc.status in (401, 403):
                raise
            if clock() >= deadline:
                return latest
        else:
            status = latest.get("status") if isinstance(latest, dict) else None
            if status in TERMINAL_STATUSES or clock() >= deadline:
                return latest
        remaining = deadline - clock()
        if remaining <= 0:
            return latest
        sleep(min(poll_seconds, remaining))


def _status_text(room):
    status = room.get("status") if isinstance(room, dict) else None
    if isinstance(status, str) and status.isascii() and status.isprintable() and len(status) <= 32:
        return status
    return "unknown"


def _print_room(room, created_at, passed, streak, consecutive, secrets):
    room_id = display_room_id(room.get("id") if isinstance(room, dict) else None) or "invalid-room-id"
    result = "pass" if passed else "fail"
    print(f"room {room_id} created_at={created_at} status={_status_text(room)} result={result}", flush=True)
    found = _message_index(room)
    codes = exit_codes(room)
    for agent in AGENTS:
        message = found.get(agent)
        code = codes[agent]
        shown = "missing" if code is None else str(code)
        text = ""
        if isinstance(message, dict) and isinstance(message.get("text"), str):
            text = redact(_one_line(message["text"]), secrets)
        print(f"  {agent} exit_code={shown} text={text}", flush=True)
    print(f"consecutive={streak}/{consecutive}", flush=True)


def _ledger_entry(created, final, passed):
    room_id = display_room_id(created.get("id") if isinstance(created, dict) else None)
    if room_id is None and isinstance(final, dict):
        room_id = display_room_id(final.get("id"))
    return {
        "room_id": room_id,
        "created_at": _created_at(created),
        "status": _status_text(final if isinstance(final, dict) else created),
        "exit_codes": exit_codes(final if isinstance(final, dict) else {}),
        "pass": passed,
    }


def write_ledger(path, payload):
    destination = Path(path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    except OSError:
        raise HubError("Could not write the ledger") from None


def run_verification(client, *, consecutive, max_rooms, ledger_path, poll_seconds, deadline_seconds,
                     sleep, clock):
    rooms = []
    streak = 0
    for _ in range(max_rooms):
        try:
            created = client.create_room()
        except HubError as exc:
            _finish_ledger(ledger_path, consecutive, max_rooms, rooms, streak)
            print(f"Error: {redact(str(exc), client.secrets)}", file=sys.stderr)
            return 1
        room_id = display_room_id(created.get("id") if isinstance(created, dict) else None)
        if room_id is None:
            _finish_ledger(ledger_path, consecutive, max_rooms, rooms, streak)
            print("Error: Hub returned an invalid room id", file=sys.stderr)
            return 1
        print(f"created room {room_id}", flush=True)
        try:
            final = wait_for_room(client, room_id, deadline_seconds=deadline_seconds,
                                  poll_seconds=poll_seconds, sleep=sleep, clock=clock)
        except HubError as exc:
            _finish_ledger(ledger_path, consecutive, max_rooms, rooms, streak)
            print(f"Error: {redact(str(exc), client.secrets)}", file=sys.stderr)
            return 1
        if not isinstance(final, dict):
            final = created
        passed = five_for_five(final)
        streak = streak + 1 if passed else 0
        rooms.append(_ledger_entry(created, final, passed))
        _finish_ledger(ledger_path, consecutive, max_rooms, rooms, streak)
        _print_room(final if final.get("id") else {**final, "id": room_id},
                    _created_at(created), passed, streak, consecutive, client.secrets)
        if streak >= consecutive:
            print(f"verification passed ({streak}/{consecutive} consecutive)", flush=True)
            return 0
    print(f"verification failed ({streak}/{consecutive} consecutive after {len(rooms)} rooms)", flush=True)
    return 1


def _finish_ledger(path, consecutive, max_rooms, rooms, streak):
    write_ledger(path, {
        "consecutive_required": consecutive,
        "max_rooms": max_rooms,
        "streak": streak,
        "result": "pass" if streak >= consecutive and consecutive > 0 else "fail",
        "rooms": rooms,
    })


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main(argv=None, *, env=None, sleep=time.sleep, clock=time.monotonic,
         poll_seconds=POLL_SECONDS, deadline_seconds=DEADLINE_SECONDS):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", required=True, help="Hub base URL")
    parser.add_argument("--consecutive", type=_positive_int, default=3,
                        help="Passing rooms in a row required for exit 0 (default 3)")
    parser.add_argument("--max-rooms", type=_positive_int, default=8,
                        help="Stop after this many rooms (default 8)")
    parser.add_argument("--ledger", required=True, help="Path of the JSON ledger to write")
    args = parser.parse_args(argv)
    source = os.environ if env is None else env
    try:
        if poll_seconds <= 0 or deadline_seconds <= 0:
            raise ValueError("poll interval and deadline must be positive")
        if args.consecutive > args.max_rooms:
            raise ValueError("consecutive must be less than or equal to max-rooms")
        manager = token_from_env(source, "HUB_MANAGER_TOKEN")
        identity = token_from_env(source, "HUB_ID_TOKEN")
        client = HubClient(args.hub, manager, identity)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    try:
        return run_verification(client, consecutive=args.consecutive, max_rooms=args.max_rooms,
                                ledger_path=args.ledger, poll_seconds=poll_seconds,
                                deadline_seconds=deadline_seconds, sleep=sleep, clock=clock)
    except HubError as exc:
        print(f"Error: {redact(str(exc), client.secrets)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
