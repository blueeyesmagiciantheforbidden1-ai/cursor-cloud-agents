"""Deep merge for nested dicts."""


def deep_merge(base, override):
    """Return a new dict merging override onto base recursively.

    Does not mutate base or override. Nested dicts are merged; other
    values are replaced by override.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
