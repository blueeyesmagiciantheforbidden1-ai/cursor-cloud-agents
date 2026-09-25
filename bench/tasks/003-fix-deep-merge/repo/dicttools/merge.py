"""Deep merge for nested dicts."""


def deep_merge(base, override):
    """Merge override onto base. Buggy: mutates base and replaces nested dicts."""
    result = base
    for key, value in override.items():
        result[key] = value
    return result
