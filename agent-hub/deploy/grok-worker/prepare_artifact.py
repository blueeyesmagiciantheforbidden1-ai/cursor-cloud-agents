"""Pin bytes from the official Grok download; no credentials or execution."""
import gzip
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parent
VERSION = '1.0.40'
URL = f'https://x.ai/cli/grok-{VERSION}-linux-x86_64.gz'
MAX_SIZE = 384 * 1024 * 1024

def main():
    cache = ROOT / '.cache'
    cache.mkdir(exist_ok=True)
    compressed = cache / 'grok.gz'
    if not compressed.exists():
        partial = compressed.with_suffix('.partial')
        try:
            with urllib.request.urlopen(URL, timeout=45) as response, partial.open('wb') as output:
                count = 0
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > MAX_SIZE:
                        raise ValueError('Download exceeds bound')
                    output.write(chunk)
            partial.replace(compressed)
        finally:
            partial.unlink(missing_ok=True)
    binary = cache / 'grok'
    with gzip.open(compressed, 'rb') as source, binary.open('wb') as output:
        first = source.read(64)
        if first[:6] != b'\x7fELF\x02\x01' or first[18:20] != b'\x3e\x00':
            raise ValueError('Expected Linux x86_64 ELF')
        output.write(first)
        size = len(first)
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_SIZE:
                raise ValueError('Native binary exceeds bound')
            output.write(chunk)
    with compressed.open('rb') as stream:
        archive_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    with binary.open('rb') as stream:
        binary_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    value = {'schema': 1, 'version': VERSION, 'url': URL,
             'archive_size': compressed.stat().st_size, 'archive_sha256': archive_digest,
             'binary_size': size, 'binary_sha256': binary_digest,
             'provenance': 'HTTPS download from installer-documented x.ai origin; locally recorded hash, not vendor signature',
             'native_execution_verified': False}
    (ROOT / 'provenance' / 'artifact-lock.json').write_text(json.dumps(value, indent=2), encoding='utf-8')
    print(json.dumps(value))

if __name__ == '__main__':
    main()
