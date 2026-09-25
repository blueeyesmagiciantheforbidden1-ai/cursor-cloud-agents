"""Fixed-width records — buggy starter."""


def _total(schema):
    return sum(w for _, w in schema)


def parse_line(line, schema):
    # Bugs: no newline strip; too-short returns partial; no strip on fields
    out = {}
    i = 0
    for name, width in schema:
        out[name] = line[i:i + width]
        i += width
    return out


def format_line(values, schema):
    # Bugs: left-pad; no truncate
    parts = []
    for name, width in schema:
        s = str(values.get(name, ''))
        parts.append(s.rjust(width))
    return ''.join(parts)


def parse_lines(text, schema):
    return [parse_line(L, schema) for L in text.splitlines()]
