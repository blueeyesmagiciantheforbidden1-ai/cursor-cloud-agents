import copy


def deep_merge(a, b):
    out = copy.deepcopy(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        elif k in out and isinstance(out[k], list) and isinstance(v, list):
            out[k] = list(out[k]) + list(v)
        else:
            out[k] = copy.deepcopy(v)
    return out
