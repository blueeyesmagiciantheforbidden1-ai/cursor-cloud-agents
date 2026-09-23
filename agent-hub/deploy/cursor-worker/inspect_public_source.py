"""Print bounded excerpts from the downloaded public CLI, without execution."""
from pathlib import Path
import json
import sys
import tarfile

HERE = Path(__file__).resolve().parent
patterns = [p.strip('"') for p in sys.argv[1:]]
if len(patterns) == 4 and patterns[0] == '--chunk':
    name, start, size = patterns[1], int(patterns[2]), int(patterns[3])
    if '/' in name or not name.endswith('.js') or start < 0 or not 1 <= size <= 20000:
        raise SystemExit('Invalid bounded chunk')
    with tarfile.open(HERE / '.cache/agent-cli-package.tar.gz', 'r:gz') as archive:
        source = archive.extractfile('dist-package/' + name).read().decode('utf-8')
    print(source[start:start+size])
    raise SystemExit(0)
if not patterns or any(not 1 <= len(p) <= 100 for p in patterns):
    raise SystemExit('Supply bounded literal source patterns')
results = {p: [] for p in patterns}
with tarfile.open(HERE / '.cache/agent-cli-package.tar.gz', 'r:gz') as archive:
    for item in archive:
        if not (item.isfile() and item.name.count('/') == 1 and item.name.endswith('.js') and item.size < 100_000_000):
            continue
        source = archive.extractfile(item).read().decode('utf-8', errors='replace')
        for pattern in patterns:
            start = 0
            while len(results[pattern]) < 8:
                found = source.find(pattern, start)
                if found < 0:
                    break
                module = source.rfind('"./src/', 0, found)
                module_name = source[module+1:source.find('"', module+1)] if module >= 0 else None
                results[pattern].append({'file': item.name, 'module': module_name,
                                        'offset': found, 'context': source[max(0, found-250):found+len(pattern)+2200]})
                start = found + len(pattern)
print(json.dumps(results, indent=2))
