"""Pipeline helpers with duplicated merge logic."""


def merge_many(records):
    acc = {}
    for rec in records:
        # Bug: mutates acc nested aliases; lists overwrite instead of concat
        for k, v in rec.items():
            if k in acc and isinstance(acc[k], dict) and isinstance(v, dict):
                acc[k].update(v)
            else:
                acc[k] = v
    return acc


def apply_overrides(base, overrides):
    # Different buggy variant: mutates base
    for k, v in overrides.items():
        if k in base and isinstance(base[k], list) and isinstance(v, list):
            base[k].extend(v)
        elif k in base and isinstance(base[k], dict) and isinstance(v, dict):
            apply_overrides(base[k], v)
        else:
            base[k] = v
    return base
