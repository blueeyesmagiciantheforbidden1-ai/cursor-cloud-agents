"""Inspect only the newly enrolled credential structure; emit no values."""
import json
from pathlib import Path
import re

ARTROOT = Path(__file__).resolve().parents[3]
PRIVATE = ARTROOT / 'runcrew-private'

def shape(value, depth=0):
    if depth > 3:
        return type(value).__name__
    if isinstance(value, dict):
        return {key: shape(item, depth + 1) for key, item in value.items()
                if isinstance(key, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,47}', key)}
    if isinstance(value, list):
        return {'type': 'list', 'count': len(value), 'first_type': shape(value[0], depth + 1) if value else None}
    return type(value).__name__

def read_json(path, comments=False):
    if path.is_symlink() or not 1 <= path.stat().st_size <= 256_000:
        raise ValueError('Unexpected enrolled credential file')
    text = path.read_text(encoding='utf-8-sig').lstrip()
    if comments:
        while text.startswith('//') or text.startswith('/*'):
            if text.startswith('//'):
                text = text.partition('\n')[2].lstrip()
            else:
                if '*/' not in text:
                    raise ValueError('Unterminated header comment')
                text = text.partition('*/')[2].lstrip()
    return json.loads(text)

def main():
    copilot = read_json(PRIVATE / 'copilot-enrollment/config/config.json', comments=True)
    grok = read_json(PRIVATE / 'grok-enrollment/attempt-s1ogcvh1/GROK_HOME/auth.json')
    scope = 'https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828'
    value = {'copilot': shape(copilot), 'grok': {'expected_scope_present': scope in grok,
             'scope_count': len(grok), 'credential_fields': shape(grok.get(scope))}}
    print(json.dumps(value))

if __name__ == '__main__':
    main()
