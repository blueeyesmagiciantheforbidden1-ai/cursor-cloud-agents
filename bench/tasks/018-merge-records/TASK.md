# Deduplicate record merging

`mergerec` has two entry points that copy-pasted slightly different deep-merge
logic. Please centralize merging and make behaviour consistent.

## Target API

- `mergerec.merge.deep_merge(a, b) -> dict` — deep-merge two dicts into a **new**
  dict. Do not mutate `a` or `b`.
  - Nested dicts merge recursively.
  - If both values are lists, concatenate (`a_list + b_list`).
  - Otherwise values from `b` overwrite values from `a`.
  - Keys only in `a` or only in `b` are included.

- `mergerec.pipeline.merge_many(records: list[dict]) -> dict` — fold `deep_merge`
  left-to-right starting from `{}`. Empty list yields `{}`.

- `mergerec.pipeline.apply_overrides(base: dict, overrides: dict) -> dict` —
  same as `deep_merge(base, overrides)` (kept as a named helper for callers).

`mergerec` re-exports `deep_merge`, `merge_many`, `apply_overrides`.

The starter duplicates buggy merge code in `pipeline.py`; please fix via a single
shared `deep_merge`.
