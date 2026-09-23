"""Download and inspect the pinned public Linux distribution; no execution."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent
VERSION = '1.0.86'
URL = f'https://github.com/github/copilot-cli/releases/download/v{VERSION}/copilot-linux-x64.tar.gz'
SHA256 = 'ea4a519d7b2ff54e9c7d10ae9921cf4ab3f5041489ec340852f47f4f63fc535b'
SIZE = 97_598_899

def validate_members(archive):
    result, seen, total = [], set(), 0
    for member in archive:
        name = PurePosixPath(member.name)
        if (name.is_absolute() or '..' in name.parts or '\\' in member.name
                or not name.parts or str(name) in seen
                or not (member.isfile() or member.isdir())):
            raise ValueError('Unexpected distribution archive member')
        seen.add(str(name))
        total += member.size
        if total > 768 * 1024 * 1024 or len(seen) > 5000:
            raise ValueError('Distribution archive exceeds bounds')
        result.append(member)
    return result

def fetch(path):
    if not path.exists():
        temporary = path.with_suffix('.partial')
        try:
            with urllib.request.urlopen(URL, timeout=45) as response, temporary.open('wb') as output:
                count = 0
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > SIZE:
                        raise ValueError('Distribution larger than published size')
                    output.write(chunk)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if path.stat().st_size != SIZE or digest != SHA256:
        raise ValueError('Distribution does not match GitHub published digest')

def main():
    cache = ROOT / '.cache'
    cache.mkdir(exist_ok=True)
    path = cache / 'copilot-linux-x64.tar.gz'
    fetch(path)
    with tarfile.open(path, 'r:gz') as archive:
        members = validate_members(archive)
        files = []
        for member in members:
            if member.isfile():
                with archive.extractfile(member) as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                files.append({'name': member.name, 'size': member.size, 'sha256': digest,
                              'executable': bool(member.mode & 0o111)})
    value = {'schema': 1, 'version': VERSION, 'url': URL, 'archive_sha256': SHA256,
             'archive_size': SIZE, 'files': files, 'native_execution_verified': False}
    (ROOT / 'provenance' / 'artifact-lock.json').write_text(json.dumps(value, indent=2), encoding='utf-8')
    print(json.dumps(value))

if __name__ == '__main__':
    main()
