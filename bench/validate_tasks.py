"""Validate benchmark tasks: hidden tests fail on the starting repo and pass with the reference.

    python bench/validate_tasks.py bench/tasks            # every task
    python bench/validate_tasks.py bench/tasks/001-slug   # one task

Standard library only. Runs each check in a temporary copy, so the task stays untouched.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REQUIRED = {'id', 'category', 'difficulty', 'time_budget_s', 'expected_files', 'multi_agent_suitable'}
CATEGORIES = {'bugfix', 'feature', 'refactor', 'test-writing', 'review', 'multi-file'}


def run_tests(root, folder):
    """True when `python -m unittest discover -s <folder> -t .` passes in root."""
    result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', folder, '-t', '.'],
                            cwd=root, capture_output=True, text=True, timeout=120)
    return result.returncode == 0, (result.stdout + result.stderr)[-1500:]


def stage(task, with_reference):
    work = Path(tempfile.mkdtemp(prefix='bench-'))
    shutil.copytree(task / 'repo', work, dirs_exist_ok=True)
    hidden = work / 'tests_hidden'
    shutil.copytree(task / 'hidden_tests', hidden)
    (hidden / '__init__.py').touch()
    if with_reference:
        for file in (task / 'reference').rglob('*'):
            if file.is_file():
                target = work / file.relative_to(task / 'reference')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, target)
    return work


def check(task):
    errors = []
    meta_path = task / 'meta.json'
    if not meta_path.is_file():
        return ['missing meta.json']
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    missing = REQUIRED - set(meta)
    if missing:
        errors.append('meta.json missing ' + ', '.join(sorted(missing)))
    if meta.get('id') != task.name:
        errors.append('meta.json id must equal the directory name')
    if meta.get('category') not in CATEGORIES:
        errors.append('unknown category')
    for part in ('TASK.md', 'repo', 'hidden_tests', 'reference'):
        if not (task / part).exists():
            errors.append('missing ' + part)
    if errors:
        return errors
    if 'hidden' in (task / 'TASK.md').read_text(encoding='utf-8').lower():
        errors.append('TASK.md must not mention hidden tests')
    base = stage(task, with_reference=False)
    try:
        passed, out = run_tests(base, 'tests_hidden')
        if passed:
            errors.append('hidden tests PASS on the starting repo (the task is already solved)')
    finally:
        shutil.rmtree(base, ignore_errors=True)
    solved = stage(task, with_reference=True)
    try:
        passed, out = run_tests(solved, 'tests_hidden')
        if not passed:
            errors.append('hidden tests FAIL with the reference: ' + out[-400:])
        if (solved / 'tests').is_dir():
            passed, out = run_tests(solved, 'tests')
            if not passed:
                errors.append('visible tests FAIL with the reference: ' + out[-400:])
    finally:
        shutil.rmtree(solved, ignore_errors=True)
    return errors


def main(argv):
    target = Path(argv[1] if len(argv) > 1 else 'bench/tasks')
    tasks = [target] if (target / 'meta.json').exists() else sorted(p for p in target.iterdir() if p.is_dir())
    failures = 0
    for task in tasks:
        errors = check(task)
        print(task.name, 'OK' if not errors else 'FAIL: ' + ' | '.join(errors))
        failures += bool(errors)
    print(f'{len(tasks) - failures}/{len(tasks)} tasks valid')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
