"""Live-only authorization around the unchanged fenced credential broker.

The protected configuration allowlists identities and profiles. An independent
cloud controller publishes an immutable, expiring grant for each exact Job
execution. Workers cannot publish grants or choose a different profile.
The original commissioning broker and its static bindings remain unchanged.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import os
import re
import time

from agent_hub import credential_broker_service as base
from agent_hub.cloud_credential_broker import (CloudCredentialBroker, ProfileConfig, GoogleREST,
    BrokerError, Conflict, MutationUncertain, UpstreamUnavailable, UPSTREAM_BUDGET_SECONDS,
    UPSTREAM_HTTP_STATUSES, DATABASE)


@dataclass(frozen=True)
class Policy:
    profile: ProfileConfig
    caller_subject: str
    caller_service_account: str
    lease_seconds: int = 240

    def __post_init__(self):
        base.Binding(self.profile, self.caller_subject, self.caller_service_account,
                     '0' * 64, 1, self.lease_seconds)

    def binding(self, grant_sha256, expires_at):
        return base.Binding(self.profile, self.caller_subject, self.caller_service_account,
                            grant_sha256, expires_at, self.lease_seconds)


def parse_config(value):
    base.require(isinstance(value, dict) and set(value) == {'schema_version', 'audience', 'policies'}
                 and type(value['schema_version']) is int and value['schema_version'] == 1,
                 'protected_config_invalid')
    base.endpoint_host(value['audience'])
    rows = value['policies']
    base.require(isinstance(rows, list) and 1 <= len(rows) <= 6, 'protected_config_invalid')
    result = []
    for item in rows:
        base.require(isinstance(item, dict) and set(item) ==
                     {'profile', 'caller_subject', 'caller_service_account', 'lease_seconds'},
                     'protected_config_invalid')
        result.append(Policy(**{**item, 'profile': ProfileConfig(**item['profile'])}))
    base.require(len({p.profile for p in result}) == len(result)
                 and len({p.caller_subject for p in result}) == len(result), 'ambiguous_live_policy')
    return value['audience'], tuple(result)


class BindingStore:
    """Read-only in the HTTP broker; create-only in the controller."""
    def __init__(self, policy, *, rest=None, clock=time.time):
        self.policy, self.clock = policy, clock
        self.rest = rest or GoogleREST(policy.profile)

    def name(self, digest):
        base.require(isinstance(digest, str) and base.SHA.fullmatch(digest), 'execution_not_authorized')
        return DATABASE + '/documents/runcrew_live_bindings/' + digest

    def exchange(self, method, path, value=None):
        try:
            status, _, raw = base.exchange('firestore.googleapis.com', '/v1/' + path,
                method=method, body=None if value is None else base.encode(value),
                headers={'Authorization': 'Bearer ' + self.rest._token(), 'Content-Type': 'application/json'},
                timeout_seconds=UPSTREAM_BUDGET_SECONDS)
        except Exception:
            if method == 'POST':
                raise MutationUncertain('live_binding_publish_uncertain') from None
            raise UpstreamUnavailable('live_binding_read_unavailable') from None
        if status == 404 and method == 'GET':
            return None
        if status in (409, 412):
            if method == 'POST':
                # Lost acknowledgement of a create-only publish still resolves
                # by the existing strong read. A 409/412 is not transport loss.
                raise MutationUncertain('live_binding_publish_uncertain')
            raise Conflict('live_binding_compare_and_swap_conflict')
        if status in UPSTREAM_HTTP_STATUSES:
            if method == 'POST':
                raise MutationUncertain('live_binding_publish_uncertain')
            raise UpstreamUnavailable('live_binding_read_unavailable')
        if not 200 <= status < 300:
            if method == 'POST':
                raise MutationUncertain('live_binding_publish_uncertain')
            raise BrokerError('live_binding_read_unavailable')
        try:
            return base.decode(raw)
        except BrokerError:
            if method == 'POST':
                raise MutationUncertain('live_binding_publish_uncertain') from None
            raise UpstreamUnavailable('live_binding_read_unavailable') from None

    def read(self, digest):
        document = self.name(digest)
        result = self.exchange('GET', document)
        if result is None:
            return None
        base.require(result.get('name') == document and isinstance(result.get('fields'), dict)
                     and set(result['fields']) == {'binding_json'}, 'execution_not_authorized')
        field = result['fields']['binding_json']
        base.require(isinstance(field, dict) and set(field) == {'stringValue'}, 'execution_not_authorized')
        raw = base.decode(field['stringValue'].encode())
        expected_keys = {'profile', 'caller_subject', 'caller_service_account',
                         'lease_seconds', 'grant_sha256', 'expires_at'}
        base.require(set(raw) == expected_keys, 'execution_not_authorized')
        binding = self.policy.binding(digest, raw['expires_at'])
        base.require(raw == asdict(binding), 'execution_not_authorized')
        base.require(self.clock() < binding.expires_at <= self.clock() + 86400, 'execution_grant_expired')
        return binding

    def publish(self, binding):
        base.require(isinstance(binding, base.Binding) and
                     binding == self.policy.binding(binding.grant_sha256, binding.expires_at),
                     'execution_not_authorized')
        base.require(self.clock() < binding.expires_at <= self.clock() + 86400, 'execution_grant_expired')
        document = self.name(binding.grant_sha256)
        body = {'writes': [{'update': {'name': document, 'fields': {
            'binding_json': {'stringValue': base.encode(asdict(binding)).decode()}}},
            'currentDocument': {'exists': False}}]}
        try:
            response = self.exchange('POST', DATABASE + '/documents:commit', body)
            writes = response.get('writeResults')
            if (not isinstance(writes, list) or len(writes) != 1
                    or not isinstance(writes[0], dict) or not writes[0].get('updateTime')):
                raise MutationUncertain('live_binding_publish_uncertain')
        except MutationUncertain:
            try:
                observed = self.read(binding.grant_sha256)
            except BrokerError:
                raise MutationUncertain('live_binding_publish_uncertain') from None
            if observed != binding:
                raise MutationUncertain('live_binding_publish_uncertain') from None
        return binding


class LiveBrokerService(base.BrokerService):
    def __init__(self, audience, policies, *, authenticator=None,
                 broker_factory=CloudCredentialBroker, binding_store_factory=BindingStore,
                 grant_store_factory=base.ExecutionGrantStore, clock=time.time):
        base.endpoint_host(audience)
        base.require(isinstance(policies, tuple) and policies and all(isinstance(p, Policy) for p in policies),
                     'protected_config_invalid')
        self.clock = clock
        self.authenticator = authenticator or base.GoogleIDAuthenticator(audience, clock=clock)
        self.policies = policies
        self.brokers = {p: broker_factory(p.profile, lease_seconds=p.lease_seconds) for p in policies}
        self.stores = {p: binding_store_factory(p) for p in policies}
        self.grant_store_factory = grant_store_factory

    def _authorize(self, authorization, grant):
        caller = self.authenticator(authorization)
        base.require(isinstance(caller, dict), 'authentication_required')
        base.require(isinstance(grant, str) and base.GRANT.fullmatch(grant), 'execution_not_authorized')
        matches = [p for p in self.policies if caller.get('subject') == p.caller_subject and
                   caller.get('service_account') == p.caller_service_account]
        base.require(len(matches) == 1, 'execution_not_authorized')
        policy = matches[0]
        digest = hashlib.sha256(grant.encode('ascii')).hexdigest()
        binding = self.stores[policy].read(digest)
        base.require(binding is not None, 'execution_not_authorized')
        base.require(binding == policy.binding(digest, binding.expires_at), 'execution_not_authorized')
        base.require(self.clock() < binding.expires_at <= self.clock() + 86400, 'execution_grant_expired')
        return binding, self.brokers[policy]

    def _resolve(self, binding, broker):
        active = self.grant_store_factory(binding).read()
        if active is None:
            return None
        base.require(isinstance(active, base.ActiveBinding) and active.policy == binding,
                     'execution_not_authorized')
        observed = broker._execution_status(active.execution)
        base.require(observed.get('uid') == active.execution_uid and
                     observed.get('template', {}).get('serviceAccount') == binding.caller_service_account
                     and not observed.get('completionTime') and not observed.get('deleteTime'),
                     'execution_not_authorized')
        return active


def load_client(path):
    value = base.read_protected(path)
    base.require(set(value) == {'schema_version', 'endpoint', 'profile', 'bootstrap_timeout_seconds'}
                 and type(value['schema_version']) is int and value['schema_version'] == 2,
                 'protected_client_config_invalid')
    profile = ProfileConfig(**value['profile'])
    execution = os.environ.get('CLOUD_RUN_EXECUTION', '')
    base.require(re.fullmatch(re.escape(profile.job_id) + r'-[a-z0-9]{5,20}', execution),
                 'cloud_execution_environment_required')
    # The controller supplies only this execution-specific capability. Provider
    # credentials never pass through job overrides. Remove it from inherited env.
    grant = os.environ.pop('RUNCREW_EXECUTION_GRANT', '')
    client = base.BrokerHTTPClient(profile, endpoint=value['endpoint'],
        execution=profile.job_name + '/executions/' + execution, grant=grant)
    client.bootstrap(timeout_seconds=value['bootstrap_timeout_seconds'])
    return client


def main():
    base.require(os.name == 'posix' and os.geteuid() != 0 and os.environ.get('K_SERVICE')
                 and os.environ.get('K_REVISION'), 'cloud_rootless_service_required')
    audience, policies = parse_config(base.read_protected('/run/config/live-broker.json'))
    port = os.environ.get('PORT', '8080')
    base.require(re.fullmatch(r'[0-9]{1,5}', port) and 1024 <= int(port) <= 65535, 'listen_port_invalid')
    with base.BrokerServer(('0.0.0.0', int(port)), LiveBrokerService(audience, policies)) as server:
        server.serve_forever()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('{"status":"stopped","error":"live_broker_startup_rejected"}', flush=True)
        raise SystemExit(1)
