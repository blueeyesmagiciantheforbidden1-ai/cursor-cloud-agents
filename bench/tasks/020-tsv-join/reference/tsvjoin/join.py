import copy


def join_tables(left_headers, left_rows, right_headers, right_rows, key, how='inner'):
    if how not in ('inner', 'left'):
        raise ValueError(f'unsupported how: {how}')
    if key not in left_headers:
        raise KeyError(key)
    right_out_headers = []
    rename = {}
    for h in right_headers:
        if h == key:
            continue
        name = h
        if h in left_headers:
            name = f'{h}_right'
        rename[h] = name
        right_out_headers.append(name)
    headers = list(left_headers) + right_out_headers
    index = {}
    for r in right_rows:
        index.setdefault(r.get(key, ''), []).append(r)
    out = []
    for L in left_rows:
        matches = index.get(L.get(key, ''), [])
        if not matches:
            if how == 'left':
                row = {h: L.get(h, '') for h in left_headers}
                for name in right_out_headers:
                    row[name] = ''
                out.append(row)
            continue
        for R in matches:
            row = {h: L.get(h, '') for h in left_headers}
            for h, name in rename.items():
                row[name] = R.get(h, '')
            out.append(row)
    return headers, out
