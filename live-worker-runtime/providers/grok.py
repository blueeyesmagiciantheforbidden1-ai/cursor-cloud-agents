"""Warm, single-task Grok subscription adapter for explicit project prompts.

No commissioning room, browser receipt or automatic task claim is imported;
broker exception types come only through broker_renew. The caller owns the
exact execution grant, task journal and budgets.
Deadlines are absolute time.monotonic() values. Call maintain() during idle wait
and close() on every drain. This first route denies all tools and improvement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import math
import os
import re
import time
from uuid import uuid4

import broker_renew
from ._grok_protocol import Native, NativeError, NativeStartupStopped, need, unwrap

MODEL = 'grok-4.7'
EFFORT = 'xhigh'
CLI_NAME = 'runcrew-live-grok'
CLI_VERSION = '1'
TOOLS_POLICY = 'deny_all_and_abort_on_observed_tool'
OWNER = 'cursor-owner@example.invalid'
# Keep this binding identical to the enrolled profile (never derived from a prompt).
ACCOUNT_REF = '9ddbfe0cce4b6653b86b2057f45c398360541f21a100c1589a67a01cbc80aadc'
EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
MAX_PROMPT_BYTES = 200000
MAX_ANSWER_BYTES = 15000
# Hub release 3 marks usage stale 900 s after observed_at; refresh sooner.
QUOTA_REFRESH_SECONDS = 600
# Swallowed validation failures back off so maintain() does not re-request every poll.
QUOTA_RETRY_SECONDS = 120
# Idle refresh temporarily replaces the prepare-era native.deadline.
QUOTA_REFRESH_DEADLINE_SECONDS = 30
NativeProcess = Native


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _deadline(value):
    need(type(value) in (int, float) and math.isfinite(value) and value > time.monotonic(),
         'grok_deadline_invalid')
    return value


def _stamp(value):
    need(type(value) is str and len(value) < 64, 'grok_period_shape')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise NativeError('grok_period_shape') from None
    need(parsed.tzinfo is not None, 'grok_period_timezone')
    return parsed.timestamp()


def _cents(value):
    if value is None:
        return None
    need(type(value) is dict, 'grok_money_shape')
    amount = value.get('val', 0)  # Official Cent serde default: {} means zero.
    if type(amount) is str and re.fullmatch(r'[0-9]{1,16}', amount):
        amount = int(amount)
    need(type(amount) is int and 0 <= amount <= 10**16, 'grok_money_value')
    return amount


def _owner(value):
    value = unwrap(value)
    need(value.get('methodId') == 'cached_token' and type(value.get('email')) is str
         and value['email'].lower() == OWNER, 'grok_owner_mismatch')
    need(not value.get('userBlockedReason') and not value.get('teamBlockedReasons'), 'grok_account_blocked')
    return {'intended_account_ref': ACCOUNT_REF, 'native_owner_verified': True,
            'auth_method': 'cached_token', 'api_fallback': False}


def _catalog(value):
    value = unwrap(value)
    rows = value.get('availableModels')
    need(type(rows) is list and 0 < len(rows) <= 1000 and all(type(row) is dict for row in rows),
         'grok_catalog_shape')
    matching = [row for row in rows if row.get('modelId') == MODEL]
    need(len(matching) == 1, 'grok_reviewed_model_unavailable')
    meta = matching[0].get('_meta')
    need(type(meta) is dict and meta.get('supportsReasoningEffort') is True, 'grok_effort_support_required')
    options = meta.get('reasoningEfforts')
    need(type(options) is list and 0 < len(options) <= 16, 'grok_effort_catalog_required')
    efforts = []
    for option in options:
        effort = option if type(option) is str else option.get('value') if type(option) is dict else None
        need(effort in EFFORTS, 'grok_unreviewed_effort')
        efforts.append(effort)
    need(max(efforts, key=EFFORTS.index) == EFFORT, 'grok_reviewed_maximum_effort_changed')
    return {'source': 'native_authenticated_acp', 'model': MODEL,
            'reviewed_maximum_effort': EFFORT, 'account_eligible_catalog': True,
            'reasoning_efforts': efforts}


def _billing(value, topup):
    value, topup = unwrap(value), unwrap(topup)
    config = value.get('config')
    need(type(config) is dict, 'grok_billing_config_required')
    cap, prepaid = _cents(config.get('onDemandCap')), _cents(config.get('prepaidBalance'))
    need(cap == 0 and prepaid == 0, 'grok_native_zero_spend_required')
    need('raw' not in topup and ('rule' in topup or topup == {}), 'grok_topup_shape')
    rule = topup.get('rule')
    need(rule is None or type(rule) is dict, 'grok_topup_shape')
    # Official AutoTopupRule serde default explicitly decodes omitted enabled as false.
    enabled = False if rule is None else rule.get('enabled', False)
    need(type(enabled) is bool and enabled is False, 'grok_topup_must_be_disabled')
    on_demand = value.get('on_demand_enabled')
    need(on_demand is None or type(on_demand) is bool, 'grok_on_demand_flag_shape')
    period = config.get('currentPeriod')
    if period is None:
        period = {'start': config.get('billingPeriodStart'), 'end': config.get('billingPeriodEnd')}
    need(type(period) is dict, 'grok_period_shape')
    safe_period = {key: period.get(key) for key in ('start', 'end')}
    need(_stamp(safe_period['start']) <= time.time() < _stamp(safe_period['end']), 'grok_period_expired')
    percentage = config.get('creditUsagePercent')
    usage_source = 'native_percentage'
    if percentage is None:
        limit, used = _cents(config.get('monthlyLimit')), _cents(config.get('used'))
        if limit is None and used is None:
            usage_source = 'unavailable'
        else:
            need(type(limit) is int and limit > 0 and type(used) is int, 'grok_legacy_usage_conflict')
            percentage = 100 * used / limit
            usage_source = 'native_legacy_cents'
    if percentage is not None:
        need(type(percentage) in (int, float) and math.isfinite(percentage) and 0 <= percentage <= 100,
             'grok_usage_shape')
        need(percentage < 100, 'grok_included_allowance_exhausted')
    tier = value.get('subscription_tier')
    need(tier is None or type(tier) is str and tier.lower().replace(' ', '_') == 'supergrok_heavy',
         'grok_subscription_tier_conflict')
    return {'source': 'native_authenticated_acp', 'observed_at': _utc(),
            'subscription_tier': tier, 'subscription_tier_status': 'unavailable' if tier is None else 'available',
            'native_included_used_percent': percentage,
            'native_usage_status': 'unavailable' if percentage is None else 'available',
            'usage_source': usage_source, 'on_demand_cap_cents': cap, 'prepaid_balance_cents': prepaid,
            'on_demand_enabled': on_demand, 'on_demand_used_cents': _cents(config.get('onDemandUsed')),
            'auto_topup_enabled': enabled, 'period': safe_period,
            'project_work_authorized_with_unknown_usage': percentage is None,
            'automatic_improvement_ready': False}


def _metadata_responses(native):
    """Issue every metadata RPC. Transport errors propagate to the caller."""
    return (
        native.request('x.ai/auth/info', {}),
        native.request('x.ai/models/list', {}),
        native.request('x.ai/billing', {}),
        native.request('x.ai/auto-topup-rule', {}),
    )


def _metadata_preflight(auth, models, billing, topup):
    """Validate already-fetched metadata responses into a preflight dict."""
    account = _owner(auth)
    catalog = _catalog(models)
    quota = _billing(billing, topup)
    return {'account': account, 'catalog': catalog, 'quota': quota,
            'same_process_account_model_billing': True,
            'same_process_account_model_quota': quota['native_usage_status'] == 'available'}


def _fresh_metadata(native):
    return _metadata_preflight(*_metadata_responses(native))


def _home(session):
    for name in ('work', 'tmp'):
        (session.home / name).mkdir(mode=0o700)
    path = session.home / '.grok' / 'config.toml'
    config = '''[auth]
preferred_method="oidc"
[cli]
auto_update=false
[models]
max_retries=0
allowed_models=["grok-4.7"]
[subagents]
enabled=false
[memory]
enabled=false
[ui]
permission_mode="ask"
prompt_suggestions=false
[permission]
rules=[{action="deny",tool="any"}]
[features]
web_fetch=false
write_file=false
tool_search=false
lsp_tools=false
'''
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(config)
        stream.flush()
        os.fsync(stream.fileno())


@dataclass
class Handle:
    session: object = field(repr=False)
    heartbeat: object = field(repr=False)
    lease_clock: object = field(default=None, repr=False)
    native: object = field(default=None, repr=False)
    sid: str = field(default='', repr=False)
    preflight: dict = field(default_factory=dict)
    attempted: bool = False
    stopped_proven: bool = True
    finished: bool = False
    close_failed: bool = False
    credential_version: str = field(default='', repr=False)
    internal_acks: dict = field(default_factory=dict)
    next_quota_refresh: float = 0

    @property
    def readiness(self):
        live = bool(self.sid) and self.native is not None and not self.finished and not self.close_failed
        return {'provider': 'grok', 'authenticated': live,
                'ready_for_project_prompt': live and not self.attempted,
                'model': MODEL, 'effort': EFFORT, 'preflight': self.preflight,
                'tools_enabled': False, 'workspace_access': False, 'full_coding_ready': False,
                'automatic_improvement_ready': False}


def _renew(handle):
    renewed = handle.lease_clock.renew(handle.session.broker, handle.session.lease)
    need(handle.heartbeat() is True, 'grok_hub_heartbeat_lost')
    return renewed


def close(handle):
    """Stop all native descendants before commit/release; idempotent after success."""
    if handle.finished:
        return handle.credential_version
    need(not handle.close_failed, 'grok_close_requires_reconciliation')
    try:
        if handle.native is not None:
            handle.internal_acks = dict(getattr(handle.native, 'internal_ack_counts', {}))
            handle.native.close()
            handle.stopped_proven = True
            handle.native = None
        need(handle.stopped_proven, 'grok_native_stop_unconfirmed')
        handle.credential_version = handle.session.finish(native_stopped=True)
        handle.finished = True
        return handle.credential_version
    except Exception:
        handle.close_failed = True
        try:
            handle.session.broker.quarantine(handle.session.lease, 'provider_refresh_uncertain')
        except Exception:
            pass  # Existing non-idle owner state still prohibits takeover.
        raise


def prepare(session, heartbeat, deadline):
    """Authenticate and create one warm, model/effort-verified headless session."""
    _deadline(deadline)
    need(session.state == 'active' and session.lease.account_ref == ACCOUNT_REF, 'grok_owner_lease_required')
    handle = Handle(session=session, heartbeat=heartbeat,
                    lease_clock=broker_renew.LeaseClock.for_lease(session.lease, NativeError, 'grok'))
    try:
        session.broker.assert_current(session.lease)
        _home(session)
        handle.stopped_proven = False
        try:
            handle.native = NativeProcess(session.home, lambda: _renew(handle), deadline)
        except NativeStartupStopped:
            handle.stopped_proven = True
            raise
        init = handle.native.request('initialize', {'protocolVersion': 1,
            'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False},
            'clientInfo': {'name': 'runcrew-live-grok', 'version': '1'}})
        methods = init.get('authMethods')
        need(type(methods) is list and all(type(row) is dict for row in methods)
             and any(row.get('id') == 'cached_token' for row in methods)
             and not any(row.get('id') == 'xai.api_key' for row in methods), 'grok_cached_subscription_auth_required')
        handle.native.request('authenticate', {'methodId': 'cached_token', '_meta': {'headless': True}})
        handle.preflight = _fresh_metadata(handle.native)
        handle.next_quota_refresh = time.monotonic() + QUOTA_REFRESH_SECONDS
        created = handle.native.request('session/new', {'cwd': str(session.home / 'work'), 'mcpServers': [],
            '_meta': {'sessionKind': 'headless', 'modelId': MODEL, 'reasoningEffort': EFFORT}})
        need(type(created) is dict and type(created.get('sessionId')) is str and bool(created['sessionId']),
             'grok_session_identity_required')
        models = created.get('models')
        need(type(models) is dict and models.get('currentModelId') == MODEL, 'grok_selected_model_mismatch')
        rows = models.get('availableModels')
        need(type(rows) is list and all(type(row) is dict for row in rows), 'grok_session_models_shape')
        selected = [row for row in rows if row.get('modelId') == MODEL]
        need(len(selected) == 1 and type(selected[0].get('_meta')) is dict
             and selected[0]['_meta'].get('reasoningEffort') == EFFORT, 'grok_selected_effort_mismatch')
        handle.sid = created['sessionId']
        need(heartbeat() is True, 'grok_hub_heartbeat_lost')
        return handle
    except Exception:
        if not handle.finished and not handle.close_failed:
            close(handle)
        raise


# Intact error / missing-result frames from Native.request: id matched and the
# frame was consumed; the stream stays in sync. native_rpc_<int> and
# native_rpc_failed come from rpc_error_code(); native_result_missing from need().
_GROK_QUOTA_REFRESH_SWALLOW = frozenset({
    'grok_quota_exhausted', 'native_rpc_failed', 'native_result_missing',
})


def _grok_quota_refresh_swallowed(error):
    code = str(error) if isinstance(error, NativeError) else ''
    if code in _GROK_QUOTA_REFRESH_SWALLOW:
        return True
    # native_rpc_<int> from rpc_error_code when error.code is an int.
    return code.startswith('native_rpc_') and code[len('native_rpc_'):].isdigit()


def _refresh_quota(handle):
    """Re-measure quota for hub reports. Validation failures keep the last real row.

    Intact error frames and native_result_missing are swallowed like validation
    failures (stream still sync). Everything else from request()/send() is
    transport lost and drains via grok_quota_refresh_transport_lost.
    native_extension_failed surfaces in unwrap() during validation and is
    swallowed by the inner try. The prepare-era native.deadline is replaced
    for the refresh window and restored after.
    """
    if time.monotonic() < handle.next_quota_refresh:
        return
    native = handle.native
    old_deadline = native.deadline
    native.deadline = time.monotonic() + QUOTA_REFRESH_DEADLINE_SECONDS
    try:
        try:
            responses = _metadata_responses(native)
        except Exception as error:
            if _grok_quota_refresh_swallowed(error):
                handle.next_quota_refresh = time.monotonic() + QUOTA_RETRY_SECONDS
                return
            raise NativeError('grok_quota_refresh_transport_lost') from None
        try:
            handle.preflight = _metadata_preflight(*responses)
            handle.next_quota_refresh = time.monotonic() + QUOTA_REFRESH_SECONDS
        except Exception:
            # Keep last real observed_at; back off so a persistent validation
            # miss (including native_extension_failed) does not re-request
            # on every idle poll.
            handle.next_quota_refresh = time.monotonic() + QUOTA_RETRY_SECONDS
    finally:
        native.deadline = old_deadline


def maintain(handle):
    """Call during idle polling. The lease is 240s; transient renew failures are tolerated for 180s."""
    need(not handle.finished and not handle.attempted and not handle.close_failed and handle.native is not None, 'grok_handle_not_idle')
    need(handle.native.process.poll() is None, 'grok_warm_process_ended')
    if time.monotonic() >= handle.native.next_renew:
        handle.native.next_renew = broker_renew.next_due(_renew(handle), 20)
    _refresh_quota(handle)
    return handle.readiness


def _answer(native, result, sid, nonce):
    need(type(result) is dict and result.get('stopReason') == 'end_turn', 'grok_task_incomplete')
    meta = result.get('_meta')
    need(type(meta) is dict and meta.get('modelId') == MODEL and meta.get('sessionId') == sid
         and meta.get('promptId') == nonce and meta.get('requestId') == nonce, 'grok_answer_identity_mismatch')
    need('reasoningEffort' not in meta or meta['reasoningEffort'] == EFFORT, 'grok_served_effort_mismatch')
    chunks = []
    for event in native.notifications:
        params = event.get('params', {})
        if type(params) is not dict or params.get('sessionId') != sid:
            continue
        update = params.get('update', {})
        need(type(update) is dict, 'grok_update_shape')
        if update.get('sessionUpdate') in ('model_changed', 'modelChanged'):
            need(update.get('modelId', update.get('model_id')) in (None, MODEL), 'grok_model_changed')
        if update.get('sessionUpdate') == 'agent_message_chunk':
            tag = params.get('_meta')
            if type(tag) is not dict or tag.get('promptId') != nonce or tag.get('isReplay', False) is not False:
                continue
            content = update.get('content')
            need(type(content) is dict and content.get('type') == 'text' and type(content.get('text')) is str,
                 'grok_answer_chunk_shape')
            chunks.append(content['text'])
    text = ''.join(chunks).strip()
    need(0 < len(text.encode()) <= MAX_ANSWER_BYTES, 'grok_correlated_answer_missing_or_large')
    usage = {}
    for key in ('inputTokens', 'outputTokens', 'reasoningTokens', 'cachedReadTokens'):
        value = meta.get(key)
        if value is not None:
            need(type(value) is int and 0 <= value <= 10**9, 'grok_usage_counter_shape')
            usage[key] = value
    return text, usage


def execute(handle, prompt, task_deadline, *, task_kind='project'):
    """One explicit project prompt, followed by confirmed stop and writeback."""
    need(not handle.finished and not handle.attempted and handle.native is not None, 'grok_task_replay_forbidden')
    try:
        need(task_kind == 'project', 'grok_automatic_improvement_not_enabled')
        need(type(prompt) is str and 0 < len(prompt.encode()) <= MAX_PROMPT_BYTES, 'grok_prompt_limit')
        handle.native.deadline = _deadline(task_deadline)
        handle.preflight = _fresh_metadata(handle.native)  # Never reuse stale idle-time quota.
        if handle.lease_clock.degraded:  # never send the prompt on an unconfirmed lease
            handle.lease_clock.renew(handle.session.broker, handle.session.lease, strict=True)
            handle.native.next_renew = time.monotonic() + 20
        need(handle.heartbeat() is True, 'grok_hub_heartbeat_lost')
        nonce = str(uuid4())
        handle.native.notifications.clear()
        handle.attempted = True
        result = handle.native.request('session/prompt', {'sessionId': handle.sid,
            'prompt': [{'type': 'text', 'text': prompt}], '_meta': {'verbatim': True, 'promptId': nonce}})
        text, usage = _answer(handle.native, result, handle.sid, nonce)
        version = close(handle)
        need(set(handle.internal_acks) <= {'skills-reload', 'workflows-reload'}
             and all(type(value) is int and 1 <= value <= 8 for value in handle.internal_acks.values())
             and sum(handle.internal_acks.values()) <= 8, 'grok_acknowledgement_count_shape')
        return {'text': text, 'provider': 'grok', 'model': MODEL, 'effort': EFFORT, 'usage': usage,
                'preflight': handle.preflight, 'same_process_account_model_quota': handle.preflight['same_process_account_model_quota'],
                'review_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                'prompt_nonce_sha256': hashlib.sha256(nonce.encode()).hexdigest(),
                'native_session_ref_sha256': hashlib.sha256(handle.sid.encode()).hexdigest(),
                'prompt_correlation': 'matching_final_response_and_non_replay_text_chunks',
                'prompt_sent_once_by_wrapper': True, 'automatic_retry': False,
                'native_internal_reload_acks': handle.internal_acks,
                'tools_policy': 'deny_all_and_abort_on_observed_tool', 'full_coding_ready': False,
                'actual_charge': 'unverified', 'native_stopped': True, 'credential_writeback': 'committed',
                'credential_version_ref': hashlib.sha256(version.encode()).hexdigest()}
    except Exception:
        if not handle.finished and not handle.close_failed:
            close(handle)
        raise
