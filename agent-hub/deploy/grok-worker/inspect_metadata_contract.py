"""Bounded public native-string inspection; never executes the downloaded CLI."""
from pathlib import Path
import hashlib
import json
import sys

path = Path(__file__).resolve().parent / '.cache/grok'
body = path.read_bytes()
if hashlib.sha256(body).hexdigest() != '92c997dfd109c0672d40d5ae6fbd15835d53ffaf12cf9ea124d22aaef3ff23fc':
    raise SystemExit('Pinned artifact changed')
result = {}
for term in sys.argv[1:]:
    needle = term.strip('"').encode('ascii')
    if not 1 <= len(needle) <= 100:
        raise SystemExit('Invalid public source query')
    start, found = 0, []
    while len(found) < 3:
        offset = body.find(needle, start)
        if offset < 0:
            break
        found.append({'offset': offset, 'context': body[max(0, offset-300):offset+1200].decode('ascii', errors='replace')})
        start = offset + len(needle)
    result[needle.decode()] = found
print(json.dumps(result, indent=2))
