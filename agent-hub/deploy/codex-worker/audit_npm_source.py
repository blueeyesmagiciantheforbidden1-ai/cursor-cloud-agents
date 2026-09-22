"""Reproduce the public npm-wrapper source audit; never execute npm scripts."""
import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request


def main():
    root = Path(__file__).parent / 'provenance'
    metadata = json.loads((root / 'npm.json').read_text())
    url = 'https://registry.npmjs.org/@openai/codex/-/codex-0.155.1.tgz'
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(200001)
    if len(data) > 200000 or ('sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode()
                             != metadata['dist']['integrity']):
        raise ValueError('npm_integrity_mismatch')
    saved = []
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        for item in archive:
            if item.isfile() and item.name in ('package/bin/codex.js', 'package/package.json'):
                body = archive.extractfile(item).read()
                (root / ('npm-' + Path(item.name).name)).write_bytes(body)
                saved.append({'name': item.name, 'sha256': hashlib.sha256(body).hexdigest()})
    (root / 'npm-source-audit.json').write_text(json.dumps({'url': url, 'executed': False,
        'integrity_verified': True, 'files': saved}, indent=2), encoding='utf-8')
    print(json.dumps(saved))


if __name__ == '__main__':
    main()
