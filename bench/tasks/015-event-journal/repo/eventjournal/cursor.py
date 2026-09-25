def save_cursor(path, seq):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(str(seq))  # missing newline


def load_cursor(path, default=1):
    try:
        with open(path, encoding='utf-8') as f:
            return int(f.read().strip())
    except FileNotFoundError:
        return default
