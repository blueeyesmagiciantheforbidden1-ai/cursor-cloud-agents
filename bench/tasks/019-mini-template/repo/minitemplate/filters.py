def upper(v):
    return str(v).upper()


def lower(v):
    return str(v).lower()


def get_filter(name):
    # Incomplete registry
    table = {'upper': upper, 'lower': lower}
    return table[name]  # KeyError instead of ValueError
