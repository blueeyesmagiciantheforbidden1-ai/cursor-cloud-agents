# Event journal

Build a tiny append-only JSONL event journal across a few modules.

## `eventjournal.codec`

- `encode_event(seq: int, payload: dict) -> str` — one JSON object with keys
  `seq` and `payload`, then a newline. Compact JSON separators `(',', ':')`,
  `sort_keys=True` on the top-level object.
- `decode_event(line: str) -> tuple[int, dict]` — parse one line (tolerate a
  trailing newline). Raise `ValueError` if `seq`/`payload` missing or wrong types.

## `eventjournal.store.EventStore`

`EventStore(path)` wraps a file path.

- `append(payload: dict) -> int` — assign the next sequence number (starting at 1
  for a new file; continue from the last seq already on disk), append one encoded
  line, return the seq. Create the file if needed.
- `read_from(start_seq: int = 1) -> list[tuple[int, dict]]` — return all events with
  `seq >= start_seq` in order. Missing file means `[]`.
- Do not mutate the caller's `payload` dict.

## `eventjournal.cursor`

- `save_cursor(path, seq: int) -> None` — write the integer seq as decimal text
  plus a newline.
- `load_cursor(path, default: int = 1) -> int` — read it back; if the file is
  missing return `default`. Raise `ValueError` on malformed content.

`eventjournal` should re-export `EventStore`, `encode_event`, `decode_event`,
`save_cursor`, `load_cursor`.
