"""Offline tests for hub_rollback_check_rest.py. No network."""
import io
import json
from pathlib import Path
import sys
import unittest
import urllib.error
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hub_rollback_check_rest as check  # noqa: E402

TOKEN = 'ya29.SECRET_TEST_TOKEN_VALUE'


def room(room_id, status, agent='claude', updated=1700000000.5):
    return {'name': 'projects/p/databases/runcrew-hub/documents/agent_hub_rooms/' + room_id,
            'fields': {'status': {'stringValue': status},
                       'next_agent': {'stringValue': agent} if agent else {'nullValue': None},
                       'updated_at': {'doubleValue': updated}}}


class FakeFirestore:
    def __init__(self, pages):
        self.pages, self.requests = pages, []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        index = int(query.get('pageToken', ['0'])[0])
        body = dict(self.pages[index])
        if index + 1 < len(self.pages):
            body['nextPageToken'] = str(index + 1)

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        return Response(json.dumps(body).encode())


class RollbackCheckTests(unittest.TestCase):
    def run_check(self, pages, argv=('--database', 'runcrew-hub')):
        fake, out = FakeFirestore(pages), io.StringIO()
        code = check.main(list(argv), opener=fake, environ={'FIRESTORE_BEARER_TOKEN': TOKEN}, out=out)
        return code, out.getvalue(), fake

    def test_walks_every_page_and_lists_only_stranded_rooms(self):
        pages = [{'documents': [room('a' * 32, 'queued'), room('b' * 32, 'needs_reconciliation')]},
                 {'documents': [room('c' * 32, 'completed')]},
                 {'documents': [room('d' * 32, 'blocked_on_provider', agent=None), room('e' * 32, 'expired'),
                                room('f' * 32, 'retry_scheduled'), room('0' * 32, 'stalled')]}]
        code, out, fake = self.run_check(pages)
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report['scanned'], 7)
        self.assertEqual([item['id'][0] for item in report['stranded']], ['b', 'd', 'e', 'f'])
        self.assertIsNone(report['stranded'][1]['next_agent'])
        self.assertEqual(len(fake.requests), 3)

    def test_read_only_masked_and_token_never_printed(self):
        code, out, fake = self.run_check([{'documents': [room('a' * 32, 'queued')]}])
        self.assertEqual(code, 0)
        self.assertNotIn(TOKEN, out)
        for request in fake.requests:
            self.assertEqual(request.get_method(), 'GET')
            self.assertTrue(request.full_url.startswith(
                'https://firestore.googleapis.com/v1/projects/project-0c6d31fa-509e-4116-a2c/'
                'databases/runcrew-hub/documents/agent_hub_rooms?'))
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            self.assertEqual(sorted(query['mask.fieldPaths']), ['next_agent', 'status', 'updated_at'])
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TOKEN)

    def test_empty_collection_is_an_error_not_a_clean_pass(self):
        code, out, _ = self.run_check([{}])
        self.assertEqual(code, 2)
        self.assertIn('empty_collection_wrong_database_or_collection', out)

    def test_database_is_required_and_token_must_be_set(self):
        with self.assertRaises(SystemExit):
            check.main([], opener=FakeFirestore([{}]), environ={'FIRESTORE_BEARER_TOKEN': TOKEN}, out=io.StringIO())
        with self.assertRaises(SystemExit):
            check.main(['--database', 'runcrew-hub'], opener=FakeFirestore([{}]), environ={}, out=io.StringIO())

    def test_http_error_reports_only_the_status(self):
        def denied(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, 'denied ' + TOKEN, {}, None)
        out = io.StringIO()
        code = check.main(['--database', 'runcrew-hub'], opener=denied,
                          environ={'FIRESTORE_BEARER_TOKEN': TOKEN}, out=out)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue()), {'error': 'http_403'})


if __name__ == '__main__':
    unittest.main()
