"""Execute the fixed revision-validation protocol; never select a favorable seed."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ryan_frontier import research


def load_previous():
    path = ROOT / 'examples/revision-0/research.py'
    spec = importlib.util.spec_from_file_location('ryan_frontier._revision_0', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main():
    protocol = json.loads((ROOT / 'examples/revision-protocol.json').read_text())
    previous = load_previous()
    destination = ROOT / 'examples/revision-validation'
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    for seed in protocol['validation_seeds']:
        parameters = dict(seed=seed, units=protocol['independent_lineages_per_seed'],
                          budget=protocol['candidate_budget_per_task_arm'],
                          development_tasks=protocol['development_tasks_per_lineage'],
                          selection_tasks=protocol['selection_tasks_per_lineage'],
                          confirmation_tasks=protocol['confirmation_tasks_per_condition_lineage'])
        before, after = previous.run_pilot(**parameters), research.run_pilot(**parameters)
        for label, report in (('before', before), ('after', after)):
            (destination / f'{seed}-{label}.json').write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')
        for condition in protocol['conditions']:
            old_units = before['factorial']['units']
            new_units = after['factorial']['units']
            counters = dict(tasks=0, old_combined_solved=0, new_combined_solved=0,
                            no_memory_solved=0, old_memory_regressions=0,
                            new_memory_regressions=0, first_success_mismatches=0,
                            lost_previously_solved=0, no_memory_lost=0, stable_arm_changes=0, fixed_baseline_changes=0, baseline_quality_gaps=0)
            for old_unit, new_unit in zip(old_units, new_units):
                old = old_unit['conditions'][condition]['arms']
                new = new_unit['conditions'][condition]['arms']
                for a,b,c in zip(old['M1_C1']['tasks'],new['M1_C1']['tasks'],new['M1_C0']['tasks']):
                    assert a['task_id']==b['task_id']==c['task_id']
                    counters['tasks'] += 1
                    counters['old_combined_solved'] += a['solved']
                    counters['new_combined_solved'] += b['solved']
                    counters['no_memory_solved'] += c['solved']
                    counters['old_memory_regressions'] += c['solved'] and not a['solved']
                    counters['new_memory_regressions'] += c['solved'] != b['solved']
                    counters['first_success_mismatches'] += c['first_success_slot'] != b['first_success_slot']
                    counters['lost_previously_solved'] += a['solved'] and not b['solved']
                counters['no_memory_lost'] += sum(a['solved'] and not b['solved'] for a,b in zip(old['M1_C0']['tasks'],new['M1_C0']['tasks']))
                old_fixed=old_unit['conditions'][condition]['fixed_baseline']['tasks']
                new_fixed=new_unit['conditions'][condition]['fixed_baseline']['tasks']
                counters['fixed_baseline_changes'] += sum(a['solved'] != b['solved'] for a,b in zip(old_fixed,new_fixed))
                counters['baseline_quality_gaps'] += sum(a['solved'] != b['solved'] for a,b in zip(new['M1_C1']['tasks'],new_fixed))
                for arm in ('M0_C0','M0_C1'):
                    counters['stable_arm_changes'] += sum(a['solved'] != b['solved']
                        for a,b in zip(old[arm]['tasks'],new[arm]['tasks']))
            records.append({'seed':seed,'condition':condition,**counters})
    passed = all(not any(row[k] for k in ('new_memory_regressions','first_success_mismatches',
                   'lost_previously_solved','no_memory_lost','stable_arm_changes','fixed_baseline_changes','baseline_quality_gaps')) for row in records)
    summary = {'protocol':protocol,'engineering_gate_passed':passed,'comparisons':records,
               'scientific_claim':'Same finite grammar only; no superiority over the all-guards baseline and no 10/10 certification',
               'research_promotion':'Requires separate sufficient evidence and exact-version approval'}
    (destination/'summary.json').write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n')
    print(json.dumps(summary,indent=2,sort_keys=True))
    return 0 if passed else 1

if __name__=='__main__':
    raise SystemExit(main())
