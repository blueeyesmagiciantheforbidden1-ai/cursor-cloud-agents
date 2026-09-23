"""Private Google Cloud adapters for the cloud-owned live worker controller."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading
import time
from urllib.parse import quote

from agent_hub import credential_broker_service as base
from agent_hub.cloud_credential_broker import (DATABASE, GoogleREST, CloudCredentialBroker,
                                              MutationUncertain, Conflict, BrokerError)
from dynamic_broker import parse_config
from fleet_controller import Controller, ControllerError, SAFE_CODE, digest


class Google:
    def __init__(self, policy): self.rest = GoogleREST(policy.profile)

    def request(self, host, path, *, method='GET', body=None):
        try:
            status, _, raw = base.exchange(host, path, method=method,
                body=None if body is None else base.encode(body),
                headers={'Authorization': 'Bearer ' + self.rest._token(), 'Content-Type': 'application/json'})
        except Exception:
            if method == 'POST': raise MutationUncertain('cloud_mutation_uncertain') from None
            raise BrokerError('cloud_read_unavailable') from None
        if status == 404 and method == 'GET': return None
        if status in (409, 412): raise Conflict('cloud_compare_and_swap_conflict')
        if not 200 <= status < 300:
            if method == 'POST': raise MutationUncertain('cloud_mutation_uncertain')
            raise BrokerError('cloud_read_unavailable')
        try:
            return base.decode(raw)
        except BrokerError:
            if method == 'POST': raise MutationUncertain('cloud_mutation_uncertain') from None
            raise

    def get(self, name):
        base.require(re.fullmatch(r'projects/(?:496481413971|project-0c6d31fa-509e-4116-a2c)/locations/us-central1/(?:jobs|operations)/[A-Za-z0-9_-]+(?:/executions/[A-Za-z0-9_-]+)?', name),
                     'cloud_resource_not_allowed')
        value = self.request('run.googleapis.com', '/v2/' + name)
        base.require(value is not None, 'cloud_resource_missing')
        return value

    def run(self, name, body):
        base.require(name == self.rest.config.job_name, 'cloud_job_not_allowed')
        return self.request('run.googleapis.com', '/v2/' + name + ':run', method='POST', body=body)

    def executions(self, name, intent):
        base.require(name == self.rest.config.job_name and re.fullmatch('[a-f0-9]{32}', intent), 'cloud_job_not_allowed')
        value = self.request('run.googleapis.com', '/v2/' + name + '/executions?pageSize=20')
        base.require(isinstance(value, dict) and isinstance(value.get('executions', []), list), 'execution_list_invalid')
        # Never infer the execution from latestCreatedExecution. The controller
        # intent is an unguessable independent label in the exact job overrides.
        matches = []
        for execution in value.get('executions', []):
            for container in execution.get('template', {}).get('containers', []):
                if any(row == {'name': 'RUNCREW_CONTROLLER_INTENT', 'value': intent}
                       for row in container.get('env', [])):
                    matches.append(execution); break
        return matches


class StateStore:
    def __init__(self, policy, google):
        self.google = google
        self.name = DATABASE + '/documents/runcrew_fleet_state/' + policy.profile.provider + '-' + policy.profile.profile

    def read(self):
        value = self.google.request('firestore.googleapis.com', '/v1/' + self.name)
        if value is None: return None, None
        base.require(value.get('name') == self.name and value.get('updateTime')
                     and set(value.get('fields', {})) == {'state_json'}, 'controller_state_invalid')
        field = value['fields']['state_json']
        base.require(set(field) == {'stringValue'} and isinstance(field['stringValue'], str), 'controller_state_invalid')
        return base.decode(field['stringValue'].encode()), value['updateTime']

    def cas(self, state, version):
        encoded = base.encode(state).decode()
        response = self.google.request('firestore.googleapis.com', '/v1/' + DATABASE + '/documents:commit',
            method='POST', body={'writes': [{'update': {'name': self.name,
                'fields': {'state_json': {'stringValue': encoded}}},
                'currentDocument': {'exists': False} if version is None else {'updateTime': version}}]})
        writes = response.get('writeResults')
        if (not isinstance(writes, list) or len(writes) != 1 or not isinstance(writes[0], dict)
                or not writes[0].get('updateTime')):
            raise MutationUncertain('controller_state_write_uncertain')
        return writes[0]['updateTime']

    def archive(self, state, execution):
        # Never archive a raw execution grant or container environment.
        receipt = {k: v for k, v in state.items() if k != 'grant'}
        receipt['terminal'] = {k: execution.get(k) for k in
            ('name', 'uid', 'completionTime', 'succeededCount', 'failedCount', 'runningCount')}
        name = DATABASE + '/documents/runcrew_fleet_history/' + state['intent']
        body = {'writes': [{'update': {'name': name, 'fields': {
            'receipt_json': {'stringValue': base.encode(receipt).decode()}}}, 'currentDocument': {'exists': False}}]}
        try:
            response = self.google.request('firestore.googleapis.com', '/v1/' + DATABASE + '/documents:commit', method='POST', body=body)
            writes = response.get('writeResults')
            if not isinstance(writes, list) or len(writes) != 1 or not isinstance(writes[0], dict) or not writes[0].get('updateTime'):
                raise MutationUncertain('controller_archive_uncertain')
        except (Conflict, MutationUncertain):
            observed = self.google.request('firestore.googleapis.com', '/v1/' + name)
            if not isinstance(observed, dict) or observed.get('fields') != body['writes'][0]['update']['fields']:
                raise MutationUncertain('controller_archive_uncertain') from None


class Runtime:
    def __init__(self, config):
        base.require(set(config) == {'schema_version', 'audience', 'policies', 'slots'}, 'controller_config_invalid')
        _, policies = parse_config({k: config[k] for k in ('schema_version', 'audience', 'policies')})
        base.require(isinstance(config['slots'], dict) and set(config['slots']) == {p.profile.provider for p in policies},
                     'controller_slots_invalid')
        self.controllers = []
        for policy in policies:
            google = Google(policy)
            self.controllers.append(Controller(policy, config['slots'][policy.profile.provider],
                StateStore(policy, google), google, CloudCredentialBroker(policy.profile)))
        self.lock = threading.Lock()

    def tick(self):
        if not self.lock.acquire(blocking=False): return {'status': 'tick_in_progress'}
        try:
            result = {}
            for controller in self.controllers:
                name = controller.policy.profile.provider
                try:
                    result[name] = controller.tick()
                except Conflict:
                    result[name] = {'status': 'concurrent_state_changed'}
                except ControllerError as error:
                    # The controller's own fixed codes say which pre-launch
                    # check refused; without them an operator has to guess.
                    code = str(error)
                    result[name] = {'status': 'controller_attention_required',
                                    'reason': code if SAFE_CODE.fullmatch(code) else 'unrecorded'}
                except base.BrokerError as error:
                    # Broker and grant-store refusals carry fixed codes too.
                    code = str(error)
                    result[name] = {'status': 'controller_attention_required', 'exception': type(error).__name__,
                                    'reason': code if SAFE_CODE.fullmatch(code) else 'unrecorded'}
                except Exception as error:
                    # No raw exception, execution environment, grant, token or
                    # provider credential is ever returned or logged.
                    result[name] = {'status': 'controller_attention_required', 'exception': type(error).__name__}
            print(json.dumps({'kind': 'runcrew_fleet_tick', 'workers': result}), flush=True)
            return result
        finally:
            self.lock.release()

    def reset(self, slot):
        """Operator unblock of one slot, "provider" or "provider/profile"; serialized with tick."""
        provider, _, profile = slot.partition('/')
        matches = [c for c in self.controllers if c.policy.profile.provider == provider
                   and (not profile or c.policy.profile.profile == profile)]
        if len(matches) != 1: return {'status': 'unknown_slot' if not matches else 'ambiguous_slot'}
        if not self.lock.acquire(blocking=False): return {'status': 'tick_in_progress'}
        try:
            try:
                result = matches[0].reset()
            except Conflict:
                result = {'status': 'concurrent_state_changed'}
            except ControllerError as error:
                # Fixed codes only; the operator needs to know why a reset was refused.
                result = {'status': 'reset_refused', 'reason': str(error)}
            except base.BrokerError as error:
                code = str(error)
                result = {'status': 'reset_refused', 'exception': type(error).__name__,
                          'reason': code if SAFE_CODE.fullmatch(code) else 'unrecorded'}
            except Exception as error:
                result = {'status': 'controller_attention_required', 'exception': type(error).__name__}
            print(json.dumps({'kind': 'runcrew_fleet_reset', 'slot': slot, 'result': result}), flush=True)
            return result
        finally:
            self.lock.release()


def main():
    base.require(os.name == 'posix' and os.geteuid() != 0 and os.environ.get('K_SERVICE')
                 and os.environ.get('K_REVISION'), 'cloud_rootless_service_required')
    runtime = Runtime(base.read_protected('/run/config/fleet.json'))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            if self.path not in ('/tick', '/reset'): self.send_error(404); return
            if self.headers.get('Transfer-Encoding'): self.send_error(400); return
            count = self.headers.get('Content-Length', '0')
            if not re.fullmatch(r'[0-9]{1,3}', count): self.send_error(400); return
            raw = self.rfile.read(int(count)) if int(count) else b''
            if self.path == '/tick':
                if raw not in (b'', b'{}'): self.send_error(400); return
                body = json.dumps(runtime.tick()).encode()
            else:
                # Operator unblock, overriding the spend-safety stop. It exists
                # only while the operator has RUNCREW_RESET_ENABLED=1 set on the
                # service for the maintenance window; /tick alone never unblocks.
                if os.environ.get('RUNCREW_RESET_ENABLED') != '1': self.send_error(404); return
                # Body is {"slot": "<provider>"} or {"slot": "<provider>/<profile>"} and nothing else.
                try: request = json.loads(raw or b'{}')
                except ValueError: self.send_error(400); return
                if (not isinstance(request, dict) or set(request) != {'slot'} or not isinstance(request['slot'], str)
                        or not re.fullmatch(r'[a-z]{1,16}(/[a-z0-9-]{1,32})?', request['slot'])):
                    self.send_error(400); return
                body = json.dumps(runtime.reset(request['slot'])).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    port = os.environ.get('PORT', '8080')
    base.require(re.fullmatch(r'[0-9]{1,5}', port) and 1024 <= int(port) <= 65535, 'listen_port_invalid')
    # Optional self-ticking for a min-instances=1 service when Cloud Scheduler is not enabled.
    # The tick lock makes an overlapping scheduler delivery a no-op, never a duplicate launch.
    interval = os.environ.get('RUNCREW_TICK_SECONDS', '')
    if interval:
        base.require(re.fullmatch(r'[0-9]{2,4}', interval) and 30 <= int(interval) <= 3600, 'tick_interval_invalid')
        def self_tick():
            while True:
                time.sleep(int(interval))
                try:
                    runtime.tick()
                except Exception:
                    print('{"kind":"runcrew_fleet_tick","status":"tick_failed"}', flush=True)
        threading.Thread(target=self_tick, daemon=True).start()
    with ThreadingHTTPServer(('0.0.0.0', int(port)), Handler) as server: server.serve_forever()


if __name__ == '__main__': main()
