"""Minimal INI parse/dump."""


def parse(text):
    data = {}
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].strip()
            data.setdefault(section, {})
            continue
        if '=' not in line:
            raise ValueError(f'invalid line: {raw!r}')
        if section is None:
            raise ValueError('key before section')
        key, _, value = line.partition('=')
        data[section][key.strip()] = value.strip()
    return data


def dump(data):
    chunks = []
    for i, (section, vals) in enumerate(data.items()):
        if i:
            chunks.append('')
        chunks.append(f'[{section}]')
        for k, v in vals.items():
            chunks.append(f'{k}={v}')
    return '\n'.join(chunks)
