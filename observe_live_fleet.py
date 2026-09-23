"""Read-only observer for the live fleet (RUNBOOK step 5 helper). Never writes to the cloud.

    python -B observe_live_fleet.py ticks [--limit N]   last N fleet ticks (log kind=runcrew_fleet_tick) and the executions
                                                         created today for each live job, through gcloud (read-only)
    python -B observe_live_fleet.py checks              reproduce the fleet controller's READ-ONLY checks for every slot with
                                                         the operator's gcloud token: state document, job identity + template
                                                         digest, prior execution, credential release. Prints the exact require
                                                         code that stops a slot (the service masks it as controller_attention_required).

The controller code is imported from live-worker-runtime and agent_hub from the repo mirror, so `checks` exercises the same
functions the service runs. No token, grant or credential is ever printed.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

WORK = Path(__file__).resolve().parent
PROJECT, REGION, ACCOUNT = 'project-0c6d31fa-509e-4116-a2c', 'us-central1', 'gcp-operator@example.invalid'
FLEET_SERVICE = 'runcrew-live-fleet'
FLEET_CONFIG = WORK / 'cloud-agent-online' / 'live-fleet-20260922a-configs' / 'fleet.json'


def gcloud(*args, timeout=120):
    env = dict(os.environ, CLOUDSDK_CORE_DISABLE_PROMPTS='1')
    result = subprocess.run(['gcloud', *args, '--project=' + PROJECT, '--account=' + ACCOUNT, '--format=json'],
                            capture_output=True, text=True, env=env, shell=os.name == 'nt', timeout=timeout)
    if result.returncode:
        raise RuntimeError('gcloud_failed:' + ' '.join(args[:3]))
    return json.loads(result.stdout) if result.stdout.strip() else []


def summarize_ticks(rows):
    """Cloud Logging rows (dicts with timestamp + jsonPayload) -> oldest-first list of {at, workers}."""
    out = []
    for row in sorted(rows, key=lambda r: r.get('timestamp', '')):
        payload = row.get('jsonPayload') or {}
        if payload.get('kind') != 'runcrew_fleet_tick':
            continue
        workers = payload.get('workers') or {}
        out.append({'at': row.get('timestamp', '')[:19], 'workers': {
            name: {k: v for k, v in (value or {}).items() if k in ('status', 'generation', 'execution')}
            for name, value in workers.items()}})
    return out


def summarize_executions(rows, day):
    """gcloud executions list rows -> [{name, created, running, succeeded, failed}] created on `day` (YYYY-MM-DD)."""
    out = []
    for row in rows:
        meta, status = row.get('metadata', {}), row.get('status', {})
        created = meta.get('creationTimestamp', '')
        if not created.startswith(day):
            continue
        out.append({'name': meta.get('name'), 'created': created[:19], 'running': status.get('runningCount', 0),
                    'succeeded': status.get('succeededCount', 0), 'failed': status.get('failedCount', 0)})
    return out


def parked(summary):
    """True when every worker in the newest tick is still masked."""
    if not summary:
        return True
    return all(w.get('status') == 'controller_attention_required' for w in summary[-1]['workers'].values())


def ticks(limit):
    rows = gcloud('logging', 'read', 'resource.type="cloud_run_revision" AND resource.labels.service_name="' + FLEET_SERVICE
                  + '" AND jsonPayload.kind="runcrew_fleet_tick"', '--limit=' + str(limit), '--order=desc')
    summary = summarize_ticks(rows)
    config = json.loads(FLEET_CONFIG.read_bytes())
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    executions = {}
    for policy in config['policies']:
        job = policy['profile']['job_id']
        executions[job] = summarize_executions(gcloud('run', 'jobs', 'executions', 'list', '--job=' + job,
                                                       '--region=' + REGION, '--limit=5'), day)
    print(json.dumps({'ticks': summary, 'parked': parked(summary), 'executions_today': executions}, indent=1))
    return 0


def checks():
    sys.path.insert(0, str(WORK / 'source' / 'agent-hub'))
    sys.path.insert(0, str(WORK / 'live-worker-runtime'))
    from cloud_runtime import Runtime
    from fleet_controller import ControllerError
    from agent_hub.credential_broker_service import BrokerError
    config = json.loads(FLEET_CONFIG.read_bytes())
    runtime = Runtime(config)
    token = subprocess.run(['gcloud', 'auth', 'print-access-token', '--account=' + ACCOUNT], capture_output=True,
                           text=True, shell=os.name == 'nt', timeout=60).stdout.strip()
    if not token:
        raise RuntimeError('operator_token_unavailable')
    report = {}
    for controller in runtime.controllers:
        for rest in (controller.cloud.rest, controller.broker.rest):
            rest._access_token, rest._token_deadline = token, time.monotonic() + 1500
        name = controller.policy.profile.provider
        row = {}
        try:
            state, _ = controller.store.read()
            row['state'] = None if state is None else {k: state.get(k) for k in ('phase', 'generation', 'next_launch_at')}
            job = controller.job()
            row['job'] = {'ok': True, 'name_form': 'project-id' if '/project-' in job.get('name', '') else 'project-number',
                          'latest_execution': job.get('latestCreatedExecution', {}).get('name')}
            previous = controller.current_terminal(job)
            row['prior_execution_and_credential'] = {'ok': True, 'uid': previous.get('uid')}
        except (ControllerError, BrokerError) as error:
            row['stopped_by'] = str(error)
        except Exception as error:  # never leak an environment detail; the class name is enough
            row['stopped_by'] = 'unexpected:' + type(error).__name__
        report[name] = row
    print(json.dumps(report, indent=1))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=('ticks', 'checks'))
    parser.add_argument('--limit', type=int, default=6)
    args = parser.parse_args()
    return ticks(args.limit) if args.mode == 'ticks' else checks()


if __name__ == '__main__':
    sys.exit(main())
