"""Offline integration entry point. No network calls or provider credentials."""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import platform
import sys
import time
import uuid
from . import __version__
from .artifacts import ArtifactStore
from .ledger import Ledger
from .promotion import PromotionRegistry
from .research import CONDITIONS, run_pilot, export_artifacts
from .routing import RoutingPolicy, RoutingPlanner, RoutingRequest


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()


def _atomic_write(path: Path, data: bytes):
    """Publish complete bytes and fsync them before exposing the new filename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_parameters(seed, units, budget):
    """Bound the interactive CLI before creating directories or durable work."""
    for name, value, lower, upper in (
        ('seed', seed, -(1 << 63), (1 << 63) - 1),
        ('units', units, 1, 256),
        ('budget', budget, 1, 32),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError(f'{name} must be an integer in [{lower},{upper}]')


def _confirmation_gate(report):
    """Recompute the full declared gate, including the memory non-regression check."""
    margin = report['design']['effect_margin']
    if isinstance(margin, bool) or not isinstance(margin, (int, float)) or not 0 <= margin < 1:
        raise ValueError('Invalid evidence margin')
    intervals = report['confirmation']['intervals']
    if set(intervals) != set(CONDITIONS):
        raise ValueError('Confirmation evidence must include every predefined condition')
    checks = {}
    lower_bounds = []
    for condition in CONDITIONS:
        for contrast in ('method_without_memory', 'method_with_memory'):
            lower = intervals[condition][contrast]['lower']
            if (isinstance(lower, bool) or not isinstance(lower, (int, float))
                    or not -1 <= lower <= 1 or not math.isfinite(lower)):
                raise ValueError('Evidence lower bounds must be finite differences in [-1,1]')
            if contrast == 'method_without_memory':
                lower_bounds.append(lower)
                checks[f'{condition}:{contrast}'] = lower > margin
            else:
                checks[f'{condition}:{contrast}'] = lower >= 0
    decision = ('evidence_passed_manual_review_required' if all(checks.values())
                else 'inconclusive')
    if report['confirmation']['decision'] != decision or report['confirmation']['promoted'] is not False:
        raise ValueError('Confirmation decision disagrees with its evidence or attempts automatic promotion')
    return min(lower_bounds), margin, checks


def execute(output, *, seed=20260921, units=8, budget=6):
    _validate_parameters(seed, units, budget)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    objects = ArtifactStore(output / 'objects')
    ledger = Ledger(output / 'controller.db', budget_limit=0)
    registry = PromotionRegistry(output / 'releases.db')
    run_id = uuid.uuid4().hex
    task = lease = invocation = None
    reserved = False
    try:
        # The content-addressed source manifest makes the evaluator and learning
        # pipeline reconstructable. domain.py alone does not bind statistical
        # inference, selection, exporting, or the trusted release gate.
        source_snapshot = {path.name: objects.put(path.read_bytes())
                           for path in sorted(Path(__file__).parent.glob('*.py'))}
        runtime = {'python': platform.python_version(),
                   'implementation': platform.python_implementation(),
                   'package_version': __version__, 'third_party_dependencies': []}
        pipeline = {'schema_version': 1, 'sources': source_snapshot, 'runtime': runtime,
                    'parameters': {'seed': seed, 'units': units, 'budget': budget}}
        pipeline_hash = objects.put(json_bytes(pipeline))
        task = ledger.create_task({'kind': 'offline_research_pilot', **pipeline['parameters'],
                                   'pipeline_sha256': pipeline_hash}, idempotency_key=run_id)
        lease = ledger.claim(task, 'local-research-worker', ttl_seconds=3600)
        invocation = run_id + '-research'
        grant = ledger.reserve(task, lease['fence'], invocation, 0)
        reserved = True
        if not grant['dispatch_allowed']:
            raise RuntimeError('Invocation has no fresh dispatch grant')
        started = time.perf_counter()
        report = run_pilot(seed=seed, units=units, budget=budget)
        elapsed = time.perf_counter() - started
        lower, margin, checks = _confirmation_gate(report)
        run_directory = output / 'runs' / run_id
        run_directory.mkdir(parents=True, exist_ok=False)
        source_paths = export_artifacts(report, run_directory / 'generated')
        source_hashes = {}
        for path in source_paths:
            p = Path(path)
            data = p.read_bytes()
            source_hashes[p.name] = objects.put(data)
            if p.suffix == '.py' and source_hashes[p.name] != report['artifacts'][p.stem]['sha256']:
                raise ValueError('Exported executable differs from its recorded source identity')
            # Compatibility copies are convenience views. The immutable run
            # directory and receipt enumerate the exact outputs of this run.
            _atomic_write(output / 'generated' / p.name, data)
        report_data = json_bytes(report)
        report_hash = objects.put(report_data)
        _atomic_write(run_directory / 'report.json', report_data)
        evaluator_hash = objects.put(json_bytes({
            'schema_version': 1, 'pipeline_sha256': pipeline_hash,
            'design': report['design'], 'design_digest': report['design_digest'],
        }))
        frozen_hash = objects.put(json_bytes(report['frozen_methods']))
        candidate_hash = objects.put(json_bytes({
            'schema_version': 1, 'kind': 'frozen_lineage_policy_bundle',
            'frozen_methods_sha256': frozen_hash,
            'executable_artifacts': {entry['artifact_id']: {
                'source_sha256': source_hashes[entry['artifact_id'] + '.py'],
                'spec_sha256': source_hashes[entry['artifact_id'] + '.json'],
            } for entry in report['frozen_methods']},
        }))
        proposal = registry.stage(candidate_hash, evaluator_hash, report_hash, lower,
                                  margin, checks=checks)
        receipt = {
            'schema_version': 1, 'run_id': run_id, 'task_id': task,
            'pipeline_sha256': pipeline_hash, 'report_sha256': report_hash,
            'candidate_sha256': candidate_hash, 'evaluator_sha256': evaluator_hash,
            'frozen_methods_sha256': frozen_hash, 'generated_artifacts': source_hashes,
            'proposal_id': proposal, 'model_calls': 0, 'api_spend_microdollars': 0,
        }
        receipt_hash = objects.put(json_bytes(receipt))
        _atomic_write(run_directory / 'receipt.json', json_bytes(receipt))
        ledger.settle(invocation, 0)
        ledger.complete(task, lease['fence'], {'outcome': 'succeeded',
                                             'receipt_sha256': receipt_hash,
                                             'report_sha256': report_hash, 'proposal_id': proposal})
        summary = {
            'version': __version__, 'seed': seed, 'units': units, 'budget': budget,
            'elapsed_seconds': round(elapsed, 6),
            'model_calls': 0, 'api_spend_microdollars': 0,
            'report_sha256': report_hash, 'evaluator_sha256': evaluator_hash,
            'candidate_sha256': candidate_hash, 'pipeline_sha256': pipeline_hash,
            'receipt_sha256': receipt_hash, 'proposal_id': proposal, 'run_id': run_id,
            'frozen_methods_sha256': frozen_hash, 'generated_artifacts': source_hashes,
            'generated_directory': str(run_directory / 'generated'),
            'run_directory': str(run_directory), 'runtime': runtime,
            'factorial': {condition: {arm: values['mean_solved_fraction']
                         for arm, values in arms.items()}
                         for condition, arms in report['factorial']['summary'].items()},
            'confirmation': report['confirmation']['decision'],
            'promotion_status': registry.proposal(proposal)['status'],
            'active_release': registry.current(),
            'lifecycle_cost': report['lifecycle_cost'], 'ledger': ledger.summary(),
            'task_id': task, 'output': str(output),
        }
        if 'fixed_baseline' in report:
            summary['fixed_baseline'] = report['fixed_baseline']['summary']
        _atomic_write(run_directory / 'summary.json', json_bytes(summary))
        _atomic_write(output / 'report.json', report_data)
        _atomic_write(output / 'summary.json', json_bytes(summary))
        return summary
    except BaseException as original:
        # Offline work has a known zero cash cost, even if the computation fails.
        # A real provider timeout must use mark_uncertain and reconciliation.
        # Cleanup errors must never mask the original failure. If a lease was
        # already lost, its remaining state is intentionally left for recovery.
        try:
            if reserved:
                ledger.settle(invocation, 0)
            if task is not None and lease is not None and ledger.get_task(task)['status'] != 'completed':
                ledger.complete(task, lease['fence'], {'outcome': 'failed',
                    'error_type': type(original).__name__, 'message': str(original)})
        except Exception as cleanup_error:
            original.add_note(f'Ledger failure cleanup could not complete: {cleanup_error}')
        raise
    finally:
        registry.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('demo', 'pilot'):
        p = commands.add_parser(command, help='Run a fully offline bounded research experiment')
        p.add_argument('--output', type=Path, default=Path('runs') / command)
        p.add_argument('--seed', type=int, default=20260921)
        p.add_argument('--units', type=int, default=4 if command == 'demo' else 8)
        p.add_argument('--budget', type=int, default=6)
    route = commands.add_parser('route', help='Inspect a route; no provider is called')
    route.add_argument('--config', type=Path, required=True)
    route.add_argument('--capability', action='append', default=[])
    route.add_argument('--local-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'route':
            policy = RoutingPolicy.from_file(args.config)
            result = RoutingPlanner(policy).plan(RoutingRequest(
                capabilities=frozenset(args.capability or ['code']), local_only=args.local_only)).to_dict()
        else:
            result = execute(args.output, seed=args.seed, units=args.units, budget=args.budget)
        print(json_bytes(result).decode(), end='')
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 2
