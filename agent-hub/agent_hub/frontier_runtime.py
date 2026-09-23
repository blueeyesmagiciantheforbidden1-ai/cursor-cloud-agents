"""Cloud runtime for the reviewed finite Ryan Frontier prototype.

Only the immutable bundled closed grammar is executed. This is not a sandbox
or evaluator for arbitrary repository patches. Durable authority lives in the
separate Firestore database; per-run SQLite files are archived experiment data.
"""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PROJECT = 'project-0c6d31fa-509e-4116-a2c'
DATABASE = 'runcrew-frontier'
BUCKET = 'runcrew-496481413971-source'
ARCHIVE_SHA = '87bd79de9d2dd26040fd150df26dc24de3703bf6341843ef0d4e3fa49a81c5a1'
PROVENANCE_SHA = '05bce4c43a3dd8cc8b6cc0a087273bea9a5c1efc7f166ee896e556fc6ac5f5e8'
VENDOR = Path(__file__).resolve().parents[1] / 'vendor' / 'ryan-frontier'
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_FILES = 1024
SUMMARY_FIELDS = ('seed', 'units', 'budget', 'runtime', 'confirmation', 'promotion_status',
    'active_release', 'model_calls', 'api_spend_microdollars', 'factorial', 'fixed_baseline',
    'lifecycle_cost', 'report_sha256', 'candidate_sha256', 'evaluator_sha256', 'pipeline_sha256',
    'receipt_sha256', 'elapsed_seconds')


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate research JSON member')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite research JSON')))


def _read_regular(path, root, limit):
    path, root = Path(path), Path(root)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Research path escapes its root')
    cursor = path
    while True:
        if cursor.is_symlink() or getattr(cursor, 'is_junction', lambda: False)():
            raise ValueError('Research links are forbidden')
        if cursor == root:
            break
        if cursor.parent == cursor:
            raise ValueError('Research path has no trusted root')
        cursor = cursor.parent
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise ValueError('Research file is not regular or exceeds limit')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit or len(data) != info.st_size:
        raise ValueError('Research file changed or exceeds limit')
    return data


def verify_vendor(root=None):
    root = Path(root if root is not None else VENDOR).absolute()
    raw = _read_regular(root / 'IMPORT_PROVENANCE.json', root, 128_000)
    if digest(raw) != PROVENANCE_SHA:
        raise ValueError('Research provenance identity mismatch')
    provenance = _json(raw)
    if provenance['archive_sha256'] != ARCHIVE_SHA:
        raise ValueError('Research archive identity mismatch')
    expected = {i['path']: i['sha256'] for i in provenance['extracted'] if i['path'].startswith('ryan_frontier/')}
    actual = {p.relative_to(root).as_posix() for p in (root / 'ryan_frontier').rglob('*')}
    if not expected or set(expected) != actual:
        raise ValueError('Research source coverage mismatch')
    for relative, sha in expected.items():
        path = root / relative
        if digest(_read_regular(path, root, 1_000_000)) != sha:
            raise ValueError('Research source identity mismatch')
    return {Path(relative).name: sha for relative, sha in expected.items()}


class OutputFiles:
    """Bound the whole stopped child's output before reading any artifact graph."""
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.files, self.cache = {}, {}
        size = entries = 0
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError('Invalid research output root')
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            for name in (*dirs, *files):
                path = Path(directory) / name
                entries += 1
                if entries > MAX_FILES or path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                    raise ValueError('Research output links or entry limit')
                if name in dirs:
                    continue
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('Research output must contain regular files')
                size += info.st_size
                if size > MAX_ARCHIVE:
                    raise ValueError('Research output exceeds archive limit')
                self.files[path.relative_to(self.root).as_posix()] = info.st_size
        self.references = set()

    def read(self, relative, limit=MAX_ARCHIVE):
        if relative not in self.files or self.files[relative] > limit:
            raise ValueError('Research artifact missing or exceeds limit')
        if relative not in self.cache:
            data = _read_regular(self.root / relative, self.root, limit)
            if len(data) != self.files[relative]:
                raise ValueError('Research output changed after inventory')
            self.cache[relative] = data
        return self.cache[relative]

    def object(self, sha):
        if type(sha) is not str or not re.fullmatch('[a-f0-9]{64}', sha):
            raise ValueError('Missing research artifact identity')
        data = self.read('objects/' + sha)
        if digest(data) != sha:
            raise ValueError('Research artifact digest mismatch')
        self.references.add('objects/' + sha)
        return data

    def document(self, sha):
        return _json(self.object(sha))

    def names(self, prefix):
        return {name.removeprefix(prefix) for name in self.files if name.startswith(prefix)}


def child_limits():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
    resource.setrlimit(resource.RLIMIT_AS, (320 * 1024 * 1024, 320 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_ARCHIVE, MAX_ARCHIVE))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def run_child(parameters, output):
    verify_vendor()
    command = [sys.executable, '-B', '-m', 'agent_hub.frontier_runtime', '--child', str(output),
        str(parameters['seed']), str(parameters['units']), str(parameters['budget'])]
    # The child receives no Google or model credential environment. It contains
    # trusted finite research code, not arbitrary candidate code.
    environment = {'PATH': os.defpath, 'PYTHONPATH': os.pathsep.join((str(VENDOR.parent.parent), str(VENDOR))), 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONUNBUFFERED': '1', 'HOME': str(output.parent), 'LANG': 'C.UTF-8'}
    child = subprocess.Popen(command, cwd=VENDOR, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        code = child.wait(timeout=30)
        if code:
            raise RuntimeError('Finite research process failed')
    finally:
        if child.poll() is None:
            if os.name == 'posix':
                os.killpg(child.pid, signal.SIGKILL)
            else:
                child.kill()
            child.wait(timeout=5)


def validate_output(output, parameters):
    sources = verify_vendor()
    files = OutputFiles(output)
    data = files.read('summary.json', 48_000)
    summary = _json(data)
    requested = {k: parameters[k] for k in ('seed', 'units', 'budget')}
    if any(type(summary.get(k)) is not int or summary[k] != requested[k] for k in requested):
        raise ValueError('Research parameter mismatch')
    if (type(summary.get('model_calls')) is not int or summary['model_calls'] != 0
            or type(summary.get('api_spend_microdollars')) is not int or summary['api_spend_microdollars'] != 0
            or 'active_release' not in summary or summary['active_release'] is not None
            or summary.get('confirmation') not in ('inconclusive', 'evidence_passed_manual_review_required')
            or summary.get('promotion_status') not in ('inconclusive', 'review_required')):
        raise ValueError('Research exceeded offline or promotion boundary')
    for field in ('run_id', 'task_id', 'proposal_id'):
        if type(summary.get(field)) is not str or not re.fullmatch('[a-f0-9]{32}', summary[field]):
            raise ValueError('Invalid research run identity')
    run_id = summary['run_id']
    run = 'runs/' + run_id + '/'
    if files.read(run + 'summary.json', 48_000) != data:
        raise ValueError('Research summary is not the immutable run summary')
    if any(not name.startswith(run) for name in files.files if name.startswith('runs/')):
        raise ValueError('Unexpected additional research run')
    report = files.object(summary.get('report_sha256'))
    receipt_bytes = files.object(summary.get('receipt_sha256'))
    if (files.read(run + 'report.json') != report or files.read('report.json') != report
            or files.read(run + 'receipt.json') != receipt_bytes):
        raise ValueError('Research report or receipt differs from committed artifacts')
    receipt = _json(receipt_bytes)
    bound = ('run_id', 'task_id', 'proposal_id', 'report_sha256', 'candidate_sha256',
             'evaluator_sha256', 'pipeline_sha256', 'frozen_methods_sha256', 'generated_artifacts')
    if set(receipt) != set(bound) | {'schema_version', 'model_calls', 'api_spend_microdollars'} or type(receipt.get('schema_version')) is not int or receipt['schema_version'] != 1:
        raise ValueError('Research receipt schema mismatch')
    for field in bound:
        if receipt.get(field) != summary[field]:
            raise ValueError('Research receipt binding mismatch')
    if any(type(receipt[k]) is not int or receipt[k] != 0 for k in ('model_calls', 'api_spend_microdollars')):
        raise ValueError('Research receipt contains unexpected model usage')

    # Import only the pinned immutable evaluator/compiler, never generated code.
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    from ryan_frontier import __version__, domain, research
    from ryan_frontier.cli import _confirmation_gate, json_bytes
    runtime = {'python': platform.python_version(), 'implementation': platform.python_implementation(),
               'package_version': __version__, 'third_party_dependencies': []}
    if summary.get('runtime') != runtime or summary.get('version') != __version__:
        raise ValueError('Research runtime provenance mismatch')
    pipeline = files.document(summary.get('pipeline_sha256'))
    expected_pipeline = {'schema_version': 1, 'sources': sources, 'runtime': runtime, 'parameters': requested}
    if pipeline != expected_pipeline:
        raise ValueError('Research pipeline source or parameter mismatch')
    for name, sha in sources.items():
        # Hash comparison is against the independently pinned vendor manifest.
        if digest(files.object(sha)) != sha:
            raise ValueError('Research source object mismatch')

    parsed_report = _json(report)
    design = parsed_report['design']
    design_digest = research._hash(design)
    if (parsed_report.get('design_digest') != design_digest or type(design.get('seed')) is not int
            or design['seed'] != requested['seed'] or type(design.get('independent_lineages')) is not int
            or design['independent_lineages'] != requested['units']
            or type(design.get('candidate_budget_per_task_arm')) is not int
            or design['candidate_budget_per_task_arm'] != requested['budget'] or design.get('auto_promote') is not False):
        raise ValueError('Research design identity or parameters mismatch')
    evaluator = files.document(summary.get('evaluator_sha256'))
    if evaluator != {'schema_version': 1, 'pipeline_sha256': summary['pipeline_sha256'],
                     'design': design, 'design_digest': design_digest}:
        raise ValueError('Research evaluator binding mismatch')
    frozen = files.document(summary.get('frozen_methods_sha256'))
    if type(frozen) is not list or frozen != parsed_report.get('frozen_methods') or len(frozen) != requested['units']:
        raise ValueError('Research frozen lineage mismatch')

    artifacts = parsed_report.get('artifacts')
    generated = summary.get('generated_artifacts')
    if type(artifacts) is not dict or not artifacts or len(artifacts) > MAX_FILES // 4 or type(generated) is not dict:
        raise ValueError('Research generated artifact coverage mismatch')
    expected_names = {identifier + suffix for identifier in artifacts for suffix in ('.py', '.json')}
    if set(generated) != expected_names or files.names(run + 'generated/') != expected_names or files.names('generated/') != expected_names:
        raise ValueError('Research generated artifact coverage mismatch')
    for identifier, artifact in artifacts.items():
        if type(identifier) is not str or not re.fullmatch('(method|candidate)_[a-f0-9]{16}', identifier):
            raise ValueError('Invalid generated research artifact name')
        spec = artifact['spec']
        if artifact.get('kind') == 'search_method':
            method = research.ResearchMethod.from_dict(spec)
            if method.to_dict() != spec or identifier != 'method_' + method.digest[:16]:
                raise ValueError('Research method specification mismatch')
            source = research.compile_method(method)
        elif artifact.get('kind') == 'workflow_candidate':
            if identifier != 'candidate_' + research._hash(spec)[:16]:
                raise ValueError('Research candidate specification mismatch')
            source = domain.compile_candidate(spec)
        else:
            raise ValueError('Unknown research artifact kind')
        if artifact.get('source') != source or artifact.get('sha256') != digest(source.encode()):
            raise ValueError('Research compiled source differs from specification')
        for suffix, content in (('.py', source.encode()), ('.json', json_bytes(spec))):
            name = identifier + suffix
            if (files.object(generated[name]) != content or files.read(run + 'generated/' + name) != content
                    or files.read('generated/' + name) != content):
                raise ValueError('Research generated bytes differ from committed specification')
    manifest = {key: {k: v for k, v in artifact.items() if k != 'source'} for key, artifact in artifacts.items()}
    if parsed_report.get('artifact_manifest') != manifest:
        raise ValueError('Research artifact manifest mismatch')

    executable = {}
    lineages = []
    for entry in frozen:
        identifier = entry['artifact_id']
        if (type(entry.get('lineage')) is not int or identifier not in artifacts
                or artifacts[identifier].get('kind') != 'search_method'
                or entry.get('spec') != artifacts[identifier]['spec']
                or entry.get('method_digest') != research._hash(entry['spec'])
                or entry.get('freeze_digest') != research._hash(entry['freeze_record'])
                or entry['freeze_record'].get('method_digest') != entry['method_digest']):
            raise ValueError('Research frozen method specification mismatch')
        lineages.append(entry['lineage'])
        executable[identifier] = {'source_sha256': generated[identifier + '.py'], 'spec_sha256': generated[identifier + '.json']}
    if sorted(lineages) != list(range(requested['units'])):
        raise ValueError('Research lineage coverage mismatch')
    candidate = files.document(summary.get('candidate_sha256'))
    if candidate != {'schema_version': 1, 'kind': 'frozen_lineage_policy_bundle',
                     'frozen_methods_sha256': summary['frozen_methods_sha256'], 'executable_artifacts': executable}:
        raise ValueError('Research candidate bundle binding mismatch')
    if files.names('objects/') != {name.removeprefix('objects/') for name in files.references}:
        raise ValueError('Research object graph contains unbound objects')

    # Recompute the declared gate, without claiming to rerun scientific evaluation.
    _confirmation_gate(parsed_report)
    confirmation = parsed_report['confirmation']['decision']
    expected_status = 'review_required' if confirmation == 'evidence_passed_manual_review_required' else 'inconclusive'
    if confirmation != summary['confirmation'] or summary['promotion_status'] != expected_status:
        raise ValueError('Summary decision differs from evaluator evidence')
    factorial = {condition: {arm: values['mean_solved_fraction'] for arm, values in arms.items()}
                 for condition, arms in parsed_report['factorial']['summary'].items()}
    if (summary.get('factorial') != factorial or summary.get('lifecycle_cost') != parsed_report['lifecycle_cost']
            or summary.get('fixed_baseline') != parsed_report.get('fixed_baseline', {}).get('summary')
            or type(summary.get('elapsed_seconds')) not in (int, float)
            or not math.isfinite(summary['elapsed_seconds']) or summary['elapsed_seconds'] < 0):
        raise ValueError('Research summary metrics differ from committed report')
    result = {key: summary[key] for key in SUMMARY_FIELDS if key in summary}
    if len(encoded(result)) > 48000:
        raise ValueError('Research result exceeds limit')
    return result


def pack_output(root):
    files = []
    size = 0
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Research output links are forbidden')
        if path.is_dir():
            continue
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Invalid research output file')
        size += path.stat().st_size
        files.append(path)
        if len(files) > MAX_FILES or size > MAX_ARCHIVE:
            raise ValueError('Research output exceeds archive limit')
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz', format=tarfile.PAX_FORMAT) as archive:
        for path in files:
            info = tarfile.TarInfo(path.relative_to(root).as_posix())
            content = path.read_bytes()
            info.size, info.mode, info.mtime = len(content), 0o600, 0
            archive.addfile(info, io.BytesIO(content))
    payload = buffer.getvalue()
    if len(payload) > MAX_ARCHIVE:
        raise ValueError('Research compressed archive exceeds limit')
    return payload


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def upload_artifact(request_id, data):
    # Create-only content identity. No update/delete permission and no retries.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    request = Request('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',
                      headers={'Metadata-Flavor': 'Google'})
    with opener.open(request, timeout=5) as response:
        if response.headers.get('Metadata-Flavor') != 'Google':
            raise ValueError('Missing cloud metadata identity header')
        raw = response.read(16385)
    if len(raw) > 16384:
        raise ValueError('Cloud identity exceeds limit')
    token = json.loads(raw)['access_token']
    sha = digest(data)
    name = 'frontier/runs/' + request_id + '/' + sha + '.tar.gz'
    url = ('https://storage.googleapis.com/upload/storage/v1/b/' + BUCKET + '/o?uploadType=media'
           '&ifGenerationMatch=0&name=' + quote(name, safe=''))
    request = Request(url, data=data, headers={'Authorization': 'Bearer ' + token,
                                             'Content-Type': 'application/gzip'}, method='POST')
    with opener.open(request, timeout=10) as response:
        raw = response.read(32001)
    if len(raw) > 32000:
        raise ValueError('Cloud artifact receipt exceeds limit')
    value = json.loads(raw)
    if (value.get('bucket') != BUCKET or value.get('name') != name or value.get('size') != str(len(data))
            or not re.fullmatch('[1-9][0-9]{0,19}', str(value.get('generation', '')))):
        raise ValueError('Cloud artifact receipt mismatch')
    import base64
    if value.get('md5Hash') != base64.b64encode(hashlib.md5(data).digest()).decode():
        raise ValueError('Cloud artifact checksum mismatch')
    return {'bucket': BUCKET, 'object': name, 'generation': str(value['generation']), 'sha256': sha, 'bytes': len(data)}


def run_research(parameters, request_id):
    with tempfile.TemporaryDirectory(prefix='frontier-') as temporary:
        output = Path(temporary) / 'run'
        run_child(parameters, output)
        summary = validate_output(output, parameters)
        payload = pack_output(output)
        artifact = upload_artifact(request_id, payload)
    return {'schema_version': 1, 'parameters': parameters, 'source_archive_sha256': ARCHIVE_SHA,
            'summary': summary, 'artifact': artifact}


def make_handler(service):
    from .frontier_service import FrontierError
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def reply(self, status, result):
            body = encoded(result)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(200 if self.path == '/healthz' else 404,
                {'service': 'myhero-frontier', 'scope': 'finite_offline_research'} if self.path == '/healthz' else {'error': 'not_found'})

        def do_POST(self):
            try:
                self.connection.settimeout(8)
                lengths = self.headers.get_all('Content-Length', [])
                if (len(lengths) != 1 or not re.fullmatch('[0-9]{1,4}', lengths[0])
                        or not 0 < int(lengths[0]) <= 4096 or self.headers.get('Transfer-Encoding')
                        or self.headers.get_content_type() != 'application/json'):
                    return self.reply(400, {'error': 'invalid_request'})
                raw = self.rfile.read(int(lengths[0]))
                if len(raw) != int(lengths[0]):
                    return self.reply(400, {'error': 'incomplete_request'})
                def pairs(values):
                    value = {}
                    for key, item in values:
                        if key in value:
                            raise ValueError()
                        value[key] = item
                    return value
                data = json.loads(raw, object_pairs_hook=pairs,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if self.path == '/v1/research/run':
                    result = service.run(data)
                elif self.path == '/v1/research/get':
                    result = service.get(data)
                else:
                    return self.reply(404, {'error': 'not_found'})
                self.reply(200, result)
            except FrontierError as error:
                self.reply(error.status, {'error': error.code})
            except (ValueError, UnicodeError, RecursionError):
                self.reply(400, {'error': 'invalid_request'})
            except Exception:
                self.reply(503, {'error': 'research_state_requires_inspection'})
    return Handler


def main():
    if not os.environ.get('K_SERVICE') or os.environ.get('GOOGLE_CLOUD_PROJECT') != PROJECT:
        raise ValueError('Research runtime requires its configured cloud project')
    verify_vendor()
    from .frontier_service import ResearchService
    from .store import FirestoreStore
    service = ResearchService(FirestoreStore(PROJECT, collection='frontier', database=DATABASE),
                              runner=run_research, artifact_bucket=BUCKET)
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), make_handler(service))
    server.daemon_threads = True
    server.serve_forever()


if __name__ == '__main__':
    if len(sys.argv) == 6 and sys.argv[1] == '--child':
        if os.name != 'posix':
            raise ValueError('Finite research execution requires Linux/POSIX')
        child_limits()
        verify_vendor()
        output, seed, units, budget = Path(sys.argv[2]), *map(int, sys.argv[3:])
        if not 0 <= seed <= 2147483647 or not 1 <= units <= 8 or not 1 <= budget <= 8:
            raise ValueError('Research parameters exceed fixed runtime limits')
        sys.path.insert(0, str(VENDOR))
        from ryan_frontier.cli import execute
        execute(output, seed=seed, units=units, budget=budget)
    else:
        main()
