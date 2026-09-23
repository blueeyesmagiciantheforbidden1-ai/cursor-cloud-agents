"""Frozen, bounded transfer experiments for improvement procedures.

The trusted evaluator supplies measured outcomes and complete resource costs.
This archive validates a fixed experiment; it executes no method, authenticates
no evaluator and never promotes deployed software. SQLite requires a private,
persistent controller disk. Candidate workers must not have database access.
"""
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from statistics import fmean


MAX_BYTES = 8_388_608
MAX_METHODS, MAX_CONDITIONS, MAX_UNITS, MAX_CELLS = 16, 8, 8192, 100_000
RESEARCH_COSTS = ('search_execution', 'failed_search', 'model_calls', 'compilation', 'verification',
                  'retries', 'speculation', 'context', 'migration', 'maintenance', 'other')
ECONOMIC_COSTS = ('discovery', 'unsuccessful_research', 'migration', 'maintenance', 'verification')
METHOD_FIELDS = ('code_digest', 'prompts_digest', 'dependencies_digest', 'parameters_digest', 'configuration_digest')


class TransferError(ValueError):
    pass


def _require(value, message):
    if not value:
        raise TransferError(message)


def canonical(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise TransferError('Record must be finite UTF-8 JSON') from None
    _require(len(raw) <= MAX_BYTES, 'Transfer record byte limit exceeded')
    return raw


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _fields(value, fields):
    _require(isinstance(value, dict) and set(value) == set(fields), 'Missing or unknown transfer fields')


def _hash(value):
    _require(isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None, 'Expected SHA-256 digest')
    return value


def _id(value):
    _require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value) is not None, 'Expected bounded identifier')
    return value


def _number(value, low=0, high=1, integer=False):
    _require(type(value) is int if integer else type(value) in (int, float), 'Invalid numeric type')
    _require(low <= value <= high and math.isfinite(value), 'Number outside declared bounds')
    return value


def simultaneous_radius(k, a, delta, m):
    """Two-sided union bound for K*A means of m independent [-1,1] deltas."""
    _number(k, 1, MAX_METHODS, True)
    _number(a, 1, MAX_CONDITIONS, True)
    _number(m, 1, MAX_UNITS, True)
    _number(delta, 1e-12, 0.25)
    return math.sqrt(2 * math.log(2 * k * a / delta) / m)


def required_units(k, a, delta, radius):
    _number(radius, 1e-9, 2)
    # Validate multiplicity parameters without restricting the computed result.
    simultaneous_radius(k, a, delta, 1)
    return math.ceil(2 * math.log(2 * k * a / delta) / radius**2)


def _unit(value):
    _fields(value, ('id', 'start_controller_digest', 'task_batch_digest', 'randomness_digest', 'holdout_ids'))
    _id(value['id'])
    for key in ('start_controller_digest', 'task_batch_digest', 'randomness_digest'):
        _hash(value[key])
    holdouts = value['holdout_ids']
    _require(isinstance(holdouts, list) and 1 <= len(holdouts) <= 128, 'Each independent unit needs bounded holdout IDs')
    for identifier in holdouts:
        _id(identifier)
    _require(len(set(holdouts)) == len(holdouts), 'Repeated holdout within a unit')
    return json.loads(canonical(value))


class TransferArchive:
    def __init__(self, path):
        _require(str(path) != ':memory:', 'Transfer archive requires persistent state')
        self.path = str(Path(path).resolve())
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS transfer_records (kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS transfer_reserved (kind TEXT, id TEXT, owner TEXT NOT NULL, PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS transfer_results (trial TEXT, method TEXT, condition_id TEXT, unit_id TEXT,
                    record_id TEXT NOT NULL, PRIMARY KEY(trial,method,condition_id,unit_id));
                CREATE TABLE IF NOT EXISTS transfer_decisions (trial TEXT PRIMARY KEY, record_id TEXT NOT NULL);
            ''')
            for table in ('transfer_records', 'transfer_reserved', 'transfer_results', 'transfer_decisions'):
                for action in ('UPDATE', 'DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()} BEFORE {action} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'transfer records are immutable'); END")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            raise TransferError('Record, unit or holdout already used') from None
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _put(self, db, kind, data):
        raw = canonical(data)
        identity = hashlib.sha256(raw).hexdigest()
        if not db.execute('SELECT 1 FROM transfer_records WHERE kind=? AND id=?', (kind, identity)).fetchone():
            _require(db.execute('SELECT COUNT(*) FROM transfer_records').fetchone()[0] < 200_000,
                     'Transfer archive record limit reached')
            db.execute('INSERT INTO transfer_records VALUES(?,?,?)', (kind, identity, raw.decode('utf-8')))
        return identity

    def _get(self, db, kind, identity):
        _hash(identity)
        row = db.execute('SELECT data FROM transfer_records WHERE kind=? AND id=?', (kind, identity)).fetchone()
        _require(row is not None, 'Unknown transfer record')
        _require(hashlib.sha256(row[0].encode('utf-8')).hexdigest() == identity, 'Transfer record hash mismatch')
        return json.loads(row[0])

    def freeze_method(self, snapshot):
        """Freeze references to code, prompts, dependencies, learned parameters and configuration."""
        _fields(snapshot, METHOD_FIELDS)
        for value in snapshot.values():
            _hash(value)
        data = {'kind': 'improvement_procedure', 'snapshot': snapshot}
        with self._db() as db:
            identity = self._put(db, 'method', data)
        return {'id': identity, 'snapshot_digest': digest(snapshot)}

    def _reserve(self, db, units, owner):
        for unit in units:
            fingerprint = digest({key: unit[key] for key in ('start_controller_digest', 'task_batch_digest', 'randomness_digest')})
            values = [('unit', unit['id']), ('unit_fingerprint', fingerprint)] + [('holdout', item) for item in unit['holdout_ids']]
            for kind, identity in values:
                db.execute('INSERT INTO transfer_reserved VALUES(?,?,?)', (kind, identity, owner))

    def preregister(self, spec):
        """Freeze every method, condition, sample and budget before any results."""
        spec = json.loads(canonical(spec))
        _fields(spec, ('name', 'incumbent_id', 'candidate_ids', 'delta', 'sample_size', 'conditions'))
        _id(spec['name'])
        candidates, conditions, m = spec['candidate_ids'], spec['conditions'], spec['sample_size']
        _require(isinstance(candidates, list) and 1 <= len(candidates) <= MAX_METHODS, 'Invalid candidate count')
        _require(isinstance(conditions, list) and 1 <= len(conditions) <= MAX_CONDITIONS, 'Invalid condition count')
        for method in [spec['incumbent_id'], *candidates]:
            _hash(method)
        _require(len(set(candidates)) == len(candidates) and spec['incumbent_id'] not in candidates, 'Methods must be distinct')
        simultaneous_radius(len(candidates), len(conditions), spec['delta'], m)
        _require((len(candidates) + 1) * len(conditions) * m <= MAX_CELLS, 'Transfer cell budget exceeded')
        all_units, ids = [], []
        for condition in conditions:
            _fields(condition, ('id', 'task_family_digest', 'research_horizon', 'research_budget_per_arm',
                'execution_budget_per_arm', 'resource_unit', 'eta', 'evaluator_digest', 'common_library_digest',
                'routing_policy_digest', 'units'))
            ids.append(_id(condition['id']))
            for key in ('task_family_digest', 'evaluator_digest', 'common_library_digest', 'routing_policy_digest'):
                _hash(condition[key])
            for key in ('research_horizon', 'research_budget_per_arm', 'execution_budget_per_arm'):
                _number(condition[key], 1, 10**12, True)
            _id(condition['resource_unit'])
            _number(condition['eta'], -1, 1)
            _require(isinstance(condition['units'], list) and len(condition['units']) == m, 'Each condition needs exactly m independent units')
            condition['units'] = [_unit(unit) for unit in condition['units']]
            all_units.extend(condition['units'])
        _require(len(set(ids)) == len(ids), 'Conditions must be distinct')
        spec['required_cells'] = (len(candidates) + 1) * len(conditions) * m
        spec['declared_full_research_budget'] = sum(condition['research_budget_per_arm'] * m * (len(candidates) + 1)
                                                   for condition in conditions)
        # This sum is meaningful only for a shared unit; no money/time conversion.
        _require(len({condition['resource_unit'] for condition in conditions}) == 1,
                 'One experiment needs one explicit common research accounting unit')
        with self._db() as db:
            for method in [spec['incumbent_id'], *candidates]:
                self._get(db, 'method', method)
            identity = digest(spec)
            # A same-name revision cannot silently consume extra multiplicity.
            db.execute("INSERT INTO transfer_reserved VALUES('experiment_name',?,?)", (spec['name'], identity))
            self._reserve(db, all_units, identity)
            self._put(db, 'trial', spec)
        return {'id': identity, 'candidate_count': len(candidates), 'condition_count': len(conditions),
                'sample_size': m, 'required_cells': spec['required_cells'],
                'declared_full_research_budget': spec['declared_full_research_budget']}

    def record_result(self, trial_id, method_id, condition_id, unit_id, *, quality, descendant_digest,
                      research_costs, research_steps, execution_cost, evaluator_digest, routing_policy_digest, common_library_digest,
                      research_disabled, private_state_transferred, contracts_pass):
        """Append one arm's unit-level quality. Tasks inside a unit are not extra samples."""
        _number(quality)
        _hash(descendant_digest)
        _fields(research_costs, RESEARCH_COSTS)
        for value in research_costs.values():
            _number(value, 0, 10**12, True)
        _number(execution_cost, 0, 10**12, True)
        _number(research_steps, 0, 10**12, True)
        _require(research_disabled is True and private_state_transferred is False,
                 'Final evaluation must disable research and exclude private task answers/history/cache')
        _require(type(contracts_pass) is bool, 'Protected contract outcome must be boolean')
        with self._db() as db:
            plan = self._get(db, 'trial', trial_id)
            _require(method_id in [plan['incumbent_id'], *plan['candidate_ids']], 'Method was not frozen for this trial')
            condition = next((row for row in plan['conditions'] if row['id'] == condition_id), None)
            _require(condition is not None and any(unit['id'] == unit_id for unit in condition['units']), 'Unit was not preregistered for this condition')
            for key, value in (('evaluator_digest', evaluator_digest), ('routing_policy_digest', routing_policy_digest),
                               ('common_library_digest', common_library_digest)):
                _require(condition[key] == value, 'Frozen evaluator, routing or common capability library changed')
            _require(sum(research_costs.values()) <= condition['research_budget_per_arm'], 'Full research budget exceeded')
            _require(research_steps <= condition['research_horizon'], 'Research horizon exceeded')
            _require(execution_cost <= condition['execution_budget_per_arm'], 'Final execution budget exceeded')
            data = {'trial_id': trial_id, 'method_id': method_id, 'condition_id': condition_id, 'unit_id': unit_id,
                    'quality': quality, 'descendant_digest': descendant_digest, 'research_costs': research_costs,
                    'research_steps': research_steps, 'execution_cost': execution_cost, 'contracts_pass': contracts_pass,
                    'research_disabled': True, 'private_state_transferred': False}
            record = self._put(db, 'result', data)
            db.execute('INSERT INTO transfer_results VALUES(?,?,?,?,?)', (trial_id, method_id, condition_id, unit_id, record))
            completed = db.execute('SELECT COUNT(*) FROM transfer_results WHERE trial=?', (trial_id,)).fetchone()[0]
        return {'record_id': record, 'completed_cells': completed, 'required_cells': plan['required_cells']}

    def progress(self, trial_id):
        with self._db() as db:
            plan = self._get(db, 'trial', trial_id)
            completed = db.execute('SELECT COUNT(*) FROM transfer_results WHERE trial=?', (trial_id,)).fetchone()[0]
        return {'completed_cells': completed, 'required_cells': plan['required_cells']}

    def finalize(self, trial_id):
        with self._db() as db:
            existing = db.execute('SELECT record_id FROM transfer_decisions WHERE trial=?', (trial_id,)).fetchone()
            if existing:
                return {'id': existing[0], **self._get(db, 'decision', existing[0])}
            plan = self._get(db, 'trial', trial_id)
            records = db.execute('SELECT method,condition_id,unit_id,record_id FROM transfer_results WHERE trial=?', (trial_id,)).fetchall()
            _require(len(records) == plan['required_cells'], 'Fixed experiment incomplete; no interim bounds or promotion')
            rows = {(method, condition, unit): self._get(db, 'result', record) for method, condition, unit, record in records}
            epsilon = simultaneous_radius(len(plan['candidate_ids']), len(plan['conditions']), plan['delta'], plan['sample_size'])
            methods = []
            for method in plan['candidate_ids']:
                results = []
                for condition in plan['conditions']:
                    pairs = [(rows[(method, condition['id'], unit['id'])],
                              rows[(plan['incumbent_id'], condition['id'], unit['id'])]) for unit in condition['units']]
                    deltas = [candidate['quality'] - baseline['quality'] for candidate, baseline in pairs]
                    mean = fmean(deltas)
                    budgets = all(sum(arm['research_costs'].values()) == condition['research_budget_per_arm']
                                  for pair in pairs for arm in pair)
                    contracts = all(arm['contracts_pass'] for pair in pairs for arm in pair)
                    lower, upper = max(-1, mean - epsilon), min(1, mean + epsilon)
                    # Promotion uses the stipulated untruncated mean-epsilon,
                    # including when a negative eta is explicitly permitted.
                    passed = mean - epsilon >= condition['eta'] and budgets and contracts
                    results.append({'condition_id': condition['id'], 'mean_delta': mean, 'lower_bound': lower,
                        'upper_bound': upper, 'eta': condition['eta'], 'equal_full_research_budget': budgets,
                        'contracts_pass': contracts, 'passes': passed})
                methods.append({'method_id': method, 'eligible': all(item['passes'] for item in results), 'conditions': results})
            actual = sum(sum(row['research_costs'].values()) for row in rows.values())
            data = {'trial_id': trial_id, 'confirmatory': True, 'record_only': True, 'epsilon': epsilon,
                    'delta': plan['delta'], 'sample_size': plan['sample_size'], 'methods': methods,
                    'declared_full_research_budget': plan['declared_full_research_budget'],
                    'actual_full_research_cost': actual, 'resource_unit': plan['conditions'][0]['resource_unit'],
                    'actual_final_execution_cost': sum(row['execution_cost'] for row in rows.values())}
            identity = self._put(db, 'decision', data)
            db.execute('INSERT INTO transfer_decisions VALUES(?,?)', (trial_id, identity))
        return {'id': identity, **data}

    def record_exploratory_2x2(self, *, incumbent_id, candidate_id, initial_library_digest,
                              accumulated_library_digest, units, scores):
        """Archive a descriptive method/library contrast; reserve its data from confirmation."""
        _hash(initial_library_digest)
        _hash(accumulated_library_digest)
        _require(incumbent_id != candidate_id and initial_library_digest != accumulated_library_digest,
                 'A factorial pilot needs distinct methods and libraries')
        _require(isinstance(units, list) and 1 <= len(units) <= MAX_UNITS, 'Invalid exploratory unit count')
        units = [_unit(unit) for unit in units]
        cells = ('m0_l0', 'm0_l1', 'm1_l0', 'm1_l1')
        _fields(scores, cells)
        for values in scores.values():
            _require(isinstance(values, list) and len(values) == len(units), 'Factorial cells must share matched units')
            for value in values:
                _number(value)
        contrasts = [scores['m1_l1'][i] - scores['m1_l0'][i] - scores['m0_l1'][i] + scores['m0_l0'][i]
                     for i in range(len(units))]
        data = {'incumbent_id': incumbent_id, 'candidate_id': candidate_id, 'initial_library_digest': initial_library_digest,
                'accumulated_library_digest': accumulated_library_digest, 'units': units, 'scores': scores,
                'mean_interaction': fmean(contrasts), 'confirmatory': False, 'eligible': False}
        with self._db() as db:
            self._get(db, 'method', incumbent_id)
            self._get(db, 'method', candidate_id)
            identity = digest(data)
            self._reserve(db, units, identity)
            self._put(db, 'exploratory', data)
        return {'id': identity, 'confirmatory': False, 'eligible': False, 'mean_interaction': data['mean_interaction']}


def economic_break_even(*, costs, baseline_per_task, candidate_per_task, reuses, unit=None, quality_comparable=False):
    """Calculate reuse economics only with explicitly comparable quality and money units.

    Costs are disjoint categories, including unsuccessful work. This does not
    convert latency, memory or quality into money or verify supplied estimates.
    """
    _fields(costs, ECONOMIC_COSTS)
    for value in [*costs.values(), baseline_per_task, candidate_per_task]:
        _number(value, 0, 1e15)
    _number(reuses, 0, 10**12, True)
    _require(type(quality_comparable) is bool, 'Quality comparability must be explicit')
    if unit is None or not quality_comparable:
        return {'defined': False, 'reason': 'common_money_unit_required' if unit is None else 'comparable_quality_required'}
    _require(isinstance(unit, str) and (unit == 'microusd' or re.fullmatch(r'[A-Z]{3}', unit) is not None),
             'Declare one common money unit; resource conversions are not inferred')
    discovery = math.fsum(costs.values())
    saving = baseline_per_task - candidate_per_task
    result = {'defined': True, 'unit': unit, 'full_discovery_cost': discovery, 'saving_per_task': saving,
              'reuses': reuses, 'net_savings': reuses * saving - discovery,
              'break_even_reuses': None, 'break_even_whole_tasks': None}
    if saving > 0:
        reuse_level = discovery / saving
        if math.isfinite(reuse_level):
            result.update(break_even_reuses=reuse_level, break_even_whole_tasks=math.ceil(reuse_level))
        else:
            result['reason'] = 'break_even_outside_numeric_range'
    else:
        result['reason'] = 'no_positive_per_task_saving'
    return result
