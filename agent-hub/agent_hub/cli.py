"""Manager CLI; credentials are read from environment or a private local file."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .core import AGENTS, DEFAULT_AGENTS
from .worker import NoRedirect


def request(args, method, path, data=None):
    parsed = urlsplit(args.url)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment or (parsed.scheme != 'https'
            and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')))):
        raise ValueError('Use HTTPS except for loopback local testing')
    token = os.environ.get(args.token_env)
    if not token:
        raise ValueError(f'Set {args.token_env} before calling the hub')
    headers = {'X-Hub-Token': token, 'Content-Type': 'application/json'}
    if args.cloud_run_auth:
        result = subprocess.run([args.gcloud, 'auth', 'print-identity-token'], capture_output=True, text=True, timeout=30)
        if result.returncode or not result.stdout.strip():
            raise ValueError('Could not obtain Cloud Run identity token; check local Google login and invoker role')
        headers['Authorization'] = 'Bearer ' + result.stdout.strip()
    body = json.dumps(data).encode() if data is not None else None
    call = Request(args.url.rstrip('/') + path, data=body, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(call, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            message = json.loads(exc.read(10000)).get('error', f'HTTP {exc.code}')
        except (ValueError, AttributeError):
            message = f'HTTP {exc.code}; verify Cloud Run invoker access and hub credentials'
        raise ValueError(message) from None
    except URLError:
        raise ValueError('Cannot reach hub; check URL and connectivity') from None


def init(args):
    destination = Path(args.output_dir).expanduser().resolve()
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError('The workspace must already exist')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Choose an empty private configuration directory')
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'nt':
        # Explicit argv, only the newly selected directory is changed.
        username = os.environ.get('USERNAME')
        domain = os.environ.get('USERDOMAIN')
        if not username or not domain:
            raise ValueError('Cannot identify Windows user for private credential directory')
        principal = domain + '\\' + username
        secured = subprocess.run(['icacls', str(destination), '/inheritance:r', '/grant:r', principal + ':(OI)(CI)F'], capture_output=True)
        if secured.returncode:
            raise ValueError('Could not restrict configuration folder to the current Windows user; no tokens written')
    tokens = {name: secrets.token_urlsafe(32) for name in ('manager', 'status', *AGENTS)}
    token_file = destination / 'hub-tokens.json'
    with token_file.open('x', encoding='utf-8') as handle:
        json.dump(tokens, handle)
    token_file.chmod(0o600)
    for name in AGENTS:
        config = dict(hub_url=args.url, agent_id=name, token_env='HUB_' + name.upper() + '_TOKEN',
                      workspaces={'default': str(workspace)}, cloud_run_auth=args.cloud_run_auth,
                      timeout_seconds=300, poll_seconds=3,
                      billing_policy={'mode': 'subscription_only', 'allow_cursor_user_key': False,
                                      'max_evidence_age_seconds': 300})
        (destination / f'{name}.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
    return {'configuration_directory': str(destination), 'tokens_file': str(token_file),
            'note': ('Tokens were written privately and were not printed. Copy only each agent token to its worker. '
                     'New workers require subscription authentication preflight, which is not implemented yet; '
                     'provider execution remains blocked until that check is available.')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('HUB_URL', 'http://127.0.0.1:8080'))
    parser.add_argument('--token-env', default='HUB_MANAGER_TOKEN')
    parser.add_argument('--cloud-run-auth', action='store_true')
    parser.add_argument('--gcloud', default='gcloud')
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('init')
    setup.add_argument('--output-dir', required=True)
    setup.add_argument('--workspace', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--tokens-file', required=True)
    serve.add_argument('--database', default='agent-hub.sqlite3')
    start = commands.add_parser('start')
    source = start.add_mutually_exclusive_group(required=True)
    source.add_argument('--prompt')
    source.add_argument('--prompt-file')
    start.add_argument('--agents', nargs='+', choices=AGENTS, default=list(DEFAULT_AGENTS))
    start.add_argument('--rounds', type=int, default=1)
    start.add_argument('--workspace', default='default')
    start.add_argument('--timeout', type=int, default=300)
    commands.add_parser('list')
    commands.add_parser('status')
    for operation in ('show', 'cancel', 'retry'):
        child = commands.add_parser(operation)
        child.add_argument('room_id')
    args = parser.parse_args()
    try:
        if args.command == 'init':
            result = init(args)
        elif args.command == 'serve':
            os.environ['HUB_TOKENS_JSON'] = Path(args.tokens_file).read_text(encoding='utf-8')
            os.environ['HUB_SQLITE_PATH'] = str(Path(args.database).resolve())
            from .server import main as serve_main
            serve_main()
            return
        elif args.command == 'start':
            prompt = Path(args.prompt_file).read_text(encoding='utf-8') if args.prompt_file else args.prompt
            result = request(args, 'POST', '/v1/rooms', dict(prompt=prompt, agents=args.agents,
                             rounds=args.rounds, workspace=args.workspace, timeout_seconds=args.timeout))
        elif args.command == 'list':
            result = request(args, 'GET', '/v1/rooms')
        elif args.command == 'status':
            result = request(args, 'GET', '/v1/status')
        else:
            if len(args.room_id) != 32 or any(c not in '0123456789abcdef' for c in args.room_id):
                raise ValueError('Invalid collaboration ID')
            path = '/v1/rooms/' + args.room_id
            if args.command == 'show':
                result = request(args, 'GET', path)
            else:
                result = request(args, 'POST', path + '/' + args.command, {})
        print(json.dumps(result, indent=2, ensure_ascii=True))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        # Token values are never inserted into exception text by this module.
        print(f'Error: {exc}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
