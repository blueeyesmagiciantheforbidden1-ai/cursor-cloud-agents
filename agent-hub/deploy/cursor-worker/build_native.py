"""Build-only extraction of the pinned public Cursor distribution; no execution."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile

from prepare_release import EXPECTED_BYTES, MAX_EXPANDED_BYTES, OBSERVED_SHA256, URL, VERSION, ReleaseError, download


def validate_release(value):
    if (not isinstance(value, dict) or value.get('version') != VERSION or value.get('platform') != 'linux-x64'
            or value.get('url') != URL or value.get('bytes') != EXPECTED_BYTES
            or value.get('sha256') != OBSERVED_SHA256
            or value.get('provenance') != 'official_https_observation'
            or value.get('publisher_signature_verified') is not False):
        raise ReleaseError('Release does not match the audited public metadata')
    return value


def verify_archive(path, receipt):
    validate_release(receipt)
    if path.stat().st_size != EXPECTED_BYTES:
        raise ReleaseError('Archive size changed')
    with path.open('rb') as handle:
        if hashlib.file_digest(handle, 'sha256').hexdigest() != receipt['sha256']:
            raise ReleaseError('Archive digest changed')


def safe_members(archive):
    rows, names, roots, expanded = [], set(), set(), 0
    for item in archive:
        path = PurePosixPath(item.name)
        if (path.is_absolute() or not path.parts or '..' in path.parts or '\\' in item.name
                or path.as_posix() in names or not (item.isfile() or item.isdir())):
            raise ReleaseError('Unsupported or unsafe archive member')
        names.add(path.as_posix()); roots.add(path.parts[0])
        expanded += item.size
        if len(rows) >= 20000 or expanded > MAX_EXPANDED_BYTES:
            raise ReleaseError('Archive exceeds extraction bounds')
        rows.append((item, path))
    if roots != {'dist-package'}:
        raise ReleaseError('Archive must have one distribution root')
    return rows


def extract_archive(path, destination):
    if destination.exists():
        raise ReleaseError('Extraction destination must be new')
    with tarfile.open(path, 'r:gz') as archive:
        rows = safe_members(archive)
        destination.mkdir(mode=0o755, parents=True)
        root = destination.resolve(strict=True)
        for item, relative in rows:
            relative = Path(*relative.parts[1:])
            target = destination / relative
            if not target.resolve().is_relative_to(root):
                raise ReleaseError('Archive path escaped destination')
            if item.isdir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            source = archive.extractfile(item)
            if source is None:
                raise ReleaseError('Missing archive data')
            with source, target.open('xb') as output:
                while block := source.read(1024 * 1024):
                    output.write(block)
            target.chmod(0o555 if item.mode & 0o111 else 0o444)
    files = {}
    for file in sorted(destination.rglob('*')):
        if file.is_file():
            with file.open('rb') as handle:
                files[file.relative_to(destination).as_posix()] = hashlib.file_digest(handle, 'sha256').hexdigest()
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    receipt = validate_release(json.loads(args.release.read_text(encoding='utf-8')))
    archive = Path('/tmp/cursor-linux-release.tar.gz')
    download(archive)
    verify_archive(archive, receipt)
    files = extract_archive(archive, args.output)
    profile = {'schema': 1, 'version': VERSION, 'platform': 'linux-x64',
               'archive_sha256': receipt['sha256'], 'files': files,
               'publisher_signature_verified': False, 'executed': False}
    profile_path = args.output / 'runtime-profile.json'
    profile_path.write_text(json.dumps(profile, sort_keys=True), encoding='utf-8')
    profile_path.chmod(0o444)
    archive.unlink()
    print(json.dumps({'version': VERSION, 'files_verified': len(files), 'provider_calls': 0}))


if __name__ == '__main__':
    main()
