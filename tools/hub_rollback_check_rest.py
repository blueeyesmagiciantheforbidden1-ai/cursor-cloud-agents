"""Read-only pre-rollback check for hub release 2, standard library only.

Lists the rooms a rollback from release 2 (runcrew 9bea79b) to 977f8ab would
strand: 977f8ab claims only `queued` rooms and answers 409 to complete and
retry on the four statuses release 2 added. It is the REST equivalent of
runcrew's tools/release2_rollback_check.py for hosts without the Firestore
client library or application-default credentials.

It sends only GET requests to firestore.googleapis.com, walks every page of
the rooms collection, and asks Firestore for three fields per room (a field
mask), so no prompt, message or lease data is read. The bearer token comes
from the environment and is never printed.

    set FIRESTORE_BEARER_TOKEN=<gcloud auth print-access-token for the owner>
    python tools/hub_rollback_check_rest.py --database runcrew-hub

The live hub uses database runcrew-hub (HUB_FIRESTORE_DATABASE), so the flag
is required: reading an empty (default) database would pass falsely. An empty
collection is therefore an error (exit 2), not a clean result.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HOST = 'https://firestore.googleapis.com/v1/'
STRANDED = ('needs_reconciliation', 'blocked_on_provider', 'expired', 'retry_scheduled')
FIELDS = ('status', 'next_agent', 'updated_at')
PAGE_SIZE = 300
MAX_PAGES = 1000
_NAME = re.compile(r'[a-z][a-z0-9-]{0,62}|\(default\)')


def _value(field):
    """Firestore REST typed value -> plain value for the fields this reads."""
    if not isinstance(field, dict):
        return None
    for kind in ('stringValue', 'timestampValue'):
        if kind in field:
            return field[kind]
    if 'integerValue' in field:
        return int(field['integerValue'])
    if 'doubleValue' in field:
        return float(field['doubleValue'])
    return None


def _get(url, token, opener):
    request = urllib.request.Request(url, method='GET', headers={'Authorization': 'Bearer ' + token})
    with opener(request, timeout=60) as response:
        return json.loads(response.read().decode('utf-8'))


def scan(project, database, collection, token, opener=urllib.request.urlopen):
    base = (HOST + 'projects/' + urllib.parse.quote(project, safe='') + '/databases/'
            + urllib.parse.quote(database, safe='()') + '/documents/' + urllib.parse.quote(collection, safe=''))
    query = [('pageSize', str(PAGE_SIZE))] + [('mask.fieldPaths', field) for field in FIELDS]
    scanned, stranded, token_page = 0, [], None
    for _ in range(MAX_PAGES):
        params = query + ([('pageToken', token_page)] if token_page else [])
        page = _get(base + '?' + urllib.parse.urlencode(params), token, opener)
        for document in page.get('documents', []):
            scanned += 1
            fields = document.get('fields', {})
            status = _value(fields.get('status'))
            if status in STRANDED:
                stranded.append({'id': document.get('name', '').rsplit('/', 1)[-1], 'status': status,
                                 'next_agent': _value(fields.get('next_agent')),
                                 'updated_at': _value(fields.get('updated_at'))})
        token_page = page.get('nextPageToken')
        if not token_page:
            return scanned, stranded
    raise SystemExit('more than %d pages; refusing to report a partial scan' % MAX_PAGES)


def main(argv=None, opener=urllib.request.urlopen, environ=os.environ, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', default='project-0c6d31fa-509e-4116-a2c')
    parser.add_argument('--database', required=True, help='the hub database; live: runcrew-hub')
    parser.add_argument('--collection', default='agent_hub_rooms')
    args = parser.parse_args(argv)
    for value in (args.database, args.collection):
        if not _NAME.fullmatch(value) and not re.fullmatch(r'[A-Za-z0-9_]{1,100}', value):
            parser.error('invalid database or collection name')
    token = environ.get('FIRESTORE_BEARER_TOKEN', '').strip()
    if not token or any(c.isspace() for c in token):
        parser.error('set FIRESTORE_BEARER_TOKEN to an access token for the owner (never printed)')
    try:
        scanned, stranded = scan(args.project, args.database, args.collection, token, opener)
    except urllib.error.HTTPError as error:
        print(json.dumps({'error': 'http_%d' % error.code}), file=out)
        return 1
    except (urllib.error.URLError, OSError, ValueError):
        print(json.dumps({'error': 'request_failed'}), file=out)
        return 1
    print(json.dumps({'database': args.database, 'collection': args.collection, 'scanned': scanned,
                      'stranded_statuses': list(STRANDED), 'stranded': stranded}, indent=1), file=out)
    if scanned == 0:
        print(json.dumps({'error': 'empty_collection_wrong_database_or_collection'}), file=out)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
