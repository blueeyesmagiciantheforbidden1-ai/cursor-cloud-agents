"""Join — buggy starter."""


def join_tables(left_headers, left_rows, right_headers, right_rows, key, how='inner'):
    # Bugs: only inner; mutates; no rename; requires key on right only
    index = {}
    for r in right_rows:
        index.setdefault(r[key], []).append(r)
    headers = list(left_headers) + [h for h in right_headers if h != key]
    out = []
    for L in left_rows:
        matches = index.get(L[key], [])
        if not matches:
            continue
        for R in matches:
            row = dict(L)
            row.update(R)
            L['seen'] = True
            out.append(row)
    return headers, out
