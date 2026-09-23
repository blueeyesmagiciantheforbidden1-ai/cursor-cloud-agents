"""Compatibility/security regressions, all offline and synthetic."""
import unittest
import metadata

class PassiveNotifications(unittest.TestCase):
    def rpc(self):
        value=metadata.NativeRPC.__new__(metadata.NativeRPC);value.notifications=0
        return value
    def test_new_passive_schema_is_retained_without_success_claim(self):
        rpc=self.rpc()
        values=[{'method':'configWarning','params':{'summary':'synthetic-warning'}},
                {'method':'warning','params':{'message':'synthetic-warning'}},
                {'method':'future/status','params':['new','schema',3]},
                {'method':'futureReady'}]
        for v in values:rpc._notification(v)
        self.assertEqual(rpc.passive_notifications,values)
        self.assertFalse(hasattr(rpc,'authenticated'))
        self.assertFalse(hasattr(rpc,'metadata_verified'))
    def test_passive_request_or_work_is_not_executed_or_accepted(self):
        for value in ({'id':7,'method':'future/status','params':{}},
                      {'method':'item/toolCall','params':{}},
                      {'method':'turn/completed','params':{}},
                      {'method':'thread/started','params':{}},
                      {'method':'process/started','params':{}},
                      {'method':'fs/changed','params':{}},
                      {'method':'command/started','params':{}}):
            with self.subTest(value=value),self.assertRaises(metadata.MetadataError):self.rpc()._notification(value)
    def test_auth_route_change_is_not_hidden_by_forward_compatibility(self):
        for mode in ('apiKey',None,'chatgptAuthTokens'):
            with self.subTest(mode=mode),self.assertRaises(metadata.MetadataError):
                self.rpc()._notification({'method':'account/updated','params':{'authMode':mode}})
        self.rpc()._notification({'method':'account/updated','params':{'authMode':'chatgpt'}})
    def test_future_notifications_remain_bounded(self):
        rpc=self.rpc()
        for _ in range(64):rpc._notification({'method':'future/status'})
        with self.assertRaisesRegex(metadata.MetadataError,'native_notification_limit'):
            rpc._notification({'method':'future/status'})

if __name__=='__main__':unittest.main()
