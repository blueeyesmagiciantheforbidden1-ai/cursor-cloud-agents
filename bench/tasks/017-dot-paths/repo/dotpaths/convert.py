"""Flatten / expand — incomplete."""


def flatten(obj):
    # Bug: mutates via shared; skips empty dicts; wrong on non-dict leaves ok-ish
    out = {}

    def walk(o, prefix):
        for k, v in o.items():
            key = f'{prefix}.{k}' if prefix else k
            if isinstance(v, dict) and v:
                walk(v, key)
            elif isinstance(v, dict):
                pass  # drops empty
            else:
                out[key] = v
                o[k] = v  # touch
    walk(obj, '')
    return out


def expand(flat):
    # Bug: no conflict detection; mutates? 
    root = {}
    for key, value in flat.items():
        parts = key.split('.')
        cur = root
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
    return root
