"""Read only public archive metadata and a bounded launcher; execute nothing."""
import hashlib
import json
from pathlib import Path
import tarfile
from build_native import safe_members, verify_archive
HERE = Path(__file__).resolve().parent
receipt = json.loads((HERE / 'provenance/release.json').read_text())
verify_archive(HERE / '.cache/agent-cli-package.tar.gz', receipt)
result = {'version': receipt['version'], 'executed': False, 'artifacts': {}}
with tarfile.open(HERE / '.cache/agent-cli-package.tar.gz', 'r:gz') as archive:
    result['safe_extraction_member_count'] = len(safe_members(archive))
    for name in ('cursor-agent', 'cursor-agent-sea', 'cursor-agent-worker-sea', 'package.json', 'node'):
        member = archive.getmember('dist-package/' + name)
        with archive.extractfile(member) as handle:
            data = handle.read()
        item = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'elf': data[:4] == b'\x7fELF'}
        if name in ('cursor-agent', 'package.json') and len(data) < 32000:
            item['text'] = data.decode('utf-8')
        result['artifacts'][name] = item
(HERE / 'provenance/native-audit.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps(result, indent=2))
