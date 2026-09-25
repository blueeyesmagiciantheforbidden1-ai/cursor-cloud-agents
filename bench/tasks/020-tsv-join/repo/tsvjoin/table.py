"""TSV helpers — incomplete."""


def load_tsv(text):
    lines = [L for L in text.splitlines() if L.strip() != '']
    headers = lines[0].split('\t')
    rows = []
    for line in lines[1:]:
        parts = line.split('\t')
        rows.append(dict(zip(headers, parts)))  # drops missing cells wrongly if short
    return headers, rows


def dump_tsv(headers, rows):
    out = ['\t'.join(headers)]
    for row in rows:
        out.append('\t'.join(row.get(h, '') for h in headers))
    return '\n'.join(out)  # missing trailing newline
