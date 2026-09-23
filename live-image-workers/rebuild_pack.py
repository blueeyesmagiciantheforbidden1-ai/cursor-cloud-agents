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
PLACEHOLDER = 'cursor-owner@example.invalid'
_EMAIL = re.compile(r'[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+')


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


def pin(data, owner_email):
    """Put the operator's owner email where the public file has the placeholder."""
    text = data.decode('utf-8')
    if text.count(PLACEHOLDER) != 1:
        raise SystemExit('owner-pinned file must carry the placeholder exactly once')
    return text.replace(PLACEHOLDER, owner_email).encode('utf-8')


def rebuild(manifest_path, out, owner_email=None, expect_tree=None):
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    target = out / manifest['pack']
    if target.exists():
        raise SystemExit(f'{target} already exists; refusing to overwrite')
    pinned = any(entry.get('owner_pin') for entry in manifest['files'])
    if pinned and not (owner_email and _EMAIL.fullmatch(owner_email) and owner_email != PLACEHOLDER):
        raise SystemExit('this pack carries the owner pin: pass --owner-email')
    # A pinned pack's tree digest covers the owner email, so it is not
    # published; the maintainer hands it over privately (--expect-tree).
    expected = manifest['tree_sha256'] if not pinned else expect_tree
    if not (isinstance(expected, str) and re.fullmatch(r'[0-9a-f]{64}', expected)):
        raise SystemExit('pass --expect-tree <sha256> from the maintainer for this pack')
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
        if entry.get('owner_pin'):
            # The public manifest checks the placeholder bytes; the tree
            # digest checks the pinned result.
            if sha256(data) != entry['placeholder_sha256']:
                raise SystemExit(f'{path}: SHA-256 mismatch')
            data = pin(data, owner_email)
            digest = sha256(data)
        else:
            digest = entry['sha256']
            if sha256(data) != digest:
                raise SystemExit(f'{path}: SHA-256 mismatch')
        destination = safe_target(target, path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        written[path] = digest
    digest = tree_digest(written)
    if digest != expected:
        raise SystemExit('tree digest mismatch')
    return target, digest, len(written)


def make(pack, source_map, out, owner_email=None):
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
            elif owner_email and PLACEHOLDER in source.decode('utf-8', 'replace') \
                    and sha256(pin(source, owner_email)) == digest:
                # Publish only the placeholder's hash: a hash of the pinned
                # file would let anyone confirm a guess of the owner email.
                del entry['sha256']
                entry.update(git=mapping[path], owner_pin=True, placeholder_sha256=sha256(source))
            else:
                raise SystemExit(f'{path}: {mapping[path]} is not byte-identical')
        else:
            if len(data) > 4096:
                raise SystemExit(f'{path}: not in git and too large to inline')
            entry['inline_b64'] = base64.b64encode(data).decode()
        entries.append(entry)
        files[path] = digest
    tree = tree_digest(files)
    pinned = any(entry.get('owner_pin') for entry in entries)
    manifest = {'pack': pack.name, 'tree_sha256': None if pinned else tree, 'files': entries}
    Path(out).write_text(json.dumps(manifest, indent=1) + '\n', encoding='utf-8', newline='\n')
    return manifest, tree


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('manifest', nargs='?')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--make', type=Path, help='pack directory to describe (maintainer side)')
    parser.add_argument('--source-map', type=Path)
    parser.add_argument('--owner-email', help='owner pin for codex, cursor and grok packs (never printed)')
    parser.add_argument('--expect-tree', help='tree SHA-256 of a pinned pack, from the maintainer')
    args = parser.parse_args(argv)
    if args.make:
        manifest, tree = make(args.make, args.source_map, args.out, args.owner_email)
        # For a pinned pack the tree digest is printed here for private
        # hand-over and is not written to the public manifest.
        print(json.dumps({'pack': manifest['pack'], 'files': len(manifest['files']),
                          'tree_sha256': tree, 'published': manifest['tree_sha256'] is not None}))
        return 0
    target, digest, count = rebuild(args.manifest, args.out, args.owner_email, args.expect_tree)
    print(json.dumps({'pack': str(target), 'files': count, 'tree_sha256': digest}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
