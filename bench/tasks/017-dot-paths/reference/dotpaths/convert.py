"""Flatten / expand nested dicts."""


def flatten(obj):
    out = {}

    def walk(o, prefix):
        for k, v in o.items():
            key = f'{prefix}.{k}' if prefix else k
            if isinstance(v, dict):
                if v:
                    walk(v, key)
                else:
                    out[key] = {}
            else:
                out[key] = v
    walk(obj, '')
    return out


def expand(flat):
    root = {}
    for key, value in flat.items():
        parts = key.split('.')
        cur = root
        for i, p in enumerate(parts[:-1]):
            if p in cur and not isinstance(cur[p], dict):
                raise ValueError('conflict')
            cur = cur.setdefault(p, {})
        last = parts[-1]
        if last in cur and isinstance(cur[last], dict) and not isinstance(value, dict):
            raise ValueError('conflict')
        if last in cur and not isinstance(cur[last], dict) and isinstance(value, dict):
            raise ValueError('conflict')
        if last in cur and isinstance(cur[last], dict) and isinstance(value, dict) and cur[last] and value:
            pass
        cur[last] = value
    return root
