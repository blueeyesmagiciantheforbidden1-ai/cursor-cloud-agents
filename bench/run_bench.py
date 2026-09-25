"""MH-014 usefulness benchmark runner (modes A / B / C).

Standard library only. Real CLI adapters are out of scope; load them later with
``load_adapter('pkg.mod.ClassName')``.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import random
import shutil
import statistics
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Optional, Protocol, Sequence

# ---------------------------------------------------------------------------
# Adapter protocol + loader hook
# ---------------------------------------------------------------------------

ADAPTER_STATUSES = frozenset({'exited', 'timeout', 'error'})


class Adapter(Protocol):
    """Operator-pluggable agent runner.

    ``run`` receives a workspace (copy of task ``repo/``), a role prompt, and a
    timeout. It must return a dict with keys ``status``, ``seconds``, and
    ``transcript_tail``. Status is one of ``exited`` | ``timeout`` | ``error``.
    """

    def run(self, workspace: Path, prompt: str, timeout_s: int) -> Mapping[str, Any]:
        ...


def load_adapter(dotted_path: str, *args: Any, **kwargs: Any) -> Adapter:
    """Import an adapter class by dotted path and instantiate it.

    Example: ``load_adapter('my_pkg.cursor_adapter.CursorCliAdapter', model='…')``
    This is the hook for runner-cert / real CLI adapters.
    """
    module_path, _, name = dotted_path.rpartition('.')
    if not module_path:
        raise ValueError(f'adapter path must be module.Class, got {dotted_path!r}')
    mod = importlib.import_module(module_path)
    cls = getattr(mod, name)
    return cls(*args, **kwargs)


def _normalize_adapter_result(raw: Mapping[str, Any], elapsed: float) -> dict:
    status = raw.get('status', 'error')
    if status not in ADAPTER_STATUSES:
        status = 'error'
    return {
        'status': status,
        'seconds': float(raw.get('seconds', elapsed)),
        'transcript_tail': str(raw.get('transcript_tail', ''))[-4000:],
    }


def call_adapter(adapter: Adapter, workspace: Path, prompt: str, timeout_s: int) -> dict:
    """Invoke an adapter and always return a normalized result dict."""
    t0 = time.perf_counter()
    try:
        raw = adapter.run(workspace, prompt, timeout_s)
        elapsed = time.perf_counter() - t0
        if not isinstance(raw, Mapping):
            return {
                'status': 'error',
                'seconds': elapsed,
                'transcript_tail': f'adapter returned non-mapping: {type(raw)!r}',
            }
        return _normalize_adapter_result(raw, elapsed)
    except Exception as exc:  # noqa: BLE001 — surface as adapter error / intervention
        elapsed = time.perf_counter() - t0
        return {
            'status': 'error',
            'seconds': elapsed,
            'transcript_tail': f'{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}',
        }


# ---------------------------------------------------------------------------
# Fake adapters (for harness tests and dry runs)
# ---------------------------------------------------------------------------

def _apply_reference(reference_dir: Path, workspace: Path) -> list[str]:
    written = []
    if not reference_dir.is_dir():
        return written
    for src in reference_dir.rglob('*'):
        if not src.is_file():
            continue
        rel = src.relative_to(reference_dir)
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        written.append(str(rel).replace('\\', '/'))
    return written


def _assert_no_hidden(workspace: Path) -> None:
    """Trusted tests must not be visible while an adapter runs."""
    if (workspace / 'tests_hidden').exists():
        raise AssertionError('tests_hidden present in workspace during adapter run')
    # Also reject a top-level hidden_tests mirror if someone staged wrong.
    if (workspace / 'hidden_tests').exists():
        raise AssertionError('hidden_tests present in workspace during adapter run')


class PerfectAdapter:
    """Applies the task's ``reference/`` files — a correct solution."""

    def __init__(self, reference_dir: Optional[Path] = None, task_dir: Optional[Path] = None):
        if reference_dir is not None:
            self.reference_dir = Path(reference_dir)
        elif task_dir is not None:
            self.reference_dir = Path(task_dir) / 'reference'
        else:
            self.reference_dir = None

    def bind_task(self, task_dir: Path) -> 'PerfectAdapter':
        self.reference_dir = Path(task_dir) / 'reference'
        return self

    def run(self, workspace: Path, prompt: str, timeout_s: int) -> dict:
        t0 = time.perf_counter()
        _assert_no_hidden(workspace)
        role = _infer_role(prompt)
        transcript = f'PerfectAdapter role={role}\n'
        if role == 'reviewer':
            # Approve without requesting changes.
            transcript += 'APPROVED: looks correct.\n'
        elif role == 'planner':
            transcript += 'PLAN: apply the known-good reference solution.\n'
        elif role == 'tester':
            # Optional lightweight visible test; reference may already include tests.
            tests = workspace / 'tests'
            tests.mkdir(parents=True, exist_ok=True)
            probe = tests / 'test_perfect_probe.py'
            if not probe.exists():
                probe.write_text(
                    'import unittest\n\n'
                    'class TestPerfectProbe(unittest.TestCase):\n'
                    '    def test_truth(self):\n'
                    '        self.assertTrue(True)\n',
                    encoding='utf-8',
                )
                transcript += f'wrote {probe.name}\n'
            if self.reference_dir is not None:
                written = _apply_reference(self.reference_dir, workspace)
                transcript += 'applied reference: ' + ', '.join(written) + '\n'
        else:
            # implementer (default) and revision prompts
            if self.reference_dir is None:
                return {
                    'status': 'error',
                    'seconds': time.perf_counter() - t0,
                    'transcript_tail': 'PerfectAdapter has no reference_dir bound',
                }
            written = _apply_reference(self.reference_dir, workspace)
            transcript += 'applied reference: ' + ', '.join(written) + '\n'
        return {
            'status': 'exited',
            'seconds': time.perf_counter() - t0,
            'transcript_tail': transcript,
        }


class NoopAdapter:
    """Leaves the workspace unchanged."""

    def run(self, workspace: Path, prompt: str, timeout_s: int) -> dict:
        t0 = time.perf_counter()
        _assert_no_hidden(workspace)
        return {
            'status': 'exited',
            'seconds': time.perf_counter() - t0,
            'transcript_tail': 'NoopAdapter: no changes',
        }


class BreakVisibleAdapter:
    """Corrupts package code so at least one visible test fails."""

    def run(self, workspace: Path, prompt: str, timeout_s: int) -> dict:
        t0 = time.perf_counter()
        _assert_no_hidden(workspace)
        # Overwrite the first non-test .py under workspace with a stub that fails.
        targets = [
            p for p in workspace.rglob('*.py')
            if 'tests' not in p.parts and p.name != '__init__.py'
        ]
        if not targets:
            targets = [p for p in workspace.rglob('*.py') if 'tests' not in p.parts]
        if not targets:
            return {
                'status': 'error',
                'seconds': time.perf_counter() - t0,
                'transcript_tail': 'BreakVisibleAdapter: no python files to break',
            }
        target = sorted(targets)[0]
        target.write_text(
            '# Broken by BreakVisibleAdapter\n'
            'raise RuntimeError("intentionally broken by BreakVisibleAdapter")\n',
            encoding='utf-8',
        )
        return {
            'status': 'exited',
            'seconds': time.perf_counter() - t0,
            'transcript_tail': f'broke {target.relative_to(workspace)}',
        }


class ForbiddenPathAdapter:
    """Edits a path that violates the scope rule (tests_hidden)."""

    def run(self, workspace: Path, prompt: str, timeout_s: int) -> dict:
        t0 = time.perf_counter()
        # Intentionally create the forbidden tree (should never exist for agents).
        banned = workspace / 'tests_hidden'
        banned.mkdir(parents=True, exist_ok=True)
        (banned / 'cheat.py').write_text('# out of scope\n', encoding='utf-8')
        return {
            'status': 'exited',
            'seconds': time.perf_counter() - t0,
            'transcript_tail': 'wrote tests_hidden/cheat.py',
        }


class RequestChangesReviewer:
    """Reviewer fake that always requests one revision."""

    def run(self, workspace: Path, prompt: str, timeout_s: int) -> dict:
        t0 = time.perf_counter()
        _assert_no_hidden(workspace)
        return {
            'status': 'exited',
            'seconds': time.perf_counter() - t0,
            'transcript_tail': 'REQUEST_CHANGES: please double-check edge cases.',
        }


class ErrorAdapter:
    """Always returns an adapter error (forces a human intervention)."""

    def run(self, workspace: Path, prompt: str, timeout_s: int) -> dict:
        return {
            'status': 'error',
            'seconds': 0.0,
            'transcript_tail': 'ErrorAdapter: simulated failure',
        }


def _infer_role(prompt: str) -> str:
    head = prompt[:800].lower()
    if 'you are the reviewer' in head or 'role: reviewer' in head:
        return 'reviewer'
    if 'you are the planner' in head or 'role: planner' in head:
        return 'planner'
    if 'you are the tester' in head or 'role: tester' in head:
        return 'tester'
    if 'revision' in head and 'review feedback' in head:
        return 'implementer'
    return 'implementer'


# ---------------------------------------------------------------------------
# Workspace staging, scope, trusted scoring
# ---------------------------------------------------------------------------

def snapshot_files(root: Path) -> dict[str, str]:
    """Map relative POSIX paths -> sha256 for all files under root."""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        out[rel] = digest
    return out


def changed_files(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


def scope_ok(files_changed: Sequence[str]) -> bool:
    """Changed paths must stay under the repo workspace; never touch tests_hidden.

    ``meta.expected_files`` is advisory and is not enforced here.
    """
    for rel in files_changed:
        parts = Path(rel).parts
        if 'tests_hidden' in parts:
            return False
        if rel.startswith('/') or (len(parts) and parts[0] == '..'):
            return False
    return True


def stage_workspace(task_dir: Path, dest: Optional[Path] = None) -> Path:
    """Copy ``task_dir/repo`` into a fresh workspace (no hidden tests)."""
    task_dir = Path(task_dir)
    if dest is None:
        dest = Path(tempfile.mkdtemp(prefix='bench-ws-'))
    else:
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(task_dir / 'repo', dest, dirs_exist_ok=True)
    if (dest / 'tests').is_dir():
        (dest / 'tests' / '__init__.py').touch()
    return dest


def install_hidden_tests(task_dir: Path, workspace: Path) -> None:
    """Copy trusted hidden tests in *after* agents finish."""
    hidden = workspace / 'tests_hidden'
    if hidden.exists():
        shutil.rmtree(hidden)
    shutil.copytree(task_dir / 'hidden_tests', hidden)
    (hidden / '__init__.py').touch()
    if (workspace / 'tests').is_dir():
        (workspace / 'tests' / '__init__.py').touch()


def run_unittest_folder(workspace: Path, folder: str) -> dict:
    """Run ``unittest discover`` for ``folder`` with the harness Python.

    Returns counts and success flag without raising.
    """
    start = workspace / folder
    if not start.is_dir():
        return {
            'ok': True,
            'passed': 0,
            'failed': 0,
            'total': 0,
            'output': f'no {folder} directory',
        }

    # Isolate imports per run.
    stream = io.StringIO()
    loader = unittest.TestLoader()
    # Ensure workspace is on sys.path as top-level (packages import from repo root).
    inserted = False
    root_s = str(workspace)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
        inserted = True
    # Drop previously imported task modules so each score sees fresh files.
    doomed = [
        name for name in list(sys.modules)
        if name == folder or name.startswith(folder + '.')
    ]
    # Also clear likely package names under workspace (best-effort).
    for pkg in workspace.iterdir():
        if pkg.is_dir() and (pkg / '__init__.py').exists():
            for name in list(sys.modules):
                if name == pkg.name or name.startswith(pkg.name + '.'):
                    doomed.append(name)
    for name in set(doomed):
        sys.modules.pop(name, None)

    try:
        suite = loader.discover(str(start), pattern='test*.py', top_level_dir=str(workspace))
        runner = unittest.TextTestRunner(stream=stream, verbosity=1)
        result = runner.run(suite)
        failed = len(result.failures) + len(result.errors)
        total = result.testsRun
        passed = max(0, total - failed)
        return {
            'ok': result.wasSuccessful(),
            'passed': passed,
            'failed': failed,
            'total': total,
            'output': stream.getvalue()[-2000:],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            'ok': False,
            'passed': 0,
            'failed': 0,
            'total': 0,
            'output': f'{type(exc).__name__}: {exc}',
        }
    finally:
        if inserted:
            try:
                sys.path.remove(root_s)
            except ValueError:
                pass


def score_workspace(
    task_dir: Path,
    workspace: Path,
    baseline: Mapping[str, str],
    *,
    install_hidden: bool = True,
) -> dict:
    """Trusted scoring: scope check, then visible + hidden tests.

    Hidden tests are copied in only at scoring time (unless already installed
    for a re-score). Acceptance requires all hidden tests pass and scope ok.
    """
    task_dir = Path(task_dir)
    workspace = Path(workspace)

    after = snapshot_files(workspace)
    files = changed_files(baseline, after)
    # Scope is evaluated on agent changes *before* we add tests_hidden ourselves.
    agent_scope = scope_ok(files)

    if install_hidden:
        install_hidden_tests(task_dir, workspace)

    visible = run_unittest_folder(workspace, 'tests')
    hidden = run_unittest_folder(workspace, 'tests_hidden')

    accepted = bool(hidden['ok'] and hidden['total'] > 0 and agent_scope)

    return {
        'accepted': accepted,
        'scope_ok': agent_scope,
        'files_changed': files,
        'visible_pass': visible['passed'],
        'visible_total': visible['total'],
        'visible_ok': visible['ok'],
        'hidden_pass': hidden['passed'],
        'hidden_total': hidden['total'],
        'hidden_ok': hidden['ok'],
        'visible_output': visible['output'],
        'hidden_output': hidden['output'],
    }


# ---------------------------------------------------------------------------
# Modes A / B / C
# ---------------------------------------------------------------------------

REVIEW_CHANGES_MARKER = 'REQUEST_CHANGES'


def _prompt_implementer(task_md: str) -> str:
    return (
        'Role: implementer\n'
        'You are the implementer. Solve the task by editing files in this workspace.\n\n'
        f'{task_md}'
    )


def _prompt_reviewer(task_md: str, diff_text: str) -> str:
    return (
        'Role: reviewer\n'
        'You are the reviewer. Review the diff against the task. '
        f'If changes are needed, include {REVIEW_CHANGES_MARKER} in your reply; '
        'otherwise approve.\n\n'
        f'TASK:\n{task_md}\n\nDIFF:\n{diff_text}\n'
    )


def _prompt_revision(task_md: str, review_text: str) -> str:
    return (
        'Role: implementer\n'
        'Revision pass. Apply the review feedback below (one bounded revision).\n\n'
        f'REVIEW FEEDBACK:\n{review_text}\n\n'
        f'TASK:\n{task_md}\n'
    )


def _prompt_planner(task_md: str) -> str:
    return (
        'Role: planner\n'
        'You are the planner. Outline a short plan; you may leave notes in the transcript.\n\n'
        f'{task_md}'
    )


def _prompt_tester(task_md: str) -> str:
    return (
        'Role: tester\n'
        'You are the tester. Write extra tests only under tests/ in this workspace.\n\n'
        f'{task_md}'
    )


def _diff_text(baseline: Mapping[str, str], workspace: Path) -> str:
    after = snapshot_files(workspace)
    changed = changed_files(baseline, after)
    lines = [f'changed ({len(changed)}):']
    for rel in changed[:50]:
        lines.append(f'  - {rel}')
    if len(changed) > 50:
        lines.append(f'  … {len(changed) - 50} more')
    return '\n'.join(lines)


def _note_intervention(result: Mapping[str, Any]) -> bool:
    return result.get('status') in ('error', 'timeout')


class ModeOutcome(dict):
    """Result of one mode execution (before or after scoring merge)."""


def run_mode_a(
    workspace: Path,
    task_md: str,
    implementer: Adapter,
    timeout_s: int,
    baseline: Mapping[str, str],
) -> ModeOutcome:
    """A — single implementer gets TASK.md + repo."""
    t0 = time.perf_counter()
    adapter_calls = 0
    interventions = 0
    result = call_adapter(implementer, workspace, _prompt_implementer(task_md), timeout_s)
    adapter_calls += 1
    if _note_intervention(result):
        interventions += 1
    return ModeOutcome(
        mode='A',
        seconds=time.perf_counter() - t0,
        adapter_calls=adapter_calls,
        interventions=interventions,
        transcripts=[result.get('transcript_tail', '')],
    )


def run_mode_b(
    workspace: Path,
    task_md: str,
    implementer: Adapter,
    reviewer: Adapter,
    timeout_s: int,
    baseline: Mapping[str, str],
) -> ModeOutcome:
    """B — implement, then review; at most one revision if requested."""
    t0 = time.perf_counter()
    adapter_calls = 0
    interventions = 0
    transcripts: list[str] = []

    r1 = call_adapter(implementer, workspace, _prompt_implementer(task_md), timeout_s)
    adapter_calls += 1
    transcripts.append(r1.get('transcript_tail', ''))
    if _note_intervention(r1):
        interventions += 1
        return ModeOutcome(
            mode='B',
            seconds=time.perf_counter() - t0,
            adapter_calls=adapter_calls,
            interventions=interventions,
            transcripts=transcripts,
        )

    diff = _diff_text(baseline, workspace)
    r2 = call_adapter(reviewer, workspace, _prompt_reviewer(task_md, diff), timeout_s)
    adapter_calls += 1
    transcripts.append(r2.get('transcript_tail', ''))
    if _note_intervention(r2):
        interventions += 1
        return ModeOutcome(
            mode='B',
            seconds=time.perf_counter() - t0,
            adapter_calls=adapter_calls,
            interventions=interventions,
            transcripts=transcripts,
        )

    if REVIEW_CHANGES_MARKER in (r2.get('transcript_tail') or ''):
        r3 = call_adapter(
            implementer,
            workspace,
            _prompt_revision(task_md, r2.get('transcript_tail', '')),
            timeout_s,
        )
        adapter_calls += 1
        transcripts.append(r3.get('transcript_tail', ''))
        if _note_intervention(r3):
            interventions += 1

    return ModeOutcome(
        mode='B',
        seconds=time.perf_counter() - t0,
        adapter_calls=adapter_calls,
        interventions=interventions,
        transcripts=transcripts,
    )


def run_mode_c(
    workspace: Path,
    task_md: str,
    planner: Adapter,
    implementer: Adapter,
    tester: Adapter,
    reviewer: Adapter,
    timeout_s: int,
    baseline: Mapping[str, str],
) -> ModeOutcome:
    """C — planner → implementer → tester → reviewer → one implementer revision."""
    t0 = time.perf_counter()
    adapter_calls = 0
    interventions = 0
    transcripts: list[str] = []

    steps: list[tuple[str, Adapter, Callable[[], str]]] = [
        ('planner', planner, lambda: _prompt_planner(task_md)),
        ('implementer', implementer, lambda: _prompt_implementer(task_md)),
        ('tester', tester, lambda: _prompt_tester(task_md)),
    ]
    for _name, adapter, prompt_fn in steps:
        r = call_adapter(adapter, workspace, prompt_fn(), timeout_s)
        adapter_calls += 1
        transcripts.append(r.get('transcript_tail', ''))
        if _note_intervention(r):
            interventions += 1
            return ModeOutcome(
                mode='C',
                seconds=time.perf_counter() - t0,
                adapter_calls=adapter_calls,
                interventions=interventions,
                transcripts=transcripts,
            )

    diff = _diff_text(baseline, workspace)
    r_rev = call_adapter(reviewer, workspace, _prompt_reviewer(task_md, diff), timeout_s)
    adapter_calls += 1
    transcripts.append(r_rev.get('transcript_tail', ''))
    if _note_intervention(r_rev):
        interventions += 1
        return ModeOutcome(
            mode='C',
            seconds=time.perf_counter() - t0,
            adapter_calls=adapter_calls,
            interventions=interventions,
            transcripts=transcripts,
        )

    # One bounded implementer revision (always, as specified for mode C).
    r_fix = call_adapter(
        implementer,
        workspace,
        _prompt_revision(task_md, r_rev.get('transcript_tail', '')),
        timeout_s,
    )
    adapter_calls += 1
    transcripts.append(r_fix.get('transcript_tail', ''))
    if _note_intervention(r_fix):
        interventions += 1

    return ModeOutcome(
        mode='C',
        seconds=time.perf_counter() - t0,
        adapter_calls=adapter_calls,
        interventions=interventions,
        transcripts=transcripts,
    )


# ---------------------------------------------------------------------------
# Benchmark loop, summary, value gate
# ---------------------------------------------------------------------------

MIN_SAMPLES_FOR_GATE = 5


def list_tasks(tasks_root: Path) -> list[Path]:
    tasks_root = Path(tasks_root)
    if (tasks_root / 'meta.json').is_file():
        return [tasks_root]
    return sorted(p for p in tasks_root.iterdir() if p.is_dir() and (p / 'meta.json').is_file())


def shuffled_tasks(tasks: Sequence[Path], seed: int) -> list[Path]:
    order = list(tasks)
    random.Random(seed).shuffle(order)
    return order


def _mean(xs: Sequence[float]) -> float:
    return float(statistics.mean(xs)) if xs else 0.0


def _median(xs: Sequence[float]) -> float:
    return float(statistics.median(xs)) if xs else 0.0


def summarize_mode(rows: Sequence[Mapping[str, Any]]) -> dict:
    """Aggregate JSONL-like result rows for one mode."""
    n = len(rows)
    accepted = sum(1 for r in rows if r.get('accepted'))
    times = [float(r.get('seconds', 0)) for r in rows]
    calls = [int(r.get('adapter_calls', 0)) for r in rows]
    interventions = [int(r.get('interventions', 0)) for r in rows]
    total_calls = sum(calls)
    total_interventions = sum(interventions)
    cost = (total_calls / accepted) if accepted else float('inf')
    return {
        'n': n,
        'accepted': accepted,
        'accepted_rate': (accepted / n) if n else 0.0,
        'mean_seconds': _mean(times),
        'median_seconds': _median(times),
        'adapter_calls_total': total_calls,
        'adapter_calls_mean': _mean(calls),
        'interventions_total': total_interventions,
        'interventions_mean': _mean(interventions),
        'cost_per_accepted': cost,
    }


def value_gate(
    summary_a: Mapping[str, Any],
    summary_b: Mapping[str, Any],
    summary_c: Mapping[str, Any],
    *,
    min_samples: int = MIN_SAMPLES_FOR_GATE,
) -> dict:
    """Guide value gate: C (or B) vs A — no quality regression and ≥20% less
    intervention or ≥20% less time.

    Never claims statistical significance; small N → INSUFFICIENT_DATA.
    """
    samples = {
        'A': int(summary_a.get('n', 0)),
        'B': int(summary_b.get('n', 0)),
        'C': int(summary_c.get('n', 0)),
    }

    def _enough(s: Mapping[str, Any]) -> bool:
        return int(s.get('n', 0)) >= min_samples and samples['A'] >= min_samples

    def _beats(challenger: Mapping[str, Any], baseline: Mapping[str, Any]) -> tuple[bool, list[str]]:
        reasons = []
        # No quality regression: accepted rate must be >= A's.
        if float(challenger.get('accepted_rate', 0)) + 1e-12 < float(baseline.get('accepted_rate', 0)):
            return False, ['quality_regression']
        reasons.append('no_quality_regression')
        base_int = float(baseline.get('interventions_mean', 0))
        chal_int = float(challenger.get('interventions_mean', 0))
        base_t = float(baseline.get('mean_seconds', 0))
        chal_t = float(challenger.get('mean_seconds', 0))
        less_int = base_int > 0 and chal_int <= base_int * 0.8
        # If baseline interventions are already zero, treat "less intervention"
        # as satisfied only when challenger is also zero (no increase).
        if base_int == 0:
            less_int = chal_int == 0
        less_time = base_t > 0 and chal_t <= base_t * 0.8
        if less_int:
            reasons.append('ge_20pct_less_intervention')
        if less_time:
            reasons.append('ge_20pct_less_time')
        return (less_int or less_time), reasons

    if not (_enough(summary_b) or _enough(summary_c)):
        return {
            'verdict': 'INSUFFICIENT_DATA',
            'samples': samples,
            'min_samples': min_samples,
            'detail': 'need at least '
            f'{min_samples} samples for A and for B or C; never claim significance on small N',
        }

    evaluations = {}
    passed_modes = []
    for label, summary in (('C', summary_c), ('B', summary_b)):
        if not _enough(summary):
            evaluations[label] = {'eligible': False, 'ok': False, 'reasons': ['insufficient_samples']}
            continue
        ok, reasons = _beats(summary, summary_a)
        evaluations[label] = {'eligible': True, 'ok': ok, 'reasons': reasons}
        if ok:
            passed_modes.append(label)

    verdict = 'PASS' if passed_modes else 'FAIL'
    return {
        'verdict': verdict,
        'samples': samples,
        'min_samples': min_samples,
        'passed_modes': passed_modes,
        'evaluations': evaluations,
    }


def format_summary_table(
    summaries: Mapping[str, Mapping[str, Any]],
    gate: Mapping[str, Any],
) -> str:
    lines = []
    header = (
        f"{'mode':<6} {'n':>4} {'accept%':>8} {'mean_s':>8} {'med_s':>8} "
        f"{'calls':>7} {'interv':>7} {'cost/acc':>10}"
    )
    lines.append(header)
    lines.append('-' * len(header))
    for mode in ('A', 'B', 'C'):
        s = summaries.get(mode) or {}
        rate = 100.0 * float(s.get('accepted_rate', 0))
        cost = s.get('cost_per_accepted', float('inf'))
        cost_s = 'inf' if cost == float('inf') else f'{cost:.2f}'
        lines.append(
            f"{mode:<6} {int(s.get('n', 0)):>4} {rate:>7.1f}% "
            f"{float(s.get('mean_seconds', 0)):>8.3f} "
            f"{float(s.get('median_seconds', 0)):>8.3f} "
            f"{int(s.get('adapter_calls_total', 0)):>7} "
            f"{int(s.get('interventions_total', 0)):>7} "
            f"{cost_s:>10}"
        )
    lines.append('')
    samples = gate.get('samples', {})
    lines.append(
        f"value_gate: {gate.get('verdict')} "
        f"(samples A={samples.get('A', 0)} B={samples.get('B', 0)} "
        f"C={samples.get('C', 0)}, min={gate.get('min_samples', MIN_SAMPLES_FOR_GATE)})"
    )
    if gate.get('passed_modes'):
        lines.append('passed_modes: ' + ','.join(gate['passed_modes']))
    detail = gate.get('detail')
    if detail:
        lines.append(detail)
    return '\n'.join(lines)


def _bind_adapters_for_task(adapters: Mapping[str, Adapter], task_dir: Path) -> None:
    for ad in adapters.values():
        bind = getattr(ad, 'bind_task', None)
        if callable(bind):
            bind(task_dir)


def run_one(
    task_dir: Path,
    mode: str,
    adapters: Mapping[str, Adapter],
    *,
    timeout_s: Optional[int] = None,
    work_root: Optional[Path] = None,
) -> dict:
    """Run a single mode on one task and return a scored result record."""
    task_dir = Path(task_dir)
    meta = json.loads((task_dir / 'meta.json').read_text(encoding='utf-8'))
    task_md = (task_dir / 'TASK.md').read_text(encoding='utf-8')
    budget = int(timeout_s if timeout_s is not None else meta.get('time_budget_s', 300))

    _bind_adapters_for_task(adapters, task_dir)

    cleanup = work_root is None
    workspace = stage_workspace(task_dir, dest=work_root)
    baseline = snapshot_files(workspace)
    try:
        impl = adapters.get('implementer') or adapters.get('default')
        if impl is None:
            raise ValueError('adapters must include implementer (or default)')
        reviewer = adapters.get('reviewer', impl)
        planner = adapters.get('planner', impl)
        tester = adapters.get('tester', impl)

        if mode == 'A':
            outcome = run_mode_a(workspace, task_md, impl, budget, baseline)
        elif mode == 'B':
            outcome = run_mode_b(workspace, task_md, impl, reviewer, budget, baseline)
        elif mode == 'C':
            outcome = run_mode_c(
                workspace, task_md, planner, impl, tester, reviewer, budget, baseline,
            )
        else:
            raise ValueError(f'unknown mode {mode!r}')

        scored = score_workspace(task_dir, workspace, baseline)
        record = {
            'task_id': meta.get('id', task_dir.name),
            'mode': mode,
            'seconds': outcome['seconds'],
            'adapter_calls': outcome['adapter_calls'],
            'interventions': outcome['interventions'],
            'accepted': scored['accepted'],
            'scope_ok': scored['scope_ok'],
            'hidden_pass': scored['hidden_pass'],
            'hidden_total': scored['hidden_total'],
            'visible_pass': scored['visible_pass'],
            'visible_total': scored['visible_total'],
            'files_changed': scored['files_changed'],
            'expected_files_advisory': meta.get('expected_files', []),
        }
        return record
    finally:
        if cleanup:
            shutil.rmtree(workspace, ignore_errors=True)


def run_benchmark(
    tasks_root: Path,
    *,
    seed: int = 0,
    repeats: int = 1,
    modes: Sequence[str] = ('A', 'B', 'C'),
    adapters: Optional[Mapping[str, Adapter]] = None,
    timeout_s: Optional[int] = None,
    results_path: Optional[Path] = None,
) -> dict:
    """Shuffle tasks by seed, repeat N times, write JSONL, return summaries + gate."""
    tasks = list_tasks(Path(tasks_root))
    if not tasks:
        raise FileNotFoundError(f'no tasks under {tasks_root}')

    if adapters is None:
        # Default dry-run: perfect implementer for smoke use.
        adapters = {'implementer': PerfectAdapter(), 'reviewer': PerfectAdapter(),
                    'planner': PerfectAdapter(), 'tester': PerfectAdapter()}

    order = shuffled_tasks(tasks, seed)
    rows: list[dict] = []
    out_fp = None
    if results_path is not None:
        results_path = Path(results_path)
        results_path.parent.mkdir(parents=True, exist_ok=True)
        out_fp = results_path.open('w', encoding='utf-8')

    try:
        for rep in range(repeats):
            for task_dir in order:
                for mode in modes:
                    record = run_one(
                        task_dir, mode, adapters, timeout_s=timeout_s,
                    )
                    record['seed'] = seed
                    record['repeat'] = rep
                    rows.append(record)
                    if out_fp is not None:
                        out_fp.write(json.dumps(record, sort_keys=True) + '\n')
                        out_fp.flush()
    finally:
        if out_fp is not None:
            out_fp.close()

    summaries = {
        m: summarize_mode([r for r in rows if r['mode'] == m]) for m in modes
    }
    # Ensure A/B/C keys exist for the gate even if a mode was skipped.
    for m in ('A', 'B', 'C'):
        summaries.setdefault(m, summarize_mode([]))

    gate = value_gate(summaries['A'], summaries['B'], summaries['C'])
    table = format_summary_table(summaries, gate)
    return {
        'rows': rows,
        'summaries': summaries,
        'value_gate': gate,
        'table': table,
        'task_order': [p.name for p in order],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='MH-014 usefulness benchmark runner')
    parser.add_argument('--tasks', default='bench/tasks', help='tasks root directory')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--modes', default='A,B,C', help='comma-separated modes')
    parser.add_argument(
        '--adapter',
        default='',
        help='dotted path for a shared Adapter class (optional; default PerfectAdapter)',
    )
    parser.add_argument(
        '--implementer', default='', help='dotted path override for implementer adapter',
    )
    parser.add_argument('--reviewer', default='', help='dotted path override for reviewer')
    parser.add_argument('--planner', default='', help='dotted path override for planner')
    parser.add_argument('--tester', default='', help='dotted path override for tester')
    parser.add_argument('--timeout-s', type=int, default=None)
    parser.add_argument('--results', default='bench/results.jsonl')
    args = parser.parse_args(list(argv) if argv is not None else None)

    def _resolve(path: str, fallback: Optional[Adapter] = None) -> Adapter:
        if path:
            return load_adapter(path)
        if fallback is not None:
            return fallback
        return PerfectAdapter()

    shared = load_adapter(args.adapter) if args.adapter else PerfectAdapter()
    adapters = {
        'implementer': _resolve(args.implementer, shared),
        'reviewer': _resolve(args.reviewer, shared),
        'planner': _resolve(args.planner, shared),
        'tester': _resolve(args.tester, shared),
    }
    modes = tuple(m.strip().upper() for m in args.modes.split(',') if m.strip())
    result = run_benchmark(
        Path(args.tasks),
        seed=args.seed,
        repeats=args.repeats,
        modes=modes,
        adapters=adapters,
        timeout_s=args.timeout_s,
        results_path=Path(args.results),
    )
    print(result['table'])
    print(f"task_order (seed={args.seed}): {', '.join(result['task_order'])}")
    print(f"wrote {args.results}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
