"""Cloud-owned replenishment of single-use warm jobs, with durable intents.

Clean terminal executions with released credential ownership are replaced; a
failed one that released cleanly is replaced after a backoff, and the third
consecutive failure stops the slot. Unknown launches, unreleased or quarantined
credentials stop the slot at once instead of spending money in a restart loop.
This module contains no provider calls.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import re
import secrets
import time
import uuid

from dynamic_broker import BindingStore
from agent_hub.credential_broker_service import ExecutionGrantStore
from agent_hub.cloud_credential_broker import PROJECT_ID, PROJECT_NUMBER


class ControllerError(RuntimeError): pass


# A worker that fails after cleanly releasing its credential is replaced after
# a growing delay; the third consecutive failure stops the slot. Before this,
# the first failure stopped it: one transient provider error (a lost warm
# session, a dropped connection) killed codex, claude and copilot for good on
# 2026-09-23 after hours of clean hourly replacements. The bound and the
# backoff keep a broken credential from becoming a paid restart loop.
MAX_CONSECUTIVE_FAILURES = 3
FAILURE_BACKOFF_SECONDS = (120, 600)
# Process exit used by the worker for any *_quota_exhausted code
# (provider_errors.QUOTA_EXIT_CODE). It is not a strike: the account limit
# would otherwise burn the slot in one backoff cycle. Parks are 1h, 2h, then
# 4h, and they stay at 4h.
QUOTA_EXIT_CODE = 75
QUOTA_PARK_BASE_SECONDS = 3600
QUOTA_PARK_CAP_SECONDS = 14400


SAFE_CODE = re.compile(r'[a-z][a-z0-9_]{0,99}')


def require(value, code):
    if not value: raise ControllerError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def canonical_name(name):
    """Cloud Run echoes resource names with the project ID; profiles build them with the project number."""
    if isinstance(name, str) and name.startswith('projects/' + PROJECT_ID + '/'):
        return 'projects/' + PROJECT_NUMBER + '/' + name[len('projects/' + PROJECT_ID + '/'):]
    return name


def terminal(value):
    require(isinstance(value, dict), 'execution_response_invalid')
    return bool(value.get('completionTime')) and not value.get('reconciling')


def exact_execution(policy, value, intent=None):
    prefix = policy.profile.job_name + '/executions/' + policy.profile.job_id + '-'
    name = canonical_name(value.get('name')) if isinstance(value, dict) else None
    require(isinstance(name, str) and name.startswith(prefix) and re.fullmatch('[a-z0-9]{5,20}', name[len(prefix):])
            and re.fullmatch(r'[a-f0-9-]{36}', value.get('uid', '')), 'execution_identity_invalid')
    require(value.get('taskCount', 1) == 1 and type(value.get('taskCount', 1)) is int,
            'execution_task_count_changed')
    task = value.get('template', {})
    require(task.get('serviceAccount') == policy.caller_service_account and task.get('maxRetries', 0) == 0,
            'execution_identity_invalid')
    if intent is not None:
        containers = task.get('containers')
        require(isinstance(containers, list) and len(containers) == 1, 'execution_container_changed')
        env = containers[0].get('env', [])
        matches = [x for x in env if x.get('name') == 'RUNCREW_CONTROLLER_INTENT']
        require(matches == [{'name': 'RUNCREW_CONTROLLER_INTENT', 'value': intent}], 'execution_intent_mismatch')
    return value


class Controller:
    """store.read -> (dict|None, version); store.cas is durable compare-and-swap.

    Concurrent scheduler deliveries can read state, but only one CAS owner may
    perform the corresponding external mutation. An intent is never replayed.
    """
    def __init__(self, policy, slot, store, cloud, broker, *, binding_store=None,
                 grant_factory=ExecutionGrantStore, clock=time.time):
        self.policy, self.slot, self.store, self.cloud, self.broker = policy, slot, store, cloud, broker
        self.bindings = binding_store or BindingStore(policy)
        self.grant_factory, self.clock = grant_factory, clock
        require(set(slot) == {'job_uid', 'template_sha256', 'enabled'} and type(slot['enabled']) is bool
                and re.fullmatch(r'[a-f0-9-]{36}', slot['job_uid'])
                and re.fullmatch(r'[a-f0-9]{64}', slot['template_sha256']), 'slot_config_invalid')

    @property
    def config_sha(self):
        return digest({'policy': asdict(self.policy), 'slot': self.slot})

    def _policy_sha(self):
        return digest(asdict(self.policy))

    def _config_for_template(self, template_sha256):
        slot = {'job_uid': self.slot['job_uid'], 'template_sha256': template_sha256,
                'enabled': self.slot['enabled']}
        return digest({'policy': asdict(self.policy), 'slot': slot})

    def _binding_fields(self):
        """Identity the state must carry so a later template rollout can be proved.

        config_sha256 mixes the policy and the whole slot. Splitting policy_sha256
        from the slot fields lets a template-only change be checked without
        treating every image rollout as a new controller.
        """
        return {'policy_sha256': self._policy_sha(), 'slot_job_uid': self.slot['job_uid'],
                'slot_enabled': self.slot['enabled'], 'slot_template_sha256': self.slot['template_sha256'],
                'config_sha256': self.config_sha}

    def _previous_template_sha256(self, state):
        """Template digest the stored config_sha256 was computed with.

        New states persist it. A document that only has config_sha256 (the
        controller before this split) still proves a re-key when the live job
        has not yet been updated: that template is the previous one, and
        recomputing the digest with it must equal the stored config_sha256.
        Once the image and the slot digest have both moved, the previous
        template is no longer derivable; the tick that ran while they still
        matched has to have saved it.
        """
        recorded = state.get('slot_template_sha256')
        if isinstance(recorded, str) and re.fullmatch(r'[a-f0-9]{64}', recorded):
            return recorded
        job = self.cloud.get(self.policy.profile.job_name)
        if (not isinstance(job, dict) or canonical_name(job.get('name')) != self.policy.profile.job_name
                or job.get('uid') != self.slot['job_uid']):
            return None
        try:
            derived = digest(job.get('template'))
        except (TypeError, ValueError):
            return None
        return derived if re.fullmatch(r'[a-f0-9]{64}', derived) else None

    def _template_only_change(self, state):
        previous = self._previous_template_sha256(state)
        if previous is None or previous == self.slot['template_sha256']:
            return False
        if state.get('policy_sha256') not in (None, self._policy_sha()):
            return False
        if state.get('slot_job_uid') not in (None, self.slot['job_uid']):
            return False
        if 'slot_enabled' in state and state.get('slot_enabled') != self.slot['enabled']:
            return False
        # Current policy + current job uid and enabled + the PREVIOUS template.
        # Any other config edit fails this equality, including on a legacy
        # document that stored nothing but config_sha256.
        return self._config_for_template(previous) == state.get('config_sha256')

    def _remember_binding(self, state, version):
        fields = self._binding_fields()
        if all(state.get(key) == value for key, value in fields.items()):
            return state, version
        return self.save(state, version, **fields)

    def _adopt_config(self, state, version):
        """Re-key a provable template-only change, or refuse.

        Idle and blocked are the only phases that may take the new digest: the
        next launch has not started, or the slot is already stopped. Binding,
        launch, grant, and active keep controller_config_changed so a rollout
        cannot retarget an attempt that is already in flight. A matching config
        is not rewritten in those phases either. A CAS while the execution is
        still active changes the receipt a concurrent revision will archive, and
        the two receipts then disagree (controller_archive_uncertain). The
        active drain saves idle and continues this loop, so the binding is
        recorded on that later pass of the same tick. The re-key itself is one
        CAS of the new config_sha256 plus the split fields.
        """
        if state.get('config_sha256') == self.config_sha:
            if state.get('phase') not in ('idle', 'blocked'):
                return state, version
            return self._remember_binding(state, version)
        if state.get('phase') not in ('idle', 'blocked'):
            raise ControllerError('controller_config_changed')
        if not self._template_only_change(state):
            raise ControllerError('controller_config_changed')
        return self.save(state, version, **self._binding_fields())

    def job(self):
        value = self.cloud.get(self.policy.profile.job_name)
        require(canonical_name(value.get('name')) == self.policy.profile.job_name and value.get('uid') == self.slot['job_uid']
                and value.get('etag') and not value.get('deleteTime') and not value.get('reconciling'),
                'job_identity_changed')
        require(digest(value.get('template')) == self.slot['template_sha256'], 'job_template_changed')
        return value

    def idle_credential(self, previous=None):
        state, _ = self.broker._read()
        require(state.get('phase') == 'idle' and state.get('quarantine_reason') == ''
                and state.get('version') == state.get('last_release_version')
                and state.get('fence') == state.get('last_release_fence'), 'credential_not_cleanly_released')
        if previous is not None:
            require(state.get('execution_uid') == previous['uid'], 'credential_release_execution_mismatch')
        return state

    def never_bound(self, state, value):
        """True when the latest execution is this slot's own failed launch that never acquired the credential.

        Either its grant was never published (the worker dies at bootstrap
        without one), or the broker's release record still names the slot's
        previous holder: acquiring moves the record to the new execution, so a
        record that never moved means this execution never held the credential.
        Demanding equality with it would deadlock the slot. The clean release
        itself is still checked by idle_credential().
        """
        if not isinstance(state, dict) or state.get('execution_uid') != value.get('uid'):
            return False
        # Failed or cancelled before binding; a succeeded execution held the credential.
        if value.get('succeededCount', 0) != 0:
            return False
        grant_sha256, expires_at = state.get('grant_sha256'), state.get('expires_at')
        if not (isinstance(grant_sha256, str) and re.fullmatch(r'[a-f0-9]{64}', grant_sha256) and type(expires_at) is int):
            return False
        if self.grant_factory(self.policy.binding(grant_sha256, expires_at)).read() is None:
            return True
        # release_uid: the holder whose clean release licensed this launch
        # (older launches recorded only previous_uid, the same execution).
        holder = state.get('release_uid', state.get('previous_uid'))
        credential, _ = self.broker._read()
        return (isinstance(holder, str) and holder != value.get('uid')
                and credential.get('execution_uid') == holder)

    def current_terminal(self, job, state=None):
        latest = job.get('latestCreatedExecution', {}).get('name')
        require(isinstance(latest, str) and latest, 'prior_execution_required')
        if '/' not in latest: latest = self.policy.profile.job_name + '/executions/' + latest
        value = exact_execution(self.policy, self.cloud.get(latest))
        require(terminal(value) and value.get('runningCount', 0) == 0, 'prior_execution_still_active')
        # A clean release is always required; the releasing execution must be
        # this one unless this one never held the credential at all.
        self.idle_credential(None if self.never_bound(state, value) else value)
        return value

    def _task_exit_code(self, execution):
        """Exit code of the execution's only task, or None when it cannot be proved.

        Cloud Run v2 lists tasks at ``<execution>/tasks``. Permission, transport,
        shape, a count other than one, or a missing exit code all fail closed
        so the strike path is unchanged. An exit code other than 75 is returned
        and is not treated as quota.
        """
        try:
            listed = self.cloud.get(execution + '/tasks')
        except Exception:
            return None
        if not isinstance(listed, dict):
            return None
        tasks = listed.get('tasks')
        if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
            return None
        attempt = tasks[0].get('lastAttemptResult')
        if not isinstance(attempt, dict) or type(attempt.get('exitCode')) is not int:
            return None
        return attempt['exitCode']

    def _quota_park_seconds(self, parks):
        if type(parks) is not int or parks < 0:
            parks = 0
        # 2**parks hits the 4h cap at parks==2. Bound the shift so a corrupt
        # quota_parks cannot build an enormous integer.
        return min(QUOTA_PARK_CAP_SECONDS, QUOTA_PARK_BASE_SECONDS * 2 ** min(parks, 2))

    def save(self, state, version, **changes):
        result = {**state, **changes, 'updated_at': int(self.clock())}
        return result, self.store.cas(result, version)

    def tick(self):
        if not self.slot['enabled']:
            return {'status': 'disabled'}
        state, version = self.store.read()
        if state is None:
            state = {'schema_version': 1, 'phase': 'idle', 'generation': 0,
                     'updated_at': int(self.clock()), **self._binding_fields()}
            version = self.store.cas(state, None)
        for _ in range(8):
            # Active is drained under the digest it launched with. A template-only
            # change is refused until that execution is terminal; the idle or
            # blocked transition that follows is what re-keys. Any other drift
            # refuses immediately, including while the execution is still running.
            if state['phase'] == 'active' and state.get('config_sha256') != self.config_sha:
                if not self._template_only_change(state):
                    raise ControllerError('controller_config_changed')
            else:
                state, version = self._adopt_config(state, version)
            phase = state['phase']
            if phase in ('blocked', 'launch_intent', 'binding_intent', 'grant_intent'):
                # Another delivery may own this mutation, or its reply was lost.
                # Read-only reconciliation occurs separately; never repeat it.
                return {'status': phase, 'generation': state['generation']}
            if phase == 'idle':
                if self.clock() < state.get('next_launch_at', 0):
                    if state.get('error') == 'provider_quota_exhausted':
                        return {'status': 'provider_quota_parked', 'next_launch_at': state['next_launch_at'],
                                'quota_parks': state.get('quota_parks', 0), 'generation': state['generation']}
                    return {'status': 'replacement_cooldown', 'generation': state['generation']}
                job = self.job(); previous = self.current_terminal(job, state)
                release_uid = self.broker._read()[0].get('execution_uid')
                grant = secrets.token_urlsafe(48)
                state, version = self.save(state, version, phase='binding_intent',
                    generation=state['generation'] + 1, intent=uuid.uuid4().hex,
                    grant=grant, grant_sha256=hashlib.sha256(grant.encode()).hexdigest(),
                    expires_at=int(self.clock()) + 7200, previous_uid=previous['uid'], release_uid=release_uid)
                binding = self.policy.binding(state['grant_sha256'], state['expires_at'])
                self.bindings.publish(binding)
                state, version = self.save(state, version, phase='binding_ready')
                continue
            if phase == 'binding_ready':
                job = self.job(); previous = self.current_terminal(job, state)
                require(previous['uid'] == state['previous_uid'], 'another_execution_intervened')
                require(state['expires_at'] > self.clock() + 6000, 'fresh_grant_required')
                state, version = self.save(state, version, phase='launch_intent',
                                           next_launch_at=int(self.clock()) + 60)
                operation = self.cloud.run(self.policy.profile.job_name, {
                    'etag': job['etag'], 'overrides': {'containerOverrides': [{'env': [
                        {'name': 'RUNCREW_EXECUTION_GRANT', 'value': state['grant']},
                        {'name': 'RUNCREW_CONTROLLER_INTENT', 'value': state['intent']}]}]}})
                name = operation.get('name', '')
                require(re.fullmatch(r'projects/(?:496481413971|project-0c6d31fa-509e-4116-a2c)/locations/us-central1/operations/[a-zA-Z0-9_-]+', name),
                        'launch_operation_unverified')
                state, version = self.save(state, version, phase='launch_submitted', operation=name)
                continue
            if phase == 'launch_submitted':
                matches = self.cloud.executions(self.policy.profile.job_name, state['intent'])
                require(isinstance(matches, list) and len(matches) <= 1, 'ambiguous_launch')
                if not matches:
                    operation = self.cloud.get(state['operation'])
                    if operation.get('done'):
                        state, version = self.save(state, version, phase='blocked', error='launch_without_execution')
                    return {'status': state['phase'], 'generation': state['generation']}
                execution = exact_execution(self.policy, matches[0], state['intent'])
                require(not terminal(execution), 'worker_finished_before_grant')
                # Cloud Run echoes the project ID; the grant store, the broker
                # and the profiles use the project number. Store and publish the
                # canonical form, or publish is refused and the slot parks here.
                name = canonical_name(execution['name'])
                state, version = self.save(state, version, phase='grant_intent',
                    execution=name, execution_uid=execution['uid'])
                binding = self.policy.binding(state['grant_sha256'], state['expires_at'])
                self.grant_factory(binding).publish(name, execution['uid'])
                state, version = self.save(state, version, phase='active', grant=None)
                return {'status': 'job_running_readiness_separate', 'execution': state['execution'],
                        'worker_id': self.policy.profile.provider + '-live-' + state['execution_uid'].replace('-', ''),
                        'generation': state['generation']}
            if phase == 'active':
                execution = exact_execution(self.policy, self.cloud.get(state['execution']), state['intent'])
                require(execution['uid'] == state['execution_uid'], 'execution_uid_changed')
                if not terminal(execution):
                    # The slot is still mid-execution. Do not adopt the new template yet.
                    if state.get('config_sha256') != self.config_sha:
                        raise ControllerError('controller_config_changed')
                    return {'status': 'job_running_readiness_separate', 'execution': state['execution'],
                            'worker_id': self.policy.profile.provider + '-live-' + state['execution_uid'].replace('-', ''),
                            'generation': state['generation']}
                clean = (type(execution.get('succeededCount')) is int and execution['succeededCount'] == 1
                         and type(execution.get('failedCount', 0)) is int and execution.get('failedCount', 0) == 0
                         and type(execution.get('runningCount', 0)) is int and execution.get('runningCount', 0) == 0)
                if not clean:
                    # Quota is decided before the strike counter moves. A failed
                    # task read or any exit code other than 75 leaves released
                    # and failures on today's path.
                    exit_code = self._task_exit_code(state['execution'])
                    try:
                        self.idle_credential(None if self.never_bound(state, execution) else execution)
                        released = True
                    except ControllerError:
                        released = False
                    if exit_code == QUOTA_EXIT_CODE and released:
                        parks = state.get('quota_parks', 0)
                        self.store.archive(state, execution)
                        state, version = self.save(state, version, phase='idle', error='provider_quota_exhausted',
                            quota_parks=(parks if type(parks) is int and parks >= 0 else 0) + 1,
                            next_launch_at=int(self.clock()) + self._quota_park_seconds(parks),
                            last_execution=state['execution'], last_execution_uid=state['execution_uid'])
                        return {'status': 'provider_quota_parked', 'next_launch_at': state['next_launch_at'],
                                'quota_parks': state['quota_parks'], 'generation': state['generation']}
                    failures = state.get('consecutive_failures', 0) + 1
                    if not released or failures >= MAX_CONSECUTIVE_FAILURES:
                        state, version = self.save(state, version, phase='blocked', consecutive_failures=failures,
                            error='worker_failed_no_restart_loop' if released else 'worker_failed_credential_unreleased')
                        return {'status': 'blocked', 'generation': state['generation']}
                    first, cap = FAILURE_BACKOFF_SECONDS
                    self.store.archive(state, execution)
                    state, version = self.save(state, version, phase='idle', consecutive_failures=failures,
                        next_launch_at=int(self.clock()) + min(cap, first * 2 ** (failures - 1)),
                        last_execution=state['execution'], last_execution_uid=state['execution_uid'])
                    return {'status': 'replacement_after_failure', 'consecutive_failures': failures,
                            'next_launch_at': state['next_launch_at'], 'generation': state['generation']}
                self.idle_credential(execution)
                # Preserve this generation's receipt separately before replacing
                # the live state. The implementation uses immutable create-only.
                # A clean run clears a provider-quota park as well as the strikes.
                self.store.archive(state, execution)
                cleared = {key: value for key, value in state.items() if key != 'error'}
                state, version = self.save(cleared, version, phase='idle', consecutive_failures=0, quota_parks=0,
                    last_execution=state['execution'], last_execution_uid=state['execution_uid'])
                continue
            raise ControllerError('unknown_controller_state')
        return {'status': state['phase'], 'generation': state['generation']}

    STOPPED = ('blocked', 'binding_intent', 'binding_ready', 'launch_intent', 'launch_submitted', 'grant_intent')

    def reset(self):
        """Operator reconciliation of a stopped slot; launches nothing.

        A slot blocked by a failed or unverifiable execution, or left in an
        intent phase by a mutation whose reply was lost or refused (tick never
        replays an intent), returns to idle only when the same facts a launch
        requires already hold: the job's latest execution is terminal and its
        credential was cleanly released. A slot with a live execution is
        refused. The tick that follows performs the replacement with a fresh
        grant and a fresh binding; a stale binding expires on its own.
        """
        state, version = self.store.read()
        require(state is not None, 'controller_config_changed')
        # A quota park is idle, not stopped. Reset clears it so the next tick
        # can launch; any other idle slot is still slot_not_stopped.
        parked = state.get('phase') == 'idle' and state.get('error') == 'provider_quota_exhausted'
        require(state.get('phase') in self.STOPPED or parked, 'slot_not_stopped')
        # Same re-key rule as tick. Intent phases are in STOPPED but are not
        # idle or blocked, so a template change while a launch is in flight
        # still refuses here instead of resetting onto the new digest.
        state, version = self._adopt_config(state, version)
        phase = state['phase']
        previous = self.current_terminal(self.job(), state)
        state_error = state.get('error')
        # An operator reset grants a fresh failure budget and a fresh quota
        # park count. next_launch_at is removed only for a quota park; a
        # blocked slot keeps the launch interval.
        drop = {'error', 'next_launch_at'} if parked else {'error'}
        cleared = {key: value for key, value in state.items() if key not in drop}
        state, version = self.save(cleared, version, phase='idle', previous_uid=previous['uid'],
                                    consecutive_failures=0, quota_parks=0)
        cleared_code = state_error if isinstance(state_error, str) and SAFE_CODE.fullmatch(state_error) else 'unrecorded'
        return {'status': 'idle', 'cleared': cleared_code, 'from_phase': phase, 'generation': state['generation']}
