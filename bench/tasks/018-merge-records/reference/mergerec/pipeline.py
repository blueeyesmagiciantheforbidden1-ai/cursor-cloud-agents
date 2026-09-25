from mergerec.merge import deep_merge


def merge_many(records):
    acc = {}
    for rec in records:
        acc = deep_merge(acc, rec)
    return acc


def apply_overrides(base, overrides):
    return deep_merge(base, overrides)
