"""Acquire a pinned native package and verify its offline protocol surface.

Run only in an isolated Linux build stage. This does not authenticate or infer.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent
LOCK = ROOT / 'provenance' / 'archive-audit.json'
VERSION = '0.155.1'
SHA256 = 'a65b895c6ac1a73629bbe4b864640c86133e94a43b4d67b3103044e1a306d5a2'
URL = 'https://github.com/openai/codex/releases/download/rust-v0.155.1/codex-package-x86_64-unknown-linux-musl.tar.gz'
SIZE = 138838055
SCHEMA_FIELDS = {
    'GetAccountResponse': {'account', 'requiresOpenaiAuth'},
    'GetAccountRateLimitsResponse': {'accountId', 'ordinaryUsageAllowed', 'rateLimitsByLimitId'},
    'ModelListResponse': {'data', 'nextCursor'},
    'ConfigReadResponse': {'config', 'layers'},
    'ThreadStartParams': {'model', 'modelProvider', 'cwd', 'approvalPolicy', 'sandbox', 'config', 'ephemeral'},
    'ThreadStartResponse': {'thread', 'model', 'modelProvider', 'reasoningEffort', 'cwd', 'approvalPolicy', 'sandbox'},
    'TurnStartParams': {'threadId', 'input', 'model', 'effort', 'sandboxPolicy'},
}


def safe_member(name):
    p = PurePosixPath(name)
    if not name or '\\' in name or ':' in name or p.is_absolute() or any(x in ('', '.', '..') for x in name.split('/')):
        raise ValueError('unsafe_archive_path')
    return p


def extract_verified(archive_path, destination, manifest):
    """Destination must be a new, protected build directory; no generic tar extract."""
    destination = Path(destination)
    if destination.exists():
        raise ValueError('new_destination_required')
    expected = {x['name']: x for x in manifest['members']}
    if len(expected) != len(manifest['members']) or len(expected) > 256:
        raise ValueError('invalid_archive_manifest')
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise ValueError('archive_members_mismatch')
        for item in members:
            safe_member(item.name)
            ref = expected[item.name]
            if not (item.isfile() or item.isdir()) or ref['type'] != item.type.decode() or item.size != ref['size']:
                raise ValueError('archive_member_type_or_size_mismatch')
            if item.isfile():
                with archive.extractfile(item) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != ref['sha256']:
                        raise ValueError('archive_member_digest_mismatch')
        # Verify EVERYTHING before writing any files. No links are accepted.
        destination.mkdir(mode=0o755, parents=False)
        for item in members:
            target = destination.joinpath(*safe_member(item.name).parts)
            if item.isdir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                with archive.extractfile(item) as source, target.open('xb') as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                # Retain executability, never setuid/setgid/world-write.
                target.chmod(0o755 if item.mode & 0o111 else 0o644)


def check_schemas(directory):
    digests = {}
    for name, required in SCHEMA_FIELDS.items():
        matches = list(Path(directory).rglob(name + '.json'))
        if len(matches) != 1:
            raise ValueError('native_schema_missing_' + name)
        body = matches[0].read_bytes()
        schema = json.loads(body)
        if not required <= set(schema.get('properties', {})):
            raise ValueError('native_schema_incompatible_' + name)
        digests[name] = hashlib.sha256(body).hexdigest()
    return digests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise SystemExit('linux_amd64_build_required')
    manifest = json.loads(LOCK.read_text())
    if (manifest['sha256'], manifest['url'], manifest['size'], manifest['version']) != (SHA256, URL, SIZE, VERSION):
        raise SystemExit('release_lock_mismatch')
    with tempfile.TemporaryDirectory() as temporary:
        scratch = Path(temporary)
        archive_path = scratch / 'native.tar.gz'
        digest, length = hashlib.sha256(), 0
        with urllib.request.urlopen(URL, timeout=60) as response, archive_path.open('xb') as output:
            while chunk := response.read(1024 * 1024):
                length += len(chunk)
                if length > SIZE:
                    raise ValueError('archive_size_mismatch')
                digest.update(chunk)
                output.write(chunk)
        if length != SIZE or digest.hexdigest() != SHA256:
            raise ValueError('archive_digest_mismatch')
        extract_verified(archive_path, args.destination, manifest)
        executable = str(Path(args.destination).resolve() / 'bin' / 'codex')
        home = scratch / 'empty-home'
        home.mkdir()
        env = {'HOME': str(home), 'CODEX_HOME': str(home / '.codex'), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        version = subprocess.run([executable, '--version'], env=env, cwd=home, check=True,
                                 capture_output=True, timeout=30).stdout.decode().strip()
        if version != 'codex-cli ' + VERSION:
            raise ValueError('native_version_mismatch')
        schema_dir = scratch / 'schemas'
        subprocess.run([executable, 'app-server', 'generate-json-schema', '--out', str(schema_dir)],
                       env=env, cwd=home, check=True, capture_output=True, timeout=60)
        receipt = {'version': VERSION, 'archive_sha256': SHA256, 'schema_sha256': check_schemas(schema_dir),
                   'inference_executed': False, 'authenticated': False,
                   'compatibility': 'field_surface_only_runtime_validation_required'}
        (Path(args.destination) / 'build-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    main()
