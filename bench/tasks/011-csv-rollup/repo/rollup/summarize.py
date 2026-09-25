"""Group rows and sum numeric columns. Incomplete starter."""


def summarize(rows, group_key, value_keys):
    # Bugs: mutates input, skips missing keys, no sort, no type checks.
    groups = {}
    for row in rows:
        if group_key not in row:
            continue
        g = row[group_key]
        if g not in groups:
            groups[g] = {group_key: g}
            for k in value_keys:
                groups[g][k] = 0
        for k in value_keys:
            if k in row:
                groups[g][k] += row[k]
                row[k] = groups[g][k]
    return list(groups.values())
