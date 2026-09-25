"""Minimal INI parse/dump — starter with gaps."""


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
            continue
        key, _, value = line.partition('=')
        if section is None:
            section = 'default'
            data.setdefault(section, {})
        data[section][key] = value
    return data


def dump(data):
    parts = []
    for section, vals in data.items():
        parts.append(f'[{section}]')
        for k, v in vals.items():
            parts.append(f'{k} = {v}')
        parts.append('')
    return '\n'.join(parts)
