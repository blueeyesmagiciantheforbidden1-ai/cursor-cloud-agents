import csv
import io


def read_rows(text):
    # BUG: treats first data row as header sometimes; no empty handling
    f = io.StringIO(text)
    reader = csv.reader(f)
    rows = list(reader)
    if not rows:
        return []
    header = rows[0]
    result = []
    for row in rows:  # BUG: includes header as data
        result.append(dict(zip(header, row)))
    return result
