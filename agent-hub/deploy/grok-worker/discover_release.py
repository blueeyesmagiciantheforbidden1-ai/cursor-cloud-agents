"""Fetch public Grok installation metadata without auth or execution."""
import hashlib
import json
from pathlib import Path
import re
import urllib.request

ROOT = Path(__file__).resolve().parent

def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'RunCrew-release-audit'})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise ValueError('Public metadata exceeds size bound')
    return body

def main():
    out = ROOT / 'provenance'
    out.mkdir(exist_ok=True)
    script = fetch('https://x.ai/cli/install.sh')
    (out / 'official-install.sh').write_bytes(script)
    version = fetch('https://x.ai/cli/stable').decode().strip()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9._]+)?', version):
        raise ValueError('Unexpected public release pointer')
    value = {'version': version, 'installer_sha256': hashlib.sha256(script).hexdigest(),
             'url': f'https://x.ai/cli/grok-{version}-linux-x86_64'}
    (out / 'release.json').write_text(json.dumps(value, indent=2), encoding='utf-8')
    print(json.dumps(value))

if __name__ == '__main__':
    main()
