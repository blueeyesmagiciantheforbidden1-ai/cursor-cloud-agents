def join_rows(left, right, key, how='inner'):
    # BUG: only supports naive merge by index; mutates; ignores how
    out = []
    for i, lrow in enumerate(left):
        rrow = right[i] if i < len(right) else {}
        merged = {}
        merged.update(lrow)
        merged.update(rrow)
        out.append(merged)
        lrow['_joined'] = True  # mutation bug
    return out
