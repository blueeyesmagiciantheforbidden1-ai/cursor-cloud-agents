export type RoomStatus = "queued" | "claimed" | "completed" | "stalled" | "cancelled";

export interface Room {
  id: string;
  status: RoomStatus;
  nextAgent: string | null;
  createdAt: string;
  prompt: string;
}

export interface ClaimStore {
  insert(room: Room): void;
  list(agent?: string): Room[];
}

function copyRoom(room: Room): Room {
  return { ...room };
}

function byAge(a: Room, b: Room): number {
  return a.createdAt.localeCompare(b.createdAt) || a.id.localeCompare(b.id);
}

/**
 * list(agent) keeps status = queued and an exact nextAgent match, oldest 50.
 * list() with no argument returns every room, same sort, with no status filter and no limit.
 */
function listRooms(rooms: readonly Room[], agent?: string): Room[] {
  let rows = rooms.map(copyRoom);
  if (agent !== undefined) {
    rows = rows.filter((room) => room.status === "queued" && room.nextAgent === agent);
  }
  rows.sort(byAge);
  if (agent !== undefined) rows = rows.slice(0, 50);
  return rows;
}

export class SqliteClaimStore implements ClaimStore {
  private rooms: Room[] = [];

  insert(room: Room): void {
    this.rooms.push(copyRoom(room));
  }

  list(agent?: string): Room[] {
    return listRooms(this.rooms, agent);
  }
}

export class FirestoreClaimStore implements ClaimStore {
  private rooms: Room[] = [];

  insert(room: Room): void {
    this.rooms.push(copyRoom(room));
  }

  list(agent?: string): Room[] {
    return listRooms(this.rooms, agent);
  }
}
