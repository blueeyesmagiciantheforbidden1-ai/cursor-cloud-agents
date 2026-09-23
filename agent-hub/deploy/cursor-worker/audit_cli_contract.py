"""Bounded public source inspection; no authentication or executable loading."""
import json
from pathlib import Path
import re
import tarfile
HERE = Path(__file__).resolve().parent
patterns = ('CURSOR_DISABLE_AUTO_UPDATE', 'DISABLE_AUTO_UPDATE', 'CURSOR_CONFIG_DIR', 'AGENT_CLI_CREDENTIAL_STORE',
            '--reasoning-effort', '--effort', 'composer-2.5', 'cursor-agent-sea', '--trust', '--sandbox')
result = {pattern: [] for pattern in patterns}
with tarfile.open(HERE / '.cache/agent-cli-package.tar.gz', 'r:gz') as archive:
    for item in archive:
        if not (item.isfile() and item.name.count('/') == 1 and item.name.endswith('.js') and item.size < 100_000_000):
            continue
        data = archive.extractfile(item).read().decode('utf-8', errors='replace')
        for pattern in patterns:
            if len(result[pattern]) >= 3:
                continue
            found = data.find(pattern)
            if found >= 0:
                result[pattern].append({'file': item.name, 'context': data[max(0, found-160):found+len(pattern)+240]})
(HERE / 'provenance/cli-contract-audit.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps(result, indent=2))
