"""Build worker image packs from a synthetic base and check the refresh contract.

Pack refresh is local file copying only. These tests do not call Google Cloud,
Firestore, Cloud Run, or the hub.
"""
import ast
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'live-image-workers'))
import refresh_packs


BASE = 'live-20260922b'
VERSION = 'live-20260923a'
EMAIL = 'pack-owner@example.invalid'
PLACEHOLDER = refresh_packs.PLACEHOLDER
RUNTIME = refresh_packs.RUNTIME
STALE = 'STALE BASE COPY\n'
STUB = '# kept from the base pack\n'
DECOYS = ('Dockerfile', 'agent_hub/marker.py', 'live/kept_from_base.txt')


def is_stdlib(dotted):
    return dotted.split('.', 1)[0] in sys.stdlib_module_names


def absolute_module(package, level, module):
    """Resolve a relative import the way importlib does."""
    if level == 0:
        return module
    bits = package.rsplit('.', level - 1)
    if len(bits) < level:
        raise AssertionError('relative import beyond top-level package')
    base = bits[0]
    return f'{base}.{module}' if module else base


def resolve_checkout(dotted):
    parts = dotted.split('.')
    file_path = RUNTIME.joinpath(*parts).with_suffix('.py')
    if file_path.is_file():
        return file_path.relative_to(RUNTIME).as_posix()
    init = RUNTIME.joinpath(*parts, '__init__.py')
    if init.is_file():
        return init.relative_to(RUNTIME).as_posix()
    return None


def package_of(rel):
    path = Path(rel)
    if path.name == '__init__.py':
        parent = path.parent
        return '' if str(parent) == '.' else parent.as_posix().replace('/', '.')
    parent = path.parent
    return '' if str(parent) == '.' else parent.as_posix().replace('/', '.')


def parent_init(rel):
    parent = Path(rel).parent
    if str(parent) == '.':
        return None
    init = parent / '__init__.py'
    if init.as_posix() == rel or not (RUNTIME / init).is_file():
        return None
    return init.as_posix()


def dynamic_py_names(tree):
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != 'spec_from_file_location':
            continue
        args = list(node.args) + [keyword.value for keyword in node.keywords]
        for arg in args:
            for const in ast.walk(arg):
                if isinstance(const, ast.Constant) and isinstance(const.value, str) and const.value.endswith('.py'):
                    names.append(const.value)
    return names


def add_external(dotted, submodule_names, external):
    parts = dotted.split('.')
    root = [] if parts[0] == 'agent_hub' else ['live']
    if submodule_names:
        external.add('/'.join(root + parts) + '/__init__.py')
        for name in submodule_names:
            add_external('.'.join(parts + [name]), (), external)
        return
    for index in range(1, len(parts)):
        external.add('/'.join(root + parts[:index]) + '/__init__.py')
    external.add('/'.join(root + parts) + '.py')


def note(rel, checkout, pending):
    if rel in checkout:
        return
    checkout.add(rel)
    init = parent_init(rel)
    if init:
        note(init, checkout, pending)
    pending.append(rel)


def closure(provider):
    """Checkout-relative and pack-relative modules imported by the live loop and provider.

    Checkout paths are relative to live/. Pack paths for image-only modules and
    agent_hub point at files the base pack must already contain.
    """
    checkout, external, pending = set(), set(), []
    note('live_loop.py', checkout, pending)
    note(f'providers/{provider}.py', checkout, pending)
    seen = set()
    while pending:
        rel = pending.pop()
        if rel in seen:
            continue
        seen.add(rel)
        tree = ast.parse((RUNTIME / rel).read_text(encoding='utf-8'))
        package = package_of(rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                specs = [(alias.name, ()) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module == '__future__':
                    continue
                dotted = absolute_module(package, node.level, node.module)
                specs = [(dotted, tuple(alias.name for alias in node.names))]
            else:
                continue
            for dotted, names in specs:
                if not dotted or is_stdlib(dotted):
                    continue
                if rel.startswith('cursor_native/') and dotted == 'metadata':
                    note('cursor_native/metadata.py', checkout, pending)
                    continue
                resolved = resolve_checkout(dotted)
                if resolved:
                    note(resolved, checkout, pending)
                    if names and resolved.endswith('/__init__.py'):
                        for name in names:
                            child = resolve_checkout(dotted + '.' + name)
                            if child:
                                note(child, checkout, pending)
                            else:
                                add_external(dotted + '.' + name, (), external)
                    continue
                submodules = names if names and '.' not in dotted and dotted == 'agent_hub' else ()
                add_external(dotted, submodules, external)
        if rel == f'providers/{provider}.py':
            for name in dynamic_py_names(tree):
                note(f'cursor_native/{name}', checkout, pending)
    return checkout, external


def packaged(provider):
    copied = set(refresh_packs.live_files(provider))
    if provider == 'cursor':
        native = RUNTIME / 'cursor_native'
        copied |= {f'cursor_native/{path.relative_to(native).as_posix()}'
                   for path in native.rglob('*.py')}
    return copied


def cloud_build(provider):
    image = f'us-central1-docker.pkg.dev/example/runcrew-hub/{provider}-worker:{BASE}'
    config = {
        'images': [image],
        'steps': [{'name': 'gcr.io/cloud-builders/docker', 'args': ['build', '-t', image, '.']}],
    }
    text = json.dumps(config, separators=(',', ':'))
    if text.count(':' + BASE) != 2:
        raise AssertionError('synthetic cloudbuild.json must carry two base tags')
    return text + '\n'


def write_base(packs, provider, external):
    base = packs / f'live-image-{provider}-{BASE}-source'
    (base / 'live').mkdir(parents=True)
    (base / 'agent_hub').mkdir()
    (base / 'Dockerfile').write_text(
        'FROM example.invalid/worker-base\n# pin ' + PLACEHOLDER + ' stays in the base image\n',
        encoding='utf-8', newline='\n')
    (base / 'cloudbuild.json').write_text(cloud_build(provider), encoding='utf-8', newline='\n')
    (base / 'agent_hub' / 'marker.py').write_text(
        'MARKER = ' + repr(PLACEHOLDER) + '\n', encoding='utf-8', newline='\n')
    (base / 'live' / 'kept_from_base.txt').write_text(PLACEHOLDER + '\n', encoding='utf-8', newline='\n')
    (base / 'live' / 'credential_state.py').write_text(STUB, encoding='utf-8', newline='\n')
    for rel in external:
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STUB, encoding='utf-8', newline='\n')
    for rel in packaged(provider):
        path = base / 'live' / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STALE, encoding='utf-8', newline='\n')
    return base


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def files_containing(root, needle):
    raw = needle.encode()
    return {path.relative_to(root).as_posix()
            for path in root.rglob('*') if path.is_file() and raw in path.read_bytes()}


class RefreshPacksTest(unittest.TestCase):
    def setUp(self):
        # The synthetic base packs below are not git content; the drift guard
        # has its own tests in BaseDriftTest.
        patcher = mock.patch.object(refresh_packs, '_in_git', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_built_packs_keep_every_import_pin_and_tag(self):
        closures = {}
        for provider in refresh_packs.PROVIDERS:
            checkout, external = closure(provider)
            missing = checkout - packaged(provider)
            self.assertFalse(missing, f'{provider} imports {sorted(missing)} which refresh does not copy')
            closures[provider] = (checkout, external)

        with tempfile.TemporaryDirectory() as tmp:
            packs = Path(tmp) / 'packs'
            out = Path(tmp) / 'out'
            bases = {provider: write_base(packs, provider, external) for provider, (_, external) in closures.items()}
            before = {provider: snapshot(base) for provider, base in bases.items()}
            status = refresh_packs.main([
                '--packs', str(packs), '--base-version', BASE, '--version', VERSION,
                '--owner-email', EMAIL, '--out', str(out),
            ])
            self.assertEqual(status, 0)
            for provider, base in bases.items():
                self.assertEqual(snapshot(base), before[provider])

            for provider, (checkout, external) in closures.items():
                pack = out / f'live-image-{provider}-{VERSION}-source'
                base = bases[provider]
                with self.subTest(provider=provider):
                    self.assertEqual(files_containing(pack, 'STALE BASE COPY'), set())
                    for rel in checkout:
                        copied = (pack / 'live' / rel).read_text(encoding='utf-8')
                        source = (RUNTIME / rel).read_text(encoding='utf-8')
                        if rel in refresh_packs.PINNED:
                            source = source.replace(PLACEHOLDER, EMAIL)
                            self.assertNotIn(PLACEHOLDER, copied)
                        self.assertEqual(copied, source)
                    for rel, source in refresh_packs.live_files(provider).items():
                        copied = (pack / 'live' / rel).read_text(encoding='utf-8')
                        expected = source.read_text(encoding='utf-8')
                        if rel in refresh_packs.PINNED:
                            expected = expected.replace(PLACEHOLDER, EMAIL)
                        self.assertEqual(copied, expected)
                    for rel in external:
                        self.assertEqual((pack / rel).read_text(encoding='utf-8'), STUB)
                    if provider in ('claude', 'cursor'):
                        self.assertNotEqual((pack / 'live' / 'credential_state.py').read_text(encoding='utf-8'), STUB)
                    else:
                        self.assertEqual((pack / 'live' / 'credential_state.py').read_text(encoding='utf-8'), STUB)
                    pinned = {f'live/{rel}' for rel in refresh_packs.live_files(provider)
                              if rel in refresh_packs.PINNED}
                    self.assertEqual(files_containing(pack, EMAIL), pinned)
                    for rel in pinned:
                        self.assertNotIn(PLACEHOLDER, (pack / rel).read_text(encoding='utf-8'))
                    for rel in DECOYS:
                        text = (pack / rel).read_text(encoding='utf-8')
                        self.assertIn(PLACEHOLDER, text)
                        self.assertNotIn(EMAIL, text)
                    build = (pack / 'cloudbuild.json').read_text(encoding='utf-8')
                    self.assertEqual(build, cloud_build(provider).replace(':' + BASE, ':' + VERSION))
                    self.assertEqual(build.count(':' + VERSION), 2)
                    self.assertNotIn(BASE, build)
                    self.assertEqual((pack / 'Dockerfile').read_bytes(), (base / 'Dockerfile').read_bytes())
                    self.assertEqual(snapshot(pack / 'agent_hub'), snapshot(base / 'agent_hub'))

    def test_existing_target_pack_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packs = root / 'packs'
            provider = 'codex'
            _, external = closure(provider)
            base = write_base(packs, provider, external)
            out = root / 'out'
            target = out / f'live-image-{provider}-{VERSION}-source'
            target.mkdir(parents=True)
            (target / 'sentinel.txt').write_text('do not replace\n', encoding='utf-8')
            (target / 'cloudbuild.json').write_text('{"images":["untouched:live-20260922b"]}\n', encoding='utf-8')
            before_target = snapshot(target)
            before_base = snapshot(base)
            with self.assertRaises(SystemExit) as caught:
                refresh_packs.main([
                    '--packs', str(packs), '--base-version', BASE, '--version', VERSION,
                    '--owner-email', EMAIL, '--out', str(out), '--providers', provider,
                ])
            self.assertIn('refusing to overwrite', str(caught.exception))
            self.assertEqual(snapshot(target), before_target)
            self.assertEqual(snapshot(base), before_base)


class BaseDriftTest(unittest.TestCase):
    """A hand patch in the deployed pack that never reached git must stop the build.

    2026-09-23: the copilot d image was built from git on a 22b base while the
    live image came from the 22f pack, whose pre-prompt PASSIVE_EVENTS were
    never committed; every startup failed.
    """

    def make_base(self, root, files):
        base = Path(root) / 'live-image-copilot-live-20260922f-source'
        for name, data in files.items():
            path = base / 'live' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (base / 'Dockerfile').write_text('FROM scratch', encoding='utf-8')
        return base

    def git_file(self, name):
        return (refresh_packs.RUNTIME / name).read_bytes().replace(b'\r\n', b'\n')

    def test_git_content_has_no_drift_even_with_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            live_loop = self.git_file('live_loop.py')
            base = self.make_base(tmp, {'live_loop.py': live_loop.replace(b'\n', b'\r\n'),
                                        'provider_errors.py': self.git_file('provider_errors.py')})
            self.assertEqual(refresh_packs.base_drift(base, 'owner@example.org'), [])

    def test_a_hand_patch_is_named_and_refresh_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            patched = self.git_file('providers/copilot.py') + b"\nPASSIVE_EVENTS = PASSIVE_EVENTS | {'x'}\n"
            base = self.make_base(tmp, {'providers/copilot.py': patched,
                                        'live_loop.py': self.git_file('live_loop.py')})
            self.assertEqual(refresh_packs.base_drift(base, 'owner@example.org'), ['providers/copilot.py'])
            with self.assertRaises(SystemExit) as caught:
                refresh_packs.refresh('copilot', Path(tmp), 'live-20260922f', 'live-20260924a',
                                      'owner@example.org', Path(tmp) / 'out')
            self.assertIn('providers/copilot.py', str(caught.exception))
            self.assertFalse((Path(tmp) / 'out').exists())

    def test_owner_pinned_file_is_compared_with_the_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            pinned = self.git_file('providers/grok.py').replace(
                refresh_packs.PLACEHOLDER.encode(), b'owner@example.org')
            base = self.make_base(tmp, {'providers/grok.py': pinned})
            self.assertEqual(refresh_packs.base_drift(base, 'owner@example.org'), [])
            self.assertEqual(refresh_packs.base_drift(base, 'other@example.org'), ['providers/grok.py'])

    def test_per_provider_base_is_validated(self):
        with self.assertRaises(SystemExit):
            refresh_packs.main(['--packs', '.', '--base-version', 'live-20260922b', '--version', 'live-20260924a',
                                '--owner-email', 'owner@example.org', '--base', 'copilot=22f'])


if __name__ == '__main__':
    unittest.main()

