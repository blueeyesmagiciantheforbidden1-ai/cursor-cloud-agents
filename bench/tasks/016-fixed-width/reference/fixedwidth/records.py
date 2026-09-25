"""Fixed-width records."""


def _total(schema):
    return sum(w for _, w in schema)


def parse_line(line, schema):
    if line.endswith('\r\n'):
        line = line[:-2]
    elif line.endswith('\n') or line.endswith('\r'):
        line = line[:-1]
    total = _total(schema)
    if len(line) < total:
        raise ValueError('line too short')
    out = {}
    i = 0
    for name, width in schema:
        out[name] = line[i:i + width].strip()
        i += width
    return out


def format_line(values, schema):
    parts = []
    for name, width in schema:
        s = str(values.get(name, ''))
        if len(s) > width:
            s = s[:width]
        else:
            s = s.ljust(width)
        parts.append(s)
    return ''.join(parts)


def parse_lines(text, schema):
    out = []
    for L in text.splitlines():
        if L == '':
            continue
        out.append(parse_line(L, schema))
    return out
