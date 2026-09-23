"""Immutable source packaging and on-demand Cloud Shell restore; stdlib only.

This module never uploads, installs packages, logs in, starts a service, or runs
project/model code. The operator supplies the approved archive SHA and generation.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import threading
import time

PROJECT = 'project-0c6d31fa-509e-4116-a2c'
BUCKET = 'runcrew-496481413971-source'
POLICY = 'runcrew-source-v1'
MANIFEST = '.runcrew-source-manifest.json'
ORIGIN = '.runcrew-origin.json'
MAX_FILES = 2048
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 40 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
ROOTS = frozenset(('agent_hub', 'tests', 'docs', 'deploy', 'chatgpt', 'connectors'))
ROOT_FILES = frozenset(('AGENTS.md', 'README.md', 'Dockerfile', 'requirements.txt',
    'pyproject.toml', 'LICENSE', '.dockerignore', '.gcloudignore', '.gitignore'))
EXTENSIONS = frozenset(('.py', '.md', '.json', '.yaml', '.yml', '.toml', '.txt',
    '.js', '.ts', '.tsx', '.jsx', '.html', '.css', '.sh', '.ps1', '.service',
    '.svg', '.asc', '.sig', '.example'))
EXCLUDED_DIRS = frozenset(('.git', '.hg', '.svn', '.codex', '.agents', '.venv',
    'venv', 'env', 'node_modules', '__pycache__', '.cache', '.pytest_cache',
    '.mypy_cache', 'cache', 'dist', 'build', 'runtime', 'private', 'runcrew-private',
    'credentials', 'secrets', 'auth', 'backups', 'backup', 'logs'))
SECRET_NAMES = frozenset(('auth.json', 'credentials.json', 'gateway-credentials.json',
    'oauth-token.txt', 'hub-tokens.json', 'token.json', 'tokens.json', 'id_rsa',
    'id_ed25519', 'application_default_credentials.json'))
SECRET_MARKERS = re.compile(
    r'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----'
    r'|\b(?:sk-|xai-|crsr_|gh[pousr]_|github_pat_|ya29\.)[A-Za-z0-9._-]{12,}'
    r'|\bBearer\s+[A-Za-z0-9._-]{20,}'
    r'|["\x27](?:access_token|refresh_token|client_secret|api_key|private_key)["\x27]'
    r'\s*:\s*["\x27][A-Za-z0-9._/+=-]{16,}["\x27]', re.IGNORECASE)


class WorkspaceError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def strict_json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise WorkspaceError('Invalid bounded JSON metadata') from None


def valid_digest(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def portable_path(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 240:
        raise WorkspaceError('Invalid source path')
    parts = value.split('/')
    if (any(part in ('', '.', '..') or not re.fullmatch('[A-Za-z0-9_.-]{1,128}', part)
            or part.endswith('.') or re.fullmatch(r'(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)
            for part in parts) or '\\' in value):
        raise WorkspaceError('Invalid portable source path')
    return PurePosixPath(value)


def allowed_source(value):
    path = portable_path(value)
    parts = path.parts
    if any(part.lower() in EXCLUDED_DIRS for part in parts[:-1]):
        return False
    name = parts[-1]
    if (name.lower() in SECRET_NAMES or name.lower().startswith('.env')
            or name.lower().endswith(('.pem', '.key', '.p12', '.pfx', '.sqlite', '.db'))):
        return False
    if len(parts) == 1:
        return value in ROOT_FILES
    if parts[0] == 'vendor':
        if len(parts) < 3 or parts[1] != 'ryan-frontier':
            return False
    elif parts[0] not in ROOTS:
        return False
    return (path.suffix.lower() in EXTENSIONS or name in ROOT_FILES
            or name.startswith('Dockerfile.') or name.endswith('.dockerignore'))


def fixture_scope(path):
    parts = PurePosixPath(path).parts
    return parts[0] == 'tests' or 'fixtures' in parts or parts[-1].startswith('test_')


def exceptions_map(values):
    if not isinstance(values, list) or len(values) > MAX_FILES:
        raise WorkspaceError('Fixture exceptions must be a bounded list')
    result = {}
    for item in values:
        if (not isinstance(item, dict) or set(item) != {'path', 'sha256', 'reason'}
                or not allowed_source(item['path']) or not fixture_scope(item['path'])
                or not valid_digest(item['sha256']) or not isinstance(item['reason'], str)
                or not 10 <= len(item['reason']) <= 300 or item['path'] in result
                or SECRET_MARKERS.search(item['reason'])):
            raise WorkspaceError('Fixture exception must bind one reviewed test file and its digest')
        result[item['path']] = dict(item)
    return result


def validate_content(path, data, exceptions):
    if len(data) > MAX_FILE_BYTES or b'\x00' in data:
        raise WorkspaceError('Source file is oversized or binary: ' + path)
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        raise WorkspaceError('Source file must be UTF-8: ' + path) from None
    exception = exceptions.get(path)
    if exception and exception['sha256'] != sha256(data):
        raise WorkspaceError('Fixture exception digest changed: ' + path)
    if SECRET_MARKERS.search(text) and exception is None:
        # Report only the source path, never the matching material.
        raise WorkspaceError('Credential marker requires exclusion or reviewed fixture exception: ' + path)


def regular_bytes(path, limit):
    # Reject special files before opening (opening a FIFO can otherwise block).
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise WorkspaceError('Source inputs must be regular files, not links')
    flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(os.open(path, flags), 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise WorkspaceError('Source inputs must be bounded regular files, not links')
        data = handle.read(limit + 1)
        if len(data) > limit:
            raise WorkspaceError('Source input exceeds byte limit')
        return data


def source_files(source, fixture_exceptions=()):
    """Return a frozen allowlisted byte snapshot; never follow excluded folders."""
    source = Path(source).absolute()
    if source.is_symlink() or not source.is_dir() or source.resolve() != source:
        raise WorkspaceError('Source root must be a real absolute directory')
    exceptions = exceptions_map(list(fixture_exceptions))
    files, skipped, seen = {}, 0, set()
    def walk(directory, prefix=''):
        nonlocal skipped
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            relative = prefix + entry.name
            if entry.is_symlink():
                raise WorkspaceError('Source tree contains a symbolic link: ' + relative)
            if entry.is_dir(follow_symlinks=False):
                if (entry.name.lower() in EXCLUDED_DIRS or (not prefix and entry.name not in ROOTS | {'vendor'})
                        or (prefix == 'vendor/' and entry.name != 'ryan-frontier')):
                    skipped += 1
                    continue
                portable_path(relative)
                walk(Path(entry.path), relative + '/')
            elif allowed_source(relative):
                if relative.lower() in seen:
                    raise WorkspaceError('Case-colliding source paths')
                seen.add(relative.lower())
                data = regular_bytes(Path(entry.path), MAX_FILE_BYTES)
                validate_content(relative, data, exceptions)
                files[relative] = data
                if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_SOURCE_BYTES:
                    raise WorkspaceError('Source snapshot exceeds fixed bounds')
            else:
                skipped += 1
    walk(source)
    if not files or set(exceptions) - set(files):
        raise WorkspaceError('Empty snapshot or unused fixture exception')
    return files, sorted(exceptions.values(), key=lambda item: item['path']), skipped


def package_snapshot(source, output, *, fixture_exceptions=()):
    """Create a NEW local artifact directory. The caller owns reviewed upload."""
    source, output = Path(source).absolute(), Path(output).absolute()
    if output.resolve() != output or output == source or output.is_relative_to(source):
        raise WorkspaceError('Artifact output must be outside the source tree')
    files, exceptions, skipped = source_files(source, fixture_exceptions)
    manifest = dict(schema_version=1, policy=POLICY,
        files=[dict(path=path, size=len(data), sha256=sha256(data)) for path, data in sorted(files.items())],
        total_bytes=sum(map(len, files.values())), fixture_exceptions=exceptions)
    manifest_bytes = canonical(manifest)
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise WorkspaceError('Manifest exceeds fixed bounds')
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    archive = output / 'source.tar'
    with archive.open('xb') as handle:
        with tarfile.open(fileobj=handle, mode='w', format=tarfile.USTAR_FORMAT) as tar:
            for name, data in [(MANIFEST, manifest_bytes), *sorted(files.items())]:
                member = tarfile.TarInfo(name)
                member.size, member.mode, member.mtime = len(data), 0o644, 0
                member.uid = member.gid = 0
                member.uname = member.gname = ''
                tar.addfile(member, io.BytesIO(data))
    archive_bytes = regular_bytes(archive, MAX_ARCHIVE_BYTES)
    (output / 'source-manifest.json').write_bytes(manifest_bytes)
    return dict(schema_version=1, policy=POLICY, snapshot_sha256=sha256(archive_bytes),
        archive_path=str(archive), archive_size=len(archive_bytes), manifest_path=str(output / 'source-manifest.json'),
        manifest_sha256=sha256(manifest_bytes), source_file_count=len(files), excluded_entries=skipped,
        object_prefix='gs://' + BUCKET + '/cloud-workspaces/' + sha256(archive_bytes) + '/')


def object_url(snapshot_sha256, generation):
    if not valid_digest(snapshot_sha256) or not isinstance(generation, str) or not re.fullmatch('[1-9][0-9]{0,29}', generation):
        raise WorkspaceError('An exact snapshot SHA256 and positive generation are required')
    return f'gs://{BUCKET}/cloud-workspaces/{snapshot_sha256}/source.tar#{generation}'


def download_archive(snapshot_sha256, generation, *, timeout=180, executable=None):
    """One bounded read using the browser's existing Cloud Shell Google identity."""
    url = object_url(snapshot_sha256, generation)
    if not 1 <= timeout <= 600:
        raise WorkspaceError('Download timeout is outside bounds')
    executable = executable or shutil.which('gcloud')
    if not executable:
        raise WorkspaceError('Cloud Shell gcloud is required')
    command = [str(executable), 'storage', 'cat', url, '--project=' + PROJECT,
               '--quiet', '--verbosity=error']
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, shell=False, start_new_session=os.name == 'posix',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError:
        raise WorkspaceError('Unable to launch Cloud Shell gcloud') from None
    events, stop = queue.Queue(maxsize=2), threading.Event()
    def reader():
        try:
            while not stop.is_set():
                chunk = process.stdout.read(65536)
                while not stop.is_set():
                    try:
                        events.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        pass
                if not chunk:
                    return
        except (OSError, ValueError):
            stop.set()
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline, chunks, total = time.monotonic() + timeout, [], 0
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or stop.is_set():
                raise WorkspaceError('Bounded source download stopped')
            try:
                chunk = events.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                continue
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise WorkspaceError('Source archive exceeds download limit')
            chunks.append(chunk)
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise WorkspaceError('Cloud source read failed; check browser account access')
        return b''.join(chunks)
    except subprocess.TimeoutExpired:
        raise WorkspaceError('Bounded source download stopped') from None
    finally:
        stop.set()
        if process.poll() is None:
            try:
                if os.name == 'posix':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        process.wait()
        process.stdout.close()
        thread.join(timeout=1)


def inspect_archive(data, expected_sha256):
    """Validate every byte/member before creating a workspace or executing code."""
    if (not isinstance(data, bytes) or len(data) > MAX_ARCHIVE_BYTES
            or not valid_digest(expected_sha256) or sha256(data) != expected_sha256):
        raise WorkspaceError('Source archive SHA256 or size mismatch')
    members, seen, total = {}, set(), 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as tar:
            for member in tar:
                name = member.name
                portable_path(name)
                if (member.type != tarfile.REGTYPE or member.linkname or member.pax_headers
                        or member.sparse is not None or member.mode != 0o644
                        or name.lower() in seen or len(members) >= MAX_FILES + 1
                        or name != MANIFEST and not allowed_source(name)):
                    raise WorkspaceError('Archive contains an unsupported or duplicate member')
                cap = MAX_MANIFEST_BYTES if name == MANIFEST else MAX_FILE_BYTES
                if not 0 <= member.size <= cap:
                    raise WorkspaceError('Archive member exceeds byte limit')
                seen.add(name.lower())
                total += member.size
                if total > MAX_SOURCE_BYTES + MAX_MANIFEST_BYTES:
                    raise WorkspaceError('Archive contents exceed total byte limit')
                with tar.extractfile(member) as stream:
                    payload = stream.read(cap + 1)
                if len(payload) != member.size:
                    raise WorkspaceError('Archive member size mismatch')
                members[name] = payload
    except (tarfile.TarError, OSError, EOFError):
        raise WorkspaceError('Invalid uncompressed source archive') from None
    raw_manifest = members.pop(MANIFEST, None)
    if raw_manifest is None:
        raise WorkspaceError('Embedded source manifest missing')
    manifest = strict_json(raw_manifest)
    if (not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'policy', 'files', 'total_bytes', 'fixture_exceptions'}
            or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1
            or manifest['policy'] != POLICY or not isinstance(manifest['files'], list)
            or not 1 <= len(manifest['files']) <= MAX_FILES or type(manifest['total_bytes']) is not int):
        raise WorkspaceError('Unsupported source manifest')
    exceptions = exceptions_map(manifest['fixture_exceptions'])
    for item in manifest['files']:
        if (not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256'}
                or not isinstance(item['path'], str) or type(item['size']) is not int
                or not valid_digest(item['sha256'])):
            raise WorkspaceError('Invalid typed source manifest entry')
    expected = []
    for name, payload in sorted(members.items()):
        validate_content(name, payload, exceptions)
        expected.append(dict(path=name, size=len(payload), sha256=sha256(payload)))
    if (manifest['files'] != expected or manifest['total_bytes'] != sum(map(len, members.values()))
            or manifest['total_bytes'] > MAX_SOURCE_BYTES or set(exceptions) - set(members)):
        raise WorkspaceError('Source manifest does not match archive contents')
    return members, raw_manifest


def real_directory(path, *, create=False):
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise WorkspaceError('Workspace parents must be real directories')
    if hasattr(os, 'getuid') and info.st_uid != os.getuid():
        raise WorkspaceError('Workspace directories must belong to the current user')


def restore_archive(data, snapshot_sha256, generation, *, home=None):
    object_url(snapshot_sha256, generation)
    files, manifest = inspect_archive(data, snapshot_sha256)
    home = Path(home) if home is not None else Path.home()
    if not home.is_absolute() or home.resolve() != home:
        raise WorkspaceError('HOME must be an absolute real directory')
    real_directory(home)
    parent = home
    for component in ('myhero', 'workspaces'):
        parent = parent / component
        real_directory(parent, create=True)
    target = parent / snapshot_sha256
    target.mkdir(mode=0o700, exist_ok=False)
    # No extractall, path rewriting, chmod from archive, links, or executable hooks.
    # An interrupted restore remains a partial directory; reruns refuse overwrite.
    for name, payload in [(MANIFEST, manifest), *sorted(files.items())]:
        destination = target.joinpath(*PurePosixPath(name).parts)
        directory = target
        for component in PurePosixPath(name).parts[:-1]:
            directory = directory / component
            real_directory(directory, create=True)
        with destination.open('xb') as handle:
            handle.write(payload)
    with (target / ORIGIN).open('xb') as handle:
        handle.write(canonical(dict(schema_version=1, policy=POLICY,
            source_url=object_url(snapshot_sha256, generation), snapshot_sha256=snapshot_sha256,
            source_file_count=len(files), model_calls=0, services_started=0)))
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    package = commands.add_parser('package')
    package.add_argument('--source', required=True)
    package.add_argument('--output', required=True)
    package.add_argument('--fixture-exceptions')
    restore = commands.add_parser('restore')
    restore.add_argument('--snapshot-sha256', required=True)
    restore.add_argument('--generation', required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == 'package':
            exceptions = strict_json(regular_bytes(Path(args.fixture_exceptions), MAX_MANIFEST_BYTES)) if args.fixture_exceptions else []
            print(json.dumps(package_snapshot(args.source, args.output, fixture_exceptions=exceptions)))
        else:
            data = download_archive(args.snapshot_sha256, args.generation)
            target = restore_archive(data, args.snapshot_sha256, args.generation)
            print(json.dumps(dict(status='ready', workspace=str(target),
                next_step='In Cloud Shell Editor, select File > Open Folder and open this workspace.')))
        return 0
    except (WorkspaceError, OSError, ValueError):
        print(json.dumps(dict(status='stopped', reason='Source verification or workspace operation failed; no project code was run.')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
