"""TSV helpers."""


def load_tsv(text):
    lines = [L for L in text.splitlines() if L != '']
    if not lines:
        return [], []
    headers = lines[0].split('\t')
    rows = []
    for line in lines[1:]:
        parts = line.split('\t')
        row = {}
        for i, h in enumerate(headers):
            row[h] = parts[i] if i < len(parts) else ''
        rows.append(row)
    return headers, rows


def dump_tsv(headers, rows):
    lines = ['\t'.join(headers)]
    for row in rows:
        lines.append('\t'.join(row.get(h, '') for h in headers))
    return '\n'.join(lines) + '\n'
