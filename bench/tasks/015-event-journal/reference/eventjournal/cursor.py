from pathlib import Path


def save_cursor(path, seq):
    Path(path).write_text(f'{seq}\n', encoding='utf-8')


def load_cursor(path, default=1):
    p = Path(path)
    if not p.exists():
        return default
    text = p.read_text(encoding='utf-8').strip()
    try:
        return int(text)
    except ValueError as e:
        raise ValueError('malformed cursor') from e
