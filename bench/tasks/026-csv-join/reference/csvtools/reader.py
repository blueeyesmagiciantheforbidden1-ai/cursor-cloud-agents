import csv
import io


def read_rows(text):
    if text is None or not str(text).strip():
        return []
    f = io.StringIO(text)
    reader = csv.DictReader(f)
    if reader.fieldnames is None:
        return []
    return [dict(row) for row in reader]
