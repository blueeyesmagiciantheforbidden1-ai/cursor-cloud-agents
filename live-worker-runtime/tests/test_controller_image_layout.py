"""Check the controller COPY layouts without Docker or workspace imports."""
import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def package_of(rel):
    path = Path(rel)
    parent = path.parent
    if str(parent) == '.':
        return ''
    return parent.as_posix().replace('/', '.')


def absolute_module(package, level, module):
    """Resolve a relative import the way importlib does."""
    if level == 0:
        return module
    if not package:
        return None
    bits = package.rsplit('.', level - 1)
    if len(bits) < level:
        return None
    base = bits[0]
    return f'{base}.{module}' if module else base


def resolve_module(root, dotted):
    """Resolve pkg.sub to pkg/sub.py or pkg/sub/__init__.py under root."""
    if not dotted:
        return None
    parts = dotted.split('.')
    file_path = root.joinpath(*parts).with_suffix('.py')
    if file_path.is_file():
        return file_path.relative_to(root), False
    init = root.joinpath(*parts, '__init__.py')
    if init.is_file():
        return init.relative_to(root), True
    return None


def expected_relative(dotted):
    return Path(*dotted.split('.')).with_suffix('.py').as_posix()


def is_excluded(dotted):
    top = dotted.split('.', 1)[0]
    if top in sys.stdlib_module_names or top == '__future__':
        return True
    return False


def missing_imports(app):
    """Follow local imports, including sources absent from the image."""
    runtime = ROOT / 'live-worker-runtime'
    hub = ROOT / 'agent-hub'
    pending = [(path, path.relative_to(app)) for path in app.rglob('*.py')]
    visited = set()
    missing = set()
    while pending:
        source, importer = pending.pop()
        if importer in visited:
            continue
        visited.add(importer)
        tree = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
        package = package_of(importer)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module == '__future__':
                    continue
                dotted = absolute_module(package, node.level, node.module)
                if dotted:
                    names.add(dotted)
                if dotted is None and node.level == 0:
                    continue
                base = dotted
                for alias in node.names:
                    if alias.name == '*':
                        continue
                    if base:
                        names.add(f'{base}.{alias.name}')
                    elif node.level > 0 and package:
                        names.add(f'{package}.{alias.name}')
        for name in sorted(names):
            if is_excluded(name):
                continue
            found_app = resolve_module(app, name)
            if found_app:
                continue
            found_runtime = resolve_module(runtime, name)
            if found_runtime:
                relative, _ = found_runtime
                missing.add(f'{importer.as_posix()} -> {relative.as_posix()}')
                pending.append((runtime / relative, relative))
                continue
            found_hub = resolve_module(hub, name)
            if found_hub:
                relative, _ = found_hub
                missing.add(f'{importer.as_posix()} -> {relative.as_posix()}')
                pending.append((hub / relative, relative))
                continue
            # Attribute of a module file vs missing submodule of a package.
            parts = name.split('.')
            reported = False
            for index in range(len(parts) - 1, 0, -1):
                parent = '.'.join(parts[:index])
                parent_app = resolve_module(app, parent)
                if parent_app is None:
                    parent_runtime = resolve_module(runtime, parent)
                    parent_hub = resolve_module(hub, parent)
                    parent_info = parent_runtime or parent_hub
                    if parent_info is None:
                        continue
                    _, is_package = parent_info
                else:
                    _, is_package = parent_app
                if is_package:
                    missing.add(
                        f'{importer.as_posix()} -> {expected_relative(name)}')
                    reported = True
                    break
                # Parent is a module file: name is an import attribute, not a submodule.
                reported = True
                break
            if reported:
                continue
            # Top-level local module missing from the image but present in sources
            # was handled above. Anything else is third-party / optional.
    return sorted(missing)


class ControllerImageLayoutTests(unittest.TestCase):
    def _stage(self, dockerfile):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        tmp = Path(directory.name)
        subprocess.run(
            [sys.executable, '-I', '-B',
             str(ROOT / 'live-image-controller' / 'stage_image.py'),
             str(ROOT), dockerfile, str(tmp)],
            check=True, capture_output=True, text=True,
        )
        return tmp

    def test_controller_image_layout(self):
        if Path('/opt/runcrew/app').exists():
            self.skipTest('/opt/runcrew/app exists on the host and serve.py adds it to sys.path')
        layouts = {}
        for name in ('Dockerfile', 'Dockerfile.broker'):
            with self.subTest(dockerfile=name):
                tmp = self._stage(name)
                app = tmp / 'opt/runcrew/app'
                layouts[name] = {path.relative_to(app).as_posix()
                                 for path in app.rglob('*')}
                with self.subTest(check='image imports'):
                    result = subprocess.run(
                        [sys.executable, '-I', '-B', 'serve.py', '--check'],
                        cwd=app, timeout=60, capture_output=True, text=True,
                    )
                    diagnostic = f'stdout:\n{result.stdout}\nstderr:\n{result.stderr}'
                    self.assertEqual(result.returncode, 0, diagnostic)
                    lines = result.stdout.splitlines()
                    self.assertTrue(lines, diagnostic)
                    try:
                        status = json.loads(lines[-1])
                    except json.JSONDecodeError:
                        self.fail(diagnostic)
                    self.assertIsInstance(status, dict, diagnostic)
                    self.assertEqual(status.get('status'), 'image_ok', diagnostic)
                with self.subTest(check='import walk'):
                    missing = missing_imports(app)
                    self.assertEqual(missing, [], '\n'.join(missing))
        with self.subTest(check='same layout'):
            self.assertEqual(layouts['Dockerfile'], layouts['Dockerfile.broker'])

    def test_missing_relative_and_submodule_imports(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = Path(directory.name)
        pkg = app / 'pkg'
        pkg.mkdir()
        (pkg / '__init__.py').write_text('', encoding='utf-8')
        (pkg / 'mod.py').write_text(
            'from . import missing_sibling\nimport pkg.absent_sub\n',
            encoding='utf-8',
        )
        missing = missing_imports(app)
        joined = '\n'.join(missing)
        self.assertTrue(
            any('missing_sibling' in item for item in missing),
            joined,
        )
        self.assertTrue(
            any('absent_sub' in item for item in missing),
            joined,
        )

    def test_staging_stays_inside_tempdir(self):
        if Path('/opt/runcrew/app').exists():
            self.skipTest('/opt/runcrew/app exists on the host and serve.py adds it to sys.path')
        parent = ROOT.parent
        before = sorted(path.name for path in parent.iterdir())
        before_dirs = {
            path.name: path.is_dir()
            for path in parent.iterdir()
        }
        tmp = self._stage('Dockerfile')
        self.assertTrue((tmp / 'opt/runcrew/app' / 'serve.py').is_file())
        after = sorted(path.name for path in parent.iterdir())
        self.assertEqual(before, after)
        for name, was_dir in before_dirs.items():
            self.assertEqual(was_dir, (parent / name).is_dir())
        # Staging target must be the tempdir, not ROOT.parent.
        self.assertFalse(str(tmp).startswith(str(parent.resolve())))
        # Or allow tempdir elsewhere; ensure nothing new under parent matches stage layout.
        self.assertFalse((parent / 'opt' / 'runcrew' / 'app').exists())


if __name__ == '__main__':
    unittest.main()
