"""Group rows and sum numeric columns."""


def summarize(rows, group_key, value_keys):
    value_keys = list(value_keys)
    groups = {}
    order = []
    for row in rows:
        if group_key not in row:
            raise KeyError(group_key)
        for k in value_keys:
            if k not in row:
                raise KeyError(k)
            val = row[k]
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise TypeError(f'non-numeric value for {k!r}: {val!r}')
        g = row[group_key]
        if g not in groups:
            groups[g] = {group_key: g}
            for k in value_keys:
                groups[g][k] = 0
            order.append(g)
        for k in value_keys:
            groups[g][k] += row[k]
    return [groups[g] for g in sorted(order, key=lambda x: x)]
