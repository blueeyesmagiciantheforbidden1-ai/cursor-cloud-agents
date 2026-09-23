"""Inspect the public pinned Cursor Linux archive without executing its code."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

VERSION = '2026.09.18-9a7762b'
URL = f'https://downloads.cursor.com/lab/{VERSION}/linux/x64/agent-cli-package.tar.gz'
EXPECTED_BYTES = 182574768
OBSERVED_SHA256 = 'b1308f5a2fc05458b9d8966752986bb23a971bbcc67c842c1df94c4b8132bad9'
MAX_EXPANDED_BYTES = 900_000_000
HERE = Path(__file__).resolve().parent


class ReleaseError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download(destination: Path):
    if destination.exists():
        raise ReleaseError('Refusing to replace an existing archive')
    opener = build_opener(ProxyHandler({}), NoRedirect())
    total = 0
    with opener.open(Request(URL, headers={'User-Agent': 'RunCrew-release-audit/1'}), timeout=60) as response:
        if response.status != 200 or int(response.headers.get('Content-Length', '-1')) != EXPECTED_BYTES:
            raise ReleaseError('Release size or status differs from the observed pin')
        with destination.open('xb') as output:
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > EXPECTED_BYTES:
                    raise ReleaseError('Release exceeded its byte bound')
                output.write(block)
    if total != EXPECTED_BYTES:
        raise ReleaseError('Truncated release')


def inspect_archive(path: Path):
    if path.stat().st_size != EXPECTED_BYTES:
        raise ReleaseError('Unexpected archive length')
    with path.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    files = []
    total = 0
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or '\\' in member.name:
                raise ReleaseError('Unsafe archive path')
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ReleaseError('Unsupported archive entry type')
            if member.issym():
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or '..' in target.parts or '\\' in member.linkname:
                    raise ReleaseError('Unsafe archive symlink')
            total += member.size
            if total > MAX_EXPANDED_BYTES or len(files) >= 20000:
                raise ReleaseError('Expanded archive exceeds bound')
            files.append({'path': member.name, 'bytes': member.size, 'type': 'file' if member.isfile() else 'directory' if member.isdir() else 'symlink'})
    return {'schema': 1, 'version': VERSION, 'platform': 'linux-x64', 'url': URL,
            'bytes': EXPECTED_BYTES, 'sha256': digest, 'expanded_bytes': total,
            'publisher_signature_verified': False, 'provenance': 'official_https_observation',
            'executed': False, 'files': files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    cache = HERE / '.cache'
    cache.mkdir(exist_ok=True)
    path = cache / 'agent-cli-package.tar.gz'
    if args.download:
        download(path)
    receipt = inspect_archive(path)
    provenance = HERE / 'provenance'
    provenance.mkdir(exist_ok=True)
    (provenance / 'release.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in receipt.items() if key != 'files'}))


if __name__ == '__main__':
    main()
