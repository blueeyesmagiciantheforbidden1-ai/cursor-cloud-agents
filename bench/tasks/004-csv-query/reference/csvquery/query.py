"""Filter and project CSV text."""

import csv
import io


def select(csv_text, columns, where=None):
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise KeyError('missing header')
    header = list(reader.fieldnames)
    for col in columns:
        if col not in header:
            raise KeyError(col)
    if where is not None:
        wcol, wval = where
        if wcol not in header:
            raise KeyError(wcol)

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, lineterminator='\n')
    writer.writeheader()
    for row in reader:
        if where is not None:
            wcol, wval = where
            if row[wcol] != wval:
                continue
        writer.writerow({c: row[c] for c in columns})
    return out.getvalue()
