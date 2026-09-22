import { test } from "node:test";
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { FirestoreClaimStore, SqliteClaimStore, type Room, type RoomStatus } from "./claimStore.js";
import { AdapterError, boundedText, buildCursorCommand } from "./cursorAdapter.js";
import { CursorHub, HubError, LEASE_MS } from "./cursorHub.js";
import { selectStack } from "./modelStack.js";
import { reportObservation } from "./observations.js";

/**
 * Cursor joint gauntlet.
 *
 * Contract from the hub review and the joint stress run:
 * SQLite list(agent) is status=queued AND next_agent, oldest 50.
 * Firestore must match that predicate. Cursor needs an explicit absolute
 * executable, refuses project_work, refuses a shell, and keeps UTF-8
 * codepoints intact. Claims are single-winner. Observations drop command
 * channels. The model stack is five stable ids and never API-billed.
 */

const AGENTS = ["cursor", "codex", "claude", "copilot", "grok", "cursor\u0301", "Cursor"] as const;
const STATUSES: RoomStatus[] = ["queued", "claimed", "completed", "stalled", "cancelled"];
const EXE = "/opt/cursor/cursor-agent";

function mulberry32(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function iso(seconds: number): string {
  return new Date(Date.UTC(2020, 0, 1, 0, 0, seconds)).toISOString();
}

function expectedIds(rooms: Room[], agent?: string): string[] {
  let rows = rooms.slice();
  if (agent !== undefined) {
    rows = rows.filter((room) => room.status === "queued" && room.nextAgent === agent);
  }
  rows.sort((a, b) => a.createdAt.localeCompare(b.createdAt) || a.id.localeCompare(b.id));
  if (agent !== undefined) rows = rows.slice(0, 50);
  return rows.map((room) => room.id);
}

function loadBoth(rooms: Room[]): { sqlite: SqliteClaimStore; firestore: FirestoreClaimStore } {
  const sqlite = new SqliteClaimStore();
  const firestore = new FirestoreClaimStore();
  for (const room of rooms) {
    sqlite.insert(room);
    firestore.insert(room);
  }
  return { sqlite, firestore };
}

test("lease constant is the 45s hub lease", () => {
  assert.equal(LEASE_MS, 45_000);
});

test("list(agent) parity holds for adversarial queues across 10 trials", () => {
  for (let trial = 0; trial < 10; trial += 1) {
    const rand = mulberry32(0x5eed + trial * 997);
    const rooms: Room[] = [];
    for (let i = 0; i < 80; i += 1) {
      const status = STATUSES[Math.floor(rand() * STATUSES.length)] ?? "queued";
      const nextAgent = rand() < 0.08 ? null : (AGENTS[Math.floor(rand() * AGENTS.length)] ?? "cursor");
      rooms.push({
        id: `t${trial}-${String(i).padStart(3, "0")}`,
        status,
        nextAgent,
        createdAt: iso(Math.floor(rand() * 240)),
        prompt: `p-${trial}-${i}`,
      });
    }
    rooms.push(
      {
        id: `t${trial}-tie-b`,
        status: "queued",
        nextAgent: "cursor",
        createdAt: "2020-06-01T00:00:00.000Z",
        prompt: "tie",
      },
      {
        id: `t${trial}-tie-a`,
        status: "queued",
        nextAgent: "cursor",
        createdAt: "2020-06-01T00:00:00.000Z",
        prompt: "tie",
      },
      {
        id: `t${trial}-space`,
        status: "queued",
        nextAgent: "cursor ",
        createdAt: "2019-01-01T00:00:00.000Z",
        prompt: "space",
      },
    );
    const { sqlite, firestore } = loadBoth([...rooms].reverse());
    for (const agent of [undefined, "cursor", "cursor\u0301", "Cursor", "cursor ", ""] as const) {
      const expect = expectedIds(rooms, agent);
      const fromSqlite = sqlite.list(agent).map((room) => room.id);
      const fromFirestore = firestore.list(agent).map((room) => room.id);
      assert.deepEqual(fromSqlite, expect, `sqlite trial ${trial} agent ${JSON.stringify(agent)}`);
      assert.deepEqual(fromFirestore, expect, `firestore trial ${trial} agent ${JSON.stringify(agent)}`);
      if (agent !== undefined) {
        for (const room of firestore.list(agent)) {
          assert.equal(room.status, "queued");
          assert.equal(room.nextAgent, agent);
        }
      }
    }
  }
});

test("oldest queued cursor rooms survive a crowd of older claimed rows", () => {
  const rooms: Room[] = [];
  for (let i = 0; i < 50; i += 1) {
    rooms.push({
      id: `claimed-${String(i).padStart(2, "0")}`,
      status: "claimed",
      nextAgent: "cursor",
      createdAt: iso(i),
      prompt: "old claim",
    });
  }
  for (let i = 0; i < 60; i += 1) {
    rooms.push({
      id: `queued-${String(i).padStart(2, "0")}`,
      status: "queued",
      nextAgent: "cursor",
      createdAt: iso(1000 + i),
      prompt: "queued",
    });
  }
  const { sqlite, firestore } = loadBoth(rooms);
  const expect = expectedIds(rooms, "cursor");
  assert.equal(expect.length, 50);
  assert.equal(expect[0], "queued-00");
  assert.equal(expect[49], "queued-49");
  assert.deepEqual(sqlite.list("cursor").map((room) => room.id), expect);
  assert.deepEqual(firestore.list("cursor").map((room) => room.id), expect);
});

test("stores return copies and ignore later mutation of the inserted room", () => {
  for (const store of [new SqliteClaimStore(), new FirestoreClaimStore()]) {
    const room: Room = {
      id: "copy-1",
      status: "queued",
      nextAgent: "cursor",
      createdAt: iso(1),
      prompt: "original",
    };
    store.insert(room);
    room.status = "claimed";
    room.prompt = "mutated";
    const listed = store.list("cursor");
    assert.equal(listed.length, 1);
    assert.equal(listed[0]?.status, "queued");
    assert.equal(listed[0]?.prompt, "original");
    if (listed[0]) listed[0].status = "completed";
    assert.equal(store.list("cursor")[0]?.status, "queued");
  }
});

test("model stack is the five canonical ids, stable, and not API-billed", () => {
  const expected = ["codex", "claude", "cursor", "copilot", "grok"];
  const first = selectStack();
  assert.deepEqual(first.ids, expected);
  assert.equal(new Set(first.ids).size, 5);
  assert.equal(first.apiBillingAllowed, false);
  for (let i = 0; i < 10; i += 1) {
    assert.deepEqual(selectStack(), first);
  }
});

test("boundedText keeps codepoint boundaries for every cut of a mixed string", () => {
  const text = "Aé你😀e\u0301\u{1F468}\u200D\u{1F469}\u200D\u{1F467}";
  const total = Buffer.byteLength(text, "utf8");
  for (let maxBytes = 0; maxBytes <= total + 1; maxBytes += 1) {
    const cut = boundedText(text, maxBytes);
    assert.equal(cut.includes("\uFFFD"), false);
    assert.ok(isCodeUnitPrefix(text, cut), "cut must be a code-unit prefix");
    assert.ok(Buffer.byteLength(cut, "utf8") <= maxBytes, "cut exceeds the byte budget");
    if (cut.length < text.length) {
      const next = nextCodepoint(text, cut.length);
      assert.ok(Buffer.byteLength(cut + next, "utf8") > maxBytes, "cut is shorter than the longest legal prefix");
    } else {
      assert.equal(cut, text);
    }
  }
  assert.equal(boundedText("😀😀", 4), "😀");
  assert.equal(boundedText("😀😀", 5), "😀");
  assert.equal(boundedText("a😀", 1), "a");
  assert.equal(boundedText("a😀", 2), "a");
  assert.equal(boundedText("é", 1), "");
  assert.equal(boundedText("é", 2), "é");
  assert.throws(() => boundedText("ok\uD800", 10), (err: unknown) => err instanceof AdapterError && /surrogate/i.test(err.message));
  assert.throws(() => boundedText("\uDFFF", 10), (err: unknown) => err instanceof AdapterError && /surrogate/i.test(err.message));
  assert.throws(() => boundedText("ok", -1), (err: unknown) => err instanceof AdapterError);
});

test("boundedText property holds across 10 random well-formed strings", () => {
  for (let trial = 0; trial < 10; trial += 1) {
    const rand = mulberry32(0x0b0e + trial);
    const alphabet = ["a", "é", "你", "😀", "e\u0301", "\u{1F9D1}\u200D\u{1F33E}"];
    let text = "";
    const pieces = 1 + Math.floor(rand() * 40);
    for (let i = 0; i < pieces; i += 1) {
      text += alphabet[Math.floor(rand() * alphabet.length)] ?? "a";
    }
    const total = Buffer.byteLength(text, "utf8");
    const cuts = new Set([0, 1, 2, 3, 4, total, total + 3, Math.floor(rand() * (total + 1))]);
    for (const maxBytes of cuts) {
      const cut = boundedText(text, maxBytes);
      assert.ok(isCodeUnitPrefix(text, cut), `trial ${trial} bytes ${maxBytes}`);
      assert.ok(Buffer.byteLength(cut, "utf8") <= maxBytes, `trial ${trial} cut exceeds ${maxBytes}`);
      assert.equal(cut.includes("\uFFFD"), false);
      if (cut !== text) {
        const next = nextCodepoint(text, cut.length);
        assert.ok(
          Buffer.byteLength(cut + next, "utf8") > maxBytes,
          `trial ${trial} cut is shorter than the longest legal prefix`,
        );
      }
    }
  }
});

test("cursor command rejects a missing, generic, relative, or profile path", () => {
  const prompt = "review the hub";
  const bad: Array<string | undefined> = [
    undefined,
    "",
    "   ",
    "agent",
    "AGENT",
    "agent.cmd",
    "agent.exe",
    "./agent",
    "cursor-agent",
    "C:\\Users\\9\\AppData\\cursor\\agent.exe",
    "C:/users/9/cursor-agent.exe",
    "/opt/Users/9/cursor-agent",
  ];
  for (const executable of bad) {
    assert.throws(
      () => buildCursorCommand({ prompt, executable, executionMode: "review" }),
      (err: unknown) => err instanceof AdapterError,
      `accepted ${JSON.stringify(executable)}`,
    );
  }
  assert.throws(
    () => buildCursorCommand({ prompt, executable: undefined, executionMode: "review" }),
    (err: unknown) => err instanceof AdapterError && /explicit local executable/i.test(err.message),
  );
  assert.throws(
    () => buildCursorCommand({ prompt, executable: "C:\\Users\\9\\cursor\\agent.exe", executionMode: "ask" }),
    (err: unknown) => err instanceof AdapterError && /Users\\9/i.test(err.message),
  );
});

test("cursor command keeps one argv, refuses project work, and caps UTF-8 at 8000 bytes", () => {
  const prompt = "say $(whoami) && echo hi; cat `secret`";
  const argv = buildCursorCommand({ prompt, executable: EXE, executionMode: "review" });
  assert.equal(argv[0], EXE);
  assert.equal(argv.filter((part) => part === prompt).length, 1);
  assert.equal(argv.includes("sh"), false);
  assert.equal(argv.includes("-c"), false);
  assert.equal(argv.includes("cmd.exe"), false);
  assert.equal(argv.includes("powershell"), false);
  assert.equal(argv.some((part) => part === "&&"), false);
  assert.equal(argv.some((part) => part === ";"), false);

  const ask = buildCursorCommand({
    prompt: "status",
    executable: "C:\\tools\\cursor-agent.exe",
    executionMode: "ask",
  });
  assert.equal(ask[0], "C:\\tools\\cursor-agent.exe");
  assert.equal(ask.includes("status"), true);

  assert.throws(
    () => buildCursorCommand({ prompt: "ship it", executable: EXE, executionMode: "project_work" }),
    (err: unknown) =>
      err instanceof AdapterError &&
      err.message === "Project work is currently supported only by the verified Claude worker",
  );
  assert.throws(
    () => buildCursorCommand({ prompt: "ship it", executable: EXE, executionMode: "exec" }),
    (err: unknown) => err instanceof AdapterError && /unsupported execution mode/i.test(err.message),
  );
  assert.throws(
    () => buildCursorCommand({ prompt: "   ", executable: EXE, executionMode: "review" }),
    (err: unknown) => err instanceof AdapterError && /prompt is required/i.test(err.message),
  );
  assert.throws(
    () => buildCursorCommand({ prompt: "bad\u0000byte", executable: EXE, executionMode: "review" }),
    (err: unknown) => err instanceof AdapterError && /NUL/i.test(err.message),
  );
  assert.throws(
    () => buildCursorCommand({ prompt: "ok\uD800tail", executable: EXE, executionMode: "review" }),
    (err: unknown) => err instanceof AdapterError && /surrogate/i.test(err.message),
  );

  const exact = "😀".repeat(2000);
  assert.equal(Buffer.byteLength(exact, "utf8"), 8000);
  const exactArgv = buildCursorCommand({ prompt: exact, executable: "/usr/bin/agent", executionMode: "review" });
  assert.equal(exactArgv.includes(exact), true);

  const over = `${"é".repeat(3999)}xxx`;
  assert.equal(Buffer.byteLength(over, "utf8"), 8001);
  assert.throws(
    () => buildCursorCommand({ prompt: over, executable: EXE, executionMode: "review" }),
    (err: unknown) => err instanceof AdapterError && /8000/.test(err.message),
  );
});

test("32 parallel cursor claims have one winner, and 32 rooms stay unique, 10 trials", async () => {
  for (let trial = 0; trial < 10; trial += 1) {
    const single = new CursorHub([{ id: `only-${trial}`, prompt: "review", nextAgent: "cursor" }]);
    const raced = await Promise.all(Array.from({ length: 32 }, () => single.claim("cursor")));
    const winners = raced.filter((claim): claim is NonNullable<typeof claim> => claim !== null);
    assert.equal(winners.length, 1, `trial ${trial} winners`);
    assert.equal(single.get(winners[0].roomId).status, "claimed");
    assert.equal(await single.claim("cursor"), null);

    const many = new CursorHub(
      Array.from({ length: 32 }, (_, index) => ({
        id: `room-${trial}-${index}`,
        prompt: "review",
        nextAgent: "cursor",
      })),
    );
    const all = await Promise.all(Array.from({ length: 32 }, () => many.claim("cursor")));
    const ids = all.map((claim) => {
      assert.ok(claim, "each queued cursor room is claimed once");
      return claim.roomId;
    });
    assert.equal(new Set(ids).size, 32);
    assert.equal(await many.claim("cursor"), null);
  }
});

test("cursor does not take another agent's queued room", async () => {
  const hub = new CursorHub([{ id: "codex-room", prompt: "x", nextAgent: "codex" }]);
  assert.equal(await hub.claim("cursor"), null);
  const claim = await hub.claim("codex");
  assert.ok(claim, "codex claim");
  assert.equal(claim.roomId, "codex-room");
  assert.equal(hub.get("codex-room").status, "claimed");
});

test("complete is idempotent after expiry, cancel loses, and the boundary is exact", async () => {
  const starts = [0, 1, 999, 45_000, 1_700_000_000_000];
  for (const start of starts) {
    let now = start;
    const hub = new CursorHub([{ id: "r", prompt: "p", nextAgent: "cursor" }], { now: () => now });
    const claim = await hub.claim("cursor");
    assert.ok(claim, "cursor claim");
    now = start + LEASE_MS;
    const beat = await hub.heartbeat(claim.lease);
    assert.equal(beat.expiresAt, start + 2 * LEASE_MS);
    now = beat.expiresAt;
    const done = await hub.complete(claim.lease);
    assert.deepEqual(done, { ok: true, replayed: false });
    now += 10 * LEASE_MS;
    const again = await hub.complete(claim.lease);
    assert.deepEqual(again, { ok: true, replayed: true });
    assert.equal(hub.get("r").status, "completed");
    await assert.rejects(hub.cancel(claim.lease), (err: unknown) => err instanceof HubError && err.status === 409);
    assert.equal(hub.get("r").status, "completed");
  }

  let expiredNow = 0;
  const expired = new CursorHub([{ id: "late", prompt: "p", nextAgent: "cursor" }], {
    now: () => expiredNow,
  });
  const late = await expired.claim("cursor");
  assert.ok(late, "late claim");
  expiredNow = LEASE_MS + 1;
  await assert.rejects(expired.complete(late.lease), (err: unknown) => err instanceof HubError && err.status === 409);
  assert.equal(expired.get("late").status, "stalled");
  await assert.rejects(
    expired.heartbeat(late.lease),
    (err: unknown) => err instanceof HubError && err.status === 409,
  );

  await assert.rejects(expired.complete("missing"), (err: unknown) => err instanceof HubError && err.status === 404);
  await assert.rejects(expired.cancel("missing"), (err: unknown) => err instanceof HubError && err.status === 404);
});

test("complete and cancel race yields exactly one terminal status", async () => {
  for (let trial = 0; trial < 10; trial += 1) {
    const hub = new CursorHub([{ id: `race-${trial}`, prompt: "p", nextAgent: "cursor" }]);
    const claim = await hub.claim("cursor");
    assert.ok(claim, "race claim");
    const [completed, cancelled] = await Promise.allSettled([hub.complete(claim.lease), hub.cancel(claim.lease)]);
    const fulfilled = [completed, cancelled].filter((result) => result.status === "fulfilled");
    assert.equal(fulfilled.length, 1, `trial ${trial}`);
    const status = hub.get(claim.roomId).status;
    if (completed.status === "fulfilled") {
      assert.equal(status, "completed");
      assert.equal(cancelled.status, "rejected");
    } else {
      assert.equal(status, "cancelled");
      assert.equal(completed.status, "rejected");
    }
  }
});

test("cursor observations drop command channels and keep the cursor inventory", () => {
  const input = {
    inventory: [
      {
        agent_id: "cursor",
        label: "Cursor",
        command: "SENTINEL_COMMAND_9f3a",
        Command: "SENTINEL_COMMAND_CASE_9f3a",
      },
    ],
    activity: [
      {
        id: "cursor-sdk",
        agent_id: "cursor",
        argv: ["SENTINEL_ARGV_9f3a"],
        note: "desk check",
        print: "SENTINEL_PRINT_9f3a",
      },
    ],
    nested: { shell: "SENTINEL_SHELL_9f3a", keep: 1, cmd: "SENTINEL_CMD_9f3a" },
    list: [{ print: "SENTINEL_LIST_9f3a" }, { script: "SENTINEL_SCRIPT_9f3a", stdout: "SENTINEL_STDOUT_9f3a" }],
    stderr: "SENTINEL_STDERR_9f3a",
  };
  const stored = reportObservation(input);
  const json = JSON.stringify(stored);
  for (const sentinel of [
    "SENTINEL_COMMAND_9f3a",
    "SENTINEL_COMMAND_CASE_9f3a",
    "SENTINEL_ARGV_9f3a",
    "SENTINEL_PRINT_9f3a",
    "SENTINEL_SHELL_9f3a",
    "SENTINEL_CMD_9f3a",
    "SENTINEL_LIST_9f3a",
    "SENTINEL_SCRIPT_9f3a",
    "SENTINEL_STDOUT_9f3a",
    "SENTINEL_STDERR_9f3a",
  ]) {
    assert.equal(json.includes(sentinel), false, sentinel);
  }
  assert.deepEqual(stored, {
    inventory: [{ agent_id: "cursor", label: "Cursor" }],
    activity: [{ id: "cursor-sdk", agent_id: "cursor", note: "desk check" }],
    nested: { keep: 1 },
    list: [{}, {}],
  });
  assert.equal(JSON.stringify(input).includes("SENTINEL_COMMAND_9f3a"), true);
});

function isCodeUnitPrefix(whole: string, prefix: string): boolean {
  if (prefix.length > whole.length) return false;
  for (let i = 0; i < prefix.length; i += 1) {
    if (whole.charCodeAt(i) !== prefix.charCodeAt(i)) return false;
  }
  return true;
}

function nextCodepoint(text: string, index: number): string {
  const code = text.charCodeAt(index);
  if (code >= 0xd800 && code <= 0xdbff) return text.slice(index, index + 2);
  return text.slice(index, index + 1);
}
