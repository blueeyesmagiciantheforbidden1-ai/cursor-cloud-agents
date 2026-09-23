"""Fetch public release metadata only; never execute downloaded content."""
import json
from pathlib import Path
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
    release = json.loads(fetch('https://api.github.com/repos/github/copilot-cli/releases/latest'))
    (out / 'release.json').write_text(json.dumps(release, indent=2), encoding='utf-8')
    print(json.dumps({'tag': release['tag_name'], 'assets': [
        {k: a.get(k) for k in ('name', 'size', 'digest', 'browser_download_url')}
        for a in release['assets'] if 'linux-x64' in a['name'] or 'sha' in a['name'].lower()]}))

if __name__ == '__main__':
    main()
