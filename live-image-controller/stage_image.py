"""Stage /opt/runcrew/app exactly as a live-image-controller Dockerfile's COPY lines do.

Run the image's own test steps against the staged tree BEFORE a Cloud Build,
so a test that reaches outside the image fails here, not in the build
(2026-09-24: build 8d08a2eb failed on a test that read agent-hub/tests/).

    python live-image-controller/stage_image.py . Dockerfile.broker <out>
    cd <out>/opt/runcrew/app && python -I -B -m unittest discover -s tests -t . -p test_fleet_controller.py
"""
import shutil
import sys
from pathlib import Path

repo, dockerfile, root = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
text = (repo / 'live-image-controller' / dockerfile).read_text(encoding='utf-8')
text = text.replace(chr(92) + chr(10), ' ')  # join backslash-newline continuations
count = 0
for line in text.splitlines():
    words = line.split()
    if len(words) < 3 or words[0] != 'COPY' or not words[1].startswith('--chown'):
        continue
    *sources, dest = words[2:]
    target = root / dest.lstrip('/')
    target.mkdir(parents=True, exist_ok=True)
    for src in sources:
        shutil.copy(repo / src, target / Path(src).name)
        count += 1
print(dockerfile, 'staged', count, 'files')
