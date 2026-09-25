def join_rows(left, right, key, how='inner'):
    if how not in ('inner', 'left'):
        raise ValueError('how must be inner or left')
    if left and key not in left[0]:
        raise ValueError('key missing from left')
    if right and key not in right[0]:
        raise ValueError('key missing from right')

    right_index = {}
    for rrow in right:
        right_index.setdefault(rrow[key], []).append(rrow)

    right_cols = []
    if right:
        for col in right[0]:
            if col != key:
                right_cols.append(col)

    out = []
    for lrow in left:
        matches = right_index.get(lrow[key], [])
        if not matches:
            if how == 'left':
                merged = dict(lrow)
                for col in right_cols:
                    dest = col if col not in lrow else 'right_' + col
                    merged[dest] = None
                out.append(merged)
            continue
        for rrow in matches:
            merged = dict(lrow)
            for col in right_cols:
                dest = col if col not in lrow else 'right_' + col
                merged[dest] = rrow[col]
            out.append(merged)
    return out
