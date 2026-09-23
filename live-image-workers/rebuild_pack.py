"""Rebuild a worker image pack from this public repository and verify every byte.

A pack whose files are all public (no owner pin) does not have to travel as a
tarball between machines. Its manifest (live-image-workers/packs/<name>.json)
lists each file with its SHA-256 and where the bytes come from: a git blob in
this repository ("git": "<commit>:<path>") or, for the few small build files
that are not in git, the content itself ("inline_b64"). A git entry may carry
"eol": "crlf" when the pack copy has CRLF line endings and git stores LF; the
conversion is exact and the SHA-256 is checked after it. This script writes the
tree and refuses to finish unless every file matches, then prints the tree
digest (SHA-256 over the sorted "<sha256>  <path>" lines) to compare with the
digest recorded when the pack was built.

    python live-image-workers/rebuild_pack.py live-image-workers/packs/claude-live-20260923c.json --out <dir>

The manifest is made from a built pack with --make (maintainer side):

    python live-image-workers/rebuild_pack.py --make <pack dir> --source-map <json> --out <manifest>
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
_SPEC = re.compile(r'[0-9a-f]{7,40}:[A-Za-z0-9_./-]+')
_PATH = re.compile(r'(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+')
CR, LF, CRLF = b'\x0d', b'\x0a', b'\x0d\x0a'


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def tree_digest(files):
    lines = ''.join(f'{digest}  {path}\n' for path, digest in sorted(files.items()))
    return sha256(lines.encode())


def blob(spec):
    if not _SPEC.fullmatch(spec):
        raise SystemExit(f'bad git source {spec!r}')
    result = subprocess.run(['git', '-C', str(ROOT), 'cat-file', 'blob', spec], capture_output=True)
    if result.returncode != 0:
        raise SystemExit(f'git blob {spec} is not available; fetch the repository first')
    return result.stdout


def to_crlf(data):
    if CR in data:
        raise SystemExit('eol crlf on a blob that already contains CR')
    return data.replace(LF, CRLF)


def safe_target(out, path):
    if not _PATH.fullmatch(path) or '..' in path.split('/'):
        raise SystemExit(f'bad path {path!r}')
    return out.joinpath(*path.split('/'))


def rebuild(manifest_path, out):
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    target = out / manifest['pack']
    if target.exists():
        raise SystemExit(f'{target} already exists; refusing to overwrite')
    written = {}
    for entry in manifest['files']:
        path = entry['path']
        if 'git' in entry:
            data = blob(entry['git'])
            if entry.get('eol') == 'crlf':
                data = to_crlf(data)
            elif 'eol' in entry:
                raise SystemExit(f'{path}: unknown eol {entry["eol"]!r}')
        else:
            data = base64.b64decode(entry['inline_b64'], validate=True)
        if sha256(data) != entry['sha256']:
            raise SystemExit(f'{path}: SHA-256 mismatch')
        destination = safe_target(target, path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        written[path] = entry['sha256']
    digest = tree_digest(written)
    if digest != manifest['tree_sha256']:
        raise SystemExit('tree digest mismatch')
    return target, digest, len(written)


def make(pack, source_map, out):
    """source_map: {"<pack path>": "<commit>:<repo path>"}; unmapped files are inlined."""
    pack = Path(pack)
    mapping = json.loads(Path(source_map).read_text(encoding='utf-8'))
    files, entries = {}, []
    for file in sorted(p for p in pack.rglob('*') if p.is_file() and '__pycache__' not in p.parts):
        path = file.relative_to(pack).as_posix()
        data = file.read_bytes()
        digest = sha256(data)
        entry = {'path': path, 'sha256': digest}
        if path in mapping:
            source = blob(mapping[path])
            if sha256(source) == digest:
                entry['git'] = mapping[path]
            elif CR not in source and sha256(to_crlf(source)) == digest:
                entry['git'], entry['eol'] = mapping[path], 'crlf'
            else:
                raise SystemExit(f'{path}: {mapping[path]} is not byte-identical')
        else:
            if len(data) > 4096:
                raise SystemExit(f'{path}: not in git and too large to inline')
            entry['inline_b64'] = base64.b64encode(data).decode()
        entries.append(entry)
        files[path] = digest
    manifest = {'pack': pack.name, 'tree_sha256': tree_digest(files), 'files': entries}
    Path(out).write_text(json.dumps(manifest, indent=1) + '\n', encoding='utf-8', newline='\n')
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('manifest', nargs='?')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--make', type=Path, help='pack directory to describe (maintainer side)')
    parser.add_argument('--source-map', type=Path)
    args = parser.parse_args(argv)
    if args.make:
        manifest = make(args.make, args.source_map, args.out)
        print(json.dumps({'pack': manifest['pack'], 'files': len(manifest['files']),
                          'tree_sha256': manifest['tree_sha256']}))
        return 0
    target, digest, count = rebuild(args.manifest, args.out)
    print(json.dumps({'pack': str(target), 'files': count, 'tree_sha256': digest}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
