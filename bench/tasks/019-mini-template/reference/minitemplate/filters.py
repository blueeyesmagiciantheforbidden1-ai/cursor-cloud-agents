def upper(v):
    return str(v).upper()


def lower(v):
    return str(v).lower()


def title(v):
    return str(v).title()


def strip(v):
    return str(v).strip()


def length(v):
    return str(len(str(v)))


_FILTERS = {
    'upper': upper,
    'lower': lower,
    'title': title,
    'strip': strip,
    'len': length,
}


def get_filter(name):
    try:
        return _FILTERS[name]
    except KeyError as e:
        raise ValueError(f'unknown filter: {name}') from e
