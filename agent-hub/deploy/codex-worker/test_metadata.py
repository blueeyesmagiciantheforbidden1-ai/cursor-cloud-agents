"""Synthetic stdio and refresh lifecycle tests; no Codex/network/real credentials."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('_codex_metadata_tested', ROOT / 'metadata.py')
metadata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metadata)
from credential_state import RefreshSession

EMAILS = {'ryan':'ryan@example.invalid','blueeyes':'blueeyes@example.invalid'}
OWNERS = {k:hashlib.sha256(v.encode()).hexdigest() for k,v in EMAILS.items()}


class Broker:
    def __init__(self):
        self.committed, self.released, self.quarantined, self.renewals = [], [], [], 0
        self.fail_commit = False
    def assert_current(self, lease):
        pass
    def renew(self, lease):
        self.renewals += 1
    def commit(self, lease, body):
        if self.fail_commit:
            raise TimeoutError('private synthetic failure')
        self.committed.append((lease.profile,body))
        return 'exact-version-2'
    def release(self, lease, version):
        self.released.append(version)
    def quarantine(self, lease, reason):
        self.quarantined.append(reason)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.owners = patch.dict(metadata.OWNER_REFS, OWNERS, clear=True)
        self.owners.start()
        self.addCleanup(self.owners.stop)
        self.models = [{'id':'synthetic-model','model':'synthetic-model','hidden':False,'isDefault':True,
            'defaultReasoningEffort':'high','supportedReasoningEfforts':[{'reasoningEffort':'high'},{'reasoningEffort':'ultra'}],
            'description':'PRIVATE-SYNTHETIC-DESCRIPTION','serviceTiers':[{'id':'standard','description':'PRIVATE-DROP'}]}]
        self.config = {'forced_login_method':'chatgpt','cli_auth_credentials_store':'file','model_provider':'openai',
            'approval_policy':'never','sandbox_mode':'read-only','model':'synthetic-model','model_reasoning_effort':'high',
            'private_extension_value':'PRIVATE-CONFIG-DROP'}
        self.rates = {'accountId':'synthetic-ryan-account','ordinaryUsageAllowed':False,
            'rateLimitsByLimitId':{'codex':{'limitId':'codex','primary':{'usedPercent':100,'windowDurationMins':300,'resetsAt':2000000000},
                'secondary':None,'credits':{'balance':'27.50','hasCredits':True,'unlimited':False},'private':'PRIVATE-RATE-DROP'}},
            'rateLimitResetCredits':{'availableCount':2,'credits':[{'id':'PRIVATE-RESET-ID'}]}}

    def fixture(self, profile='ryan'):
        rates = copy.deepcopy(self.rates)
        rates['accountId'] = 'synthetic-'+profile+'-account'
        return {'initialize':{},'account/read':{'account':{'type':'chatgpt','email':EMAILS[profile],'planType':'pro'},'requiresOpenaiAuth':True},
                'model/list':{'data':copy.deepcopy(self.models),'nextCursor':None},
                'account/rateLimits/read':rates,'config/read':{'config':copy.deepcopy(self.config)}}

    def session(self, root, profile='ryan'):
        broker = Broker()
        lease = SimpleNamespace(profile=profile,account_ref=OWNERS[profile],fence=1,version='exact-version-1',
            auth_bytes=b'SYNTHETIC-OPAQUE-ORIGINAL',canonical_account_ref=metadata.canonical_account_ref('synthetic-'+profile+'-account'))
        session = RefreshSession(broker,lease,root / profile)
        session.restore()
        return session

    def fake_factory(self, responses, *, refresh=None, clean=True):
        calls = []
        class Fake:
            clean_shutdown = False
            def __init__(self, home, renew):
                self.home = home
            def __enter__(self): return self
            def __exit__(self,*_): pass
            def request(self,method,params):
                calls.append((method,params))
                return copy.deepcopy(responses[method])
            def finish(self):
                if refresh:
                    (self.home / 'auth.json').write_bytes(refresh)
                self.clean_shutdown = clean
        return Fake,calls

    def test_both_named_profiles_separate_account_and_quota_pools(self):
        with tempfile.TemporaryDirectory() as directory:
            results=[]
            for profile in ('ryan','blueeyes'):
                session=self.session(Path(directory),profile)
                factory,calls=self.fake_factory(self.fixture(profile),refresh=('REFRESH-'+profile).encode())
                result=metadata.collect(session,rpc_factory=factory)
                self.assertEqual(session.broker.committed,[(profile,('REFRESH-'+profile).encode())])
                self.assertEqual(session.broker.released,['exact-version-2'])
                self.assertEqual([c[0] for c in calls],['initialize','account/read','model/list','account/rateLimits/read','config/read'])
                self.assertEqual(calls[1][1],{'refreshToken':False})
                self.assertFalse(result['quota']['ordinary_usage_allowed'])
                self.assertEqual(result['model_calls'],0)
                self.assertFalse(result['task_execution_enabled'])
                results.append(result)
            self.assertNotEqual(results[0]['account']['account_ref'],results[1]['account']['account_ref'])
            self.assertNotEqual(results[0]['quota']['pools'][0]['pool_ref'],results[1]['quota']['pools'][0]['pool_ref'])

    def test_projection_drops_email_ids_descriptions_raw_config_and_currency_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory))
            factory,_=self.fake_factory(self.fixture())
            result=metadata.collect(session,rpc_factory=factory)
        raw=json.dumps(result)
        for text in ('PRIVATE-',EMAILS['ryan'],'synthetic-ryan-account','SYNTHETIC-OPAQUE'):
            self.assertNotIn(text,raw)
        self.assertEqual(result['quota']['pools'][0]['credits']['currency'],'unknown')
        self.assertEqual(result['quota']['pools'][0]['credits']['balance_raw_units'],'27.50')
        self.assertEqual(result['quota']['reset_credits_available'],2)
        self.assertFalse(result['models'][0]['inference_entitlement_verified'])

    def test_wrong_owner_and_canonical_account_quarantine_without_release(self):
        for kind in ('owner','canonical'):
            with tempfile.TemporaryDirectory() as directory:
                session=self.session(Path(directory))
                fixture=self.fixture()
                if kind=='owner': fixture['account/read']['account']['email']=EMAILS['blueeyes']
                else: fixture['account/rateLimits/read']['accountId']='another-account'
                factory,_=self.fake_factory(fixture)
                with self.assertRaises(metadata.MetadataError): metadata.collect(session,rpc_factory=factory)
                self.assertTrue(session.broker.quarantined)
                self.assertFalse(session.broker.released)
                self.assertTrue((session.home/'auth.json').exists())

    def test_unknown_usage_is_not_zero_or_recovered_and_unknown_effort_not_ranked(self):
        self.rates['ordinaryUsageAllowed']=None
        result=metadata.rate_metadata(self.rates,metadata.canonical_account_ref(self.rates['accountId']))
        self.assertIsNone(result['ordinary_usage_allowed'])
        self.models[0]['supportedReasoningEfforts'].append({'reasoningEffort':'future-level'})
        model=metadata.model_metadata(self.models)[0]
        self.assertIsNone(model['maximum_known_effort'])
        self.assertTrue(model['effort_policy_update_required'])

    def test_duplicate_models_cursor_loops_and_bad_effective_auth_route_fail(self):
        for mutation in ('duplicate','cursor','route'):
            with tempfile.TemporaryDirectory() as directory:
                session=self.session(Path(directory))
                fixture=self.fixture()
                if mutation=='duplicate': fixture['model/list']['data']*=2
                elif mutation=='cursor': fixture['model/list']['nextCursor']='repeated'
                else: fixture['config/read']['config']['forced_login_method']='api'
                factory,_=self.fake_factory(fixture)
                with self.assertRaises(metadata.MetadataError): metadata.collect(session,rpc_factory=factory)
                self.assertFalse(session.broker.committed)

    def test_unknown_or_nonzero_exit_cannot_publish_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory))
            factory,_=self.fake_factory(self.fixture(),refresh=b'UNCERTAIN-NEW-BYTES',clean=False)
            with self.assertRaises(metadata.MetadataError): metadata.collect(session,rpc_factory=factory)
            self.assertFalse(session.broker.committed)
            self.assertEqual((session.home/'auth.json').read_bytes(),b'UNCERTAIN-NEW-BYTES')

    def test_lost_commit_retains_refreshed_file_and_never_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory))
            session.broker.fail_commit=True
            factory,_=self.fake_factory(self.fixture(),refresh=b'FRESH-UNCERTAIN-COMMIT')
            with self.assertRaises(Exception): metadata.collect(session,rpc_factory=factory)
            self.assertEqual((session.home/'auth.json').read_bytes(),b'FRESH-UNCERTAIN-COMMIT')
            self.assertEqual(session.state,'quarantined')
            self.assertFalse(session.broker.released)

    def test_environment_omits_parent_keys_proxy_grant_and_desktop_home(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'PRIVATE-KEY','HTTP_PROXY':'PRIVATE-PROXY','HUB_AGENT_TOKEN':'PRIVATE-HUB'}):
            env=metadata.environment(Path('/fresh/private'))
        self.assertEqual(set(env),{'HOME','CODEX_HOME','PATH','LANG','TMPDIR'})
        self.assertEqual(env['CODEX_HOME'],str(Path('/fresh/private')))
        self.assertNotIn('PRIVATE-',json.dumps(env))

    def test_json_bounds_duplicate_ids_and_nonfinite_values_rejected(self):
        for value in (b'{"id":1,"id":2}',b'{"id":NaN}',b'[]',b'x'*(metadata.MAX_FRAME+1)):
            with self.assertRaises(metadata.MetadataError): metadata.json_value(value)

    def test_real_synthetic_stdio_handshake_eof_refresh_writeback(self):
        responses=self.fixture()
        program='''import json,os,sys
from pathlib import Path
responses=json.loads(sys.argv[1])
home=Path(os.environ['CODEX_HOME'])
with (home/'synthetic-calls.jsonl').open('w') as observed:
 for line in sys.stdin:
  value=json.loads(line)
  observed.write(json.dumps(value)+'\\n'); observed.flush()
  if value['method']=='initialized': continue
  print(json.dumps({'id':value['id'],'result':responses[value['method']]}),flush=True)
(home/'auth.json').write_bytes(b'SYNTHETIC-REFRESH-AT-CLEAN-EOF')
sys.stderr.write('PRIVATE-SYNTHETIC-STDERR')
'''
        real_popen=subprocess.Popen
        def launch(args,**kwargs):
            self.assertEqual(args,[str(metadata.NATIVE),'app-server'])
            self.assertEqual(set(kwargs['env']),{'HOME','CODEX_HOME','PATH','LANG','TMPDIR'})
            return real_popen([sys.executable,'-u','-c',program,json.dumps(responses)],**kwargs)
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory))
            with patch.object(metadata,'verify_native'),patch.object(metadata.subprocess,'Popen',side_effect=launch):
                result=metadata.collect(session)
            calls=[json.loads(line) for line in (session.home/'synthetic-calls.jsonl').read_text().splitlines()]
            self.assertEqual([c['method'] for c in calls],['initialize','initialized','account/read','model/list','account/rateLimits/read','config/read'])
            self.assertEqual(session.broker.committed,[('ryan',b'SYNTHETIC-REFRESH-AT-CLEAN-EOF')])
            self.assertNotIn('PRIVATE-',json.dumps(result))

    def test_real_synthetic_unsolicited_request_is_never_answered(self):
        real_popen=subprocess.Popen
        program="import json,sys,time;sys.stdin.readline();print(json.dumps({'id':88,'method':'item/commandExecution/requestApproval','params':{'secret':'PRIVATE-RAW'}}),flush=True);time.sleep(10)"
        def launch(args,**kwargs): return real_popen([sys.executable,'-u','-c',program],**kwargs)
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory))
            with patch.object(metadata,'verify_native'),patch.object(metadata.subprocess,'Popen',side_effect=launch):
                with self.assertRaisesRegex(metadata.MetadataError,'^native_metadata_failed_profile_quarantined$'):
                    metadata.collect(session)
            self.assertFalse(session.broker.committed)
            self.assertTrue(session.broker.quarantined)

    def test_real_synthetic_hang_has_deadline_and_no_second_process(self):
        real_popen=subprocess.Popen
        def launch(args,**kwargs): return real_popen([sys.executable,'-u','-c','import time;time.sleep(10)'],**kwargs)
        def factory(home,renew):
            native=metadata.NativeRPC(home,renew)
            native.deadline=time.monotonic()+0.15
            return native
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory))
            started=time.monotonic()
            with patch.object(metadata,'verify_native'),patch.object(metadata.subprocess,'Popen',side_effect=launch) as popen:
                with self.assertRaises(metadata.MetadataError): metadata.collect(session,rpc_factory=factory)
            self.assertLess(time.monotonic()-started,5)
            self.assertEqual(popen.call_count,1)
            self.assertFalse(session.broker.released)

    def test_transport_has_no_thread_turn_login_or_forced_refresh_surface(self):
        native=metadata.NativeRPC.__new__(metadata.NativeRPC)
        native.stage='account'
        for method,params in (('thread/start',{}),('turn/start',{}),('account/login/start',{}),
                              ('account/rateLimitResetCredit/consume',{}),('account/read',{'refreshToken':True})):
            with self.assertRaises(metadata.MetadataError): native.request(method,params)

    def test_cloud_hook_waits_for_http_broker_and_journals_before_acquire(self):
        # Package tests remain stdlib-only, including in the original isolated
        # native build context. Actual broker HTTP/signature tests live in core.
        fake_package=ModuleType('agent_hub')
        fake_package.__path__=[]
        credential_broker_service=ModuleType('agent_hub.credential_broker_service')
        credential_broker_service.load_client_config=lambda path: None
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            broker=Broker()
            broker.config=SimpleNamespace(provider='codex',profile='blueeyes',account_ref=OWNERS['blueeyes'],
                job_name='projects/496481413971/locations/us-central1/jobs/runcrew-worker-codex-blueeyes')
            execution_id='runcrew-worker-codex-blueeyes-abcde'
            broker.execution=broker.config.job_name+'/executions/'+execution_id
            def acquire(execution,request_id):
                self.assertEqual(execution,broker.execution)
                journals=list(root.glob('metadata-blueeyes-*/acquisition.json'))
                self.assertEqual(len(journals),1)
                self.assertEqual(json.loads(journals[0].read_text())['request_id'],request_id)
                return SimpleNamespace(profile='blueeyes',account_ref=OWNERS['blueeyes'],fence=1,version='exact-version-1',
                    auth_bytes=b'SYNTHETIC-OPAQUE',canonical_account_ref=metadata.canonical_account_ref('synthetic-blueeyes-account'))
            broker.acquire=acquire
            factory,calls=self.fake_factory(self.fixture('blueeyes'),refresh=b'NEW-BLUEEYES')
            original_collect=metadata.collect
            with patch.dict(sys.modules,{'agent_hub':fake_package,
                    'agent_hub.credential_broker_service':credential_broker_service}),patch.object(metadata,'verify_native'),patch.object(metadata,'ATTEMPT_ROOT',root),patch.object(
                    credential_broker_service,'load_client_config',return_value=broker) as loader,patch.dict(
                    os.environ,{'CLOUD_RUN_EXECUTION':execution_id}),patch.object(metadata,'collect',
                    side_effect=lambda session: original_collect(session,rpc_factory=factory)):
                result=metadata.cloud_main()
            loader.assert_called_once_with(metadata.BROKER_CONFIG)
            self.assertEqual(result['account']['profile'],'blueeyes')
            self.assertEqual(broker.committed,[('blueeyes',b'NEW-BLUEEYES')])


if __name__=='__main__': unittest.main()
