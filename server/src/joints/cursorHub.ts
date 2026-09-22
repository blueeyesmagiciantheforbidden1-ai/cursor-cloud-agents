import { HubError } from "./errors.js";

export { HubError };

export const LEASE_MS = 45_000;

export interface CursorClaim {
  roomId: string;
  lease: string;
}

interface HubRoom {
  id: string;
  prompt: string;
  nextAgent: string;
  status: "queued" | "claimed" | "completed" | "stalled" | "cancelled";
  expiresAt: number;
}

export class CursorHub {
  private rooms: HubRoom[];
  private now: () => number;

  constructor(
    rooms: Array<{ id: string; prompt: string; nextAgent: string }>,
    options?: { now?: () => number },
  ) {
    this.now = options?.now ?? (() => Date.now());
    this.rooms = rooms.map((room) => ({
      id: room.id,
      prompt: room.prompt,
      nextAgent: room.nextAgent,
      status: "queued",
      expiresAt: 0,
    }));
  }

  async claim(agent: string): Promise<CursorClaim | null> {
    const room = this.rooms.find((item) => item.status === "queued" && item.nextAgent === agent);
    if (!room) return null;
    room.status = "claimed";
    room.expiresAt = this.now() + LEASE_MS;
    return { roomId: room.id, lease: room.id };
  }

  async heartbeat(lease: string): Promise<{ ok: true; expiresAt: number }> {
    const room = this.requireLease(lease);
    this.stallIfExpired(room);
    if (room.status !== "claimed") throw new HubError("lease is not active", 409);
    room.expiresAt = this.now() + LEASE_MS;
    return { ok: true, expiresAt: room.expiresAt };
  }

  async complete(lease: string): Promise<{ ok: true; replayed: boolean }> {
    const room = this.requireLease(lease);
    if (room.status === "completed") return { ok: true, replayed: true };
    this.stallIfExpired(room);
    if (room.status !== "claimed") throw new HubError("lease is not active", 409);
    room.status = "completed";
    return { ok: true, replayed: false };
  }

  async cancel(lease: string): Promise<{ ok: true }> {
    const room = this.requireLease(lease);
    this.stallIfExpired(room);
    if (room.status !== "claimed") throw new HubError("lease is not active", 409);
    room.status = "cancelled";
    return { ok: true };
  }

  get(roomId: string): { id: string; status: HubRoom["status"] } {
    const room = this.rooms.find((item) => item.id === roomId);
    if (!room) throw new HubError("room not found", 404);
    return { id: room.id, status: room.status };
  }

  private requireLease(lease: string): HubRoom {
    const room = this.rooms.find((item) => item.id === lease);
    if (!room) throw new HubError("lease not found", 404);
    return room;
  }

  private stallIfExpired(room: HubRoom): void {
    if (room.status === "claimed" && this.now() > room.expiresAt) {
      room.status = "stalled";
    }
  }
}
