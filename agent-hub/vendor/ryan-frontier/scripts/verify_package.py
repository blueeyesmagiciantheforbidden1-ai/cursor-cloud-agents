"""Verify all packaged file hashes without importing package code."""
import hashlib
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'MANIFEST.sha256.json').read_text())
failures = []
for relative, expected in manifest['files'].items():
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        failures.append(relative + ': missing or invalid path')
        continue
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        failures.append(relative + ': hash mismatch')
if failures:
    print('\n'.join(failures))
    raise SystemExit(1)
print(f"Verified {len(manifest['files'])} packaged file hashes. Hashes establish identity, not authorship.")
