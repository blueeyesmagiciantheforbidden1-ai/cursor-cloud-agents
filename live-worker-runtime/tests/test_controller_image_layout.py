"""Check the controller COPY layouts without Docker or workspace imports."""
import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def missing_imports(app):
    """Follow local absolute imports, including sources absent from the image."""
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
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                if node.module:
                    names.add(node.module)
                    names.update(f'{node.module}.{alias.name}' for alias in node.names)
        for name in sorted(names):
            top = name.split('.')[0]
            candidates = [(runtime, Path(f'{top}.py')),
                          (runtime, Path(top) / '__init__.py')]
            if name.startswith('agent_hub.'):
                module = name.split('.')[1]
                candidates.append((hub, Path('agent_hub') / f'{module}.py'))
            for root, relative in candidates:
                original = root / relative
                if original.is_file() and not (app / relative).is_file():
                    missing.add(f'{importer.as_posix()} -> {relative.as_posix()}')
                    pending.append((original, relative))
    return sorted(missing)


class ControllerImageLayoutTests(unittest.TestCase):
    def test_controller_image_layout(self):
        if Path('/opt/runcrew/app').exists():
            self.skipTest('/opt/runcrew/app exists on the host and serve.py adds it to sys.path')
        layouts = {}
        for name in ('Dockerfile', 'Dockerfile.broker'):
            with self.subTest(dockerfile=name):
                with tempfile.TemporaryDirectory(dir=ROOT.parent) as directory:
                    tmp = Path(directory)
                    subprocess.run(
                        [sys.executable, '-I', '-B',
                         str(ROOT / 'live-image-controller' / 'stage_image.py'),
                         str(ROOT), name, str(tmp)],
                        check=True, capture_output=True, text=True,
                    )
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


if __name__ == '__main__':
    unittest.main()
