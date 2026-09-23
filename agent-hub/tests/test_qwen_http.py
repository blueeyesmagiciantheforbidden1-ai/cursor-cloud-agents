import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import Mock
from agent_hub.core import AGENTS, Hub
from agent_hub.server import ThreadingHTTPServer, make_handler
from agent_hub.store import SQLiteStore


class QwenHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tokens = {actor: actor+'x'*40 for actor in ('manager','status',*AGENTS)}
        self.adapter = Mock()
        self.adapter.profile.public.return_value = {'model':'test'}
        self.adapter.complete.return_value = {'status':'completed','text':'test reply'}
        self.hub = Hub(SQLiteStore(Path(self.temp.name)/'hub.sqlite3'))
        self.server = ThreadingHTTPServer(('127.0.0.1',0), make_handler(
            self.hub, self.tokens, qwen=self.adapter))
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, actor, data=None, *, path='/v1/qwen/completions'):
        call = Request(self.url+path, json.dumps(data).encode() if data is not None else None,
                       headers={'X-Hub-Token':self.tokens[actor],'Content-Type':'application/json'})
        try:
            with urlopen(call, timeout=5) as result:
                return result.status,json.load(result)
        except HTTPError as error:
            with error:
                return error.code,json.load(error)

    def test_only_manager_can_authorize_provider_dispatch(self):
        request = {'invocation_id':'sample-1','messages':[{'role':'user','content':'test'}],'mode':'hybrid'}
        for actor in ('status',*AGENTS):
            self.assertEqual(self.request(actor,request)[0],403)
        self.adapter.complete.assert_not_called()
        code, value = self.request('manager',request)
        self.assertEqual((code,value['text']),(200,'test reply'))
        self.adapter.complete.assert_called_once_with(request['messages'],invocation_id='sample-1',mode='hybrid')

    def test_caller_cannot_override_price_endpoint_key_or_limit(self):
        for extra in ('api_key','endpoint','max_tokens','daily_limit_microusd'):
            request = {'invocation_id':'sample-1','messages':[],'mode':'hybrid',extra:'forbidden'}
            self.assertEqual(self.request('manager',request)[0],400)
        self.adapter.complete.assert_not_called()

    def test_fixed_provider_errors_do_not_echo_credentials_or_exception_context(self):
        from agent_hub.qwen import QwenError
        self.adapter.complete.side_effect = QwenError('provider_http_error')
        code,value = self.request('manager', {'invocation_id':'sample-2','messages':[],'mode':'hybrid'})
        self.assertEqual(code,409)
        self.assertEqual(value,{'error':'Qwen request stopped: provider_http_error',
                                'invocation_id':None, 'charge_status':'not_dispatched'})

    def test_uncertain_charge_is_explicit_without_private_settlement(self):
        from agent_hub.qwen import QwenError
        self.adapter.complete.side_effect = QwenError(
            'reconciliation_failed', invocation_id='sample-uncertain', charge_status='uncertain',
            settlement={'private_provider_diagnostic':'secret-fixture-must-not-escape'})
        code,value = self.request('manager', {
            'invocation_id':'sample-uncertain','messages':[],'mode':'hybrid'})
        self.assertEqual(code,409)
        self.assertEqual(value,{'error':'Qwen request stopped: reconciliation_failed',
                                'invocation_id':'sample-uncertain', 'charge_status':'uncertain'})
        self.assertNotIn('secret-fixture-must-not-escape',json.dumps(value))

    def test_unconfigured_connection_refuses_dispatch_and_reports_honest_status(self):
        self.server.RequestHandlerClass = make_handler(self.hub, self.tokens)
        code,value = self.request('manager', {
            'invocation_id':'not-configured','messages':[{'role':'user','content':'test'}],'mode':'hybrid'})
        self.assertEqual((code,value),(503,{'error':'Qwen account connection is not configured'}))
        self.adapter.complete.assert_not_called()
        for actor in ('manager','status'):
            with self.subTest(actor=actor):
                code,value = self.request(actor,path='/v1/status')
                self.assertEqual(code,200)
                self.assertEqual(value['qwen'],{'configured':False,'connection_verified':False,'profile':None})

    def test_configured_status_does_not_claim_verified_connection_or_dispatch(self):
        code,value = self.request('status',path='/v1/status')
        self.assertEqual(code,200)
        self.assertEqual(value['qwen'],{
            'configured':True,'connection_verified':False,'profile':{'model':'test'}})
        self.adapter.complete.assert_not_called()


if __name__ == '__main__':
    unittest.main()
