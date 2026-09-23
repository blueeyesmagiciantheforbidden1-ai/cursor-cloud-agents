"""One-command local checks; creates no paid calls or external effects."""
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
               cwd=root, check=True)
with tempfile.TemporaryDirectory(prefix='ryan-frontier-check-') as output:
    subprocess.run([sys.executable, '-m', 'ryan_frontier', 'demo', '--output', output],
                   cwd=root, check=True)
subprocess.run([sys.executable, '-m', 'ryan_frontier', 'route', '--config',
                'examples/providers.example.json'], cwd=root, check=True)
print('Local tests and integration checks passed. No provider call was made.')
