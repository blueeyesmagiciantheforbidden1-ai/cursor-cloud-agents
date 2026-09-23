"""Produce refreshed worker image packs from a base pack set plus this checkout.

The five worker images are built from packs (Dockerfile, cloudbuild.json,
agent_hub/, live/) kept outside git, because two image tests and the grok
adapter carry the operator's provider-owner email, which this public
repository replaces with a placeholder. This tool copies a base pack,
replaces its live/ Python with the files from this checkout, restores the
owner pin from --owner-email, and retags cloudbuild.json. The Dockerfile
and agent_hub/ come from the base pack unchanged. Nothing is built or
pushed here.

    python live-image-workers/refresh_packs.py --packs C:\\API_KEYS\\cloud-agent-online \\
        --base-version live-20260922b --version live-20260923a --owner-email <operator owner email> \\
        --out C:\\API_KEYS\\cloud-agent-online

Then, from each output pack, under the deployer identity:
    gcloud builds submit --config cloudbuild.json --gcs-source-staging-dir=gs://<project>_cloudbuild/source .
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'live-worker-runtime'
PROVIDERS = ('claude', 'codex', 'copilot', 'cursor', 'grok')
PLACEHOLDER = 'cursor-owner@example.invalid'
COMMON = ('live_loop.py', 'provider_errors.py', 'broker_renew.py', 'entrypoint.py', 'dynamic_broker.py',
          'test_dynamic_broker.py', 'test_live_loop.py')
# Files whose placeholder line is the operator's owner pin in the built image.
PINNED = {'providers/grok.py', 'tests/test_codex.py', 'tests/test_cursor.py'}


def copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def live_files(provider):
    files = {name: RUNTIME / name for name in COMMON}
    files['providers/__init__.py'] = RUNTIME / 'providers' / '__init__.py'
    files['providers/' + provider + '.py'] = RUNTIME / 'providers' / (provider + '.py')
    files['tests/__init__.py'] = RUNTIME / 'tests' / '__init__.py'
    if provider == 'grok':
        files['providers/_grok_protocol.py'] = RUNTIME / 'providers' / '_grok_protocol.py'
    if provider == 'copilot':
        files['test_copilot_provider.py'] = RUNTIME / 'test_copilot_provider.py'
    else:
        files['tests/test_' + provider + '.py'] = RUNTIME / 'tests' / ('test_' + provider + '.py')
    if provider in ('claude', 'cursor'):
        files['credential_state.py'] = RUNTIME / ('credential_state_' + provider + '.py')
    return files


def refresh(provider, packs, base_version, version, owner_email, out):
    base = packs / f'live-image-{provider}-{base_version}-source'
    target = out / f'live-image-{provider}-{version}-source'
    if not (base / 'Dockerfile').is_file() or not (base / 'live').is_dir():
        raise SystemExit(f'{provider}: base pack {base} is incomplete')
    if target.exists():
        raise SystemExit(f'{provider}: {target} already exists; refusing to overwrite a pack')
    shutil.copytree(base, target, ignore=shutil.ignore_patterns('__pycache__'))
    live = target / 'live'
    for name, source in live_files(provider).items():
        if not source.is_file():
            raise SystemExit(f'{provider}: missing {source}')
        copy(source, live / name)
        if name in PINNED:
            text = (live / name).read_text(encoding='utf-8')
            if PLACEHOLDER not in text:
                raise SystemExit(f'{provider}: {name} no longer carries the placeholder; check the pin by hand')
            (live / name).write_text(text.replace(PLACEHOLDER, owner_email), encoding='utf-8', newline='\n')
    if provider == 'cursor':
        shutil.rmtree(live / 'cursor_native', ignore_errors=True)
        shutil.copytree(RUNTIME / 'cursor_native', live / 'cursor_native', ignore=shutil.ignore_patterns('__pycache__'))
    build = target / 'cloudbuild.json'
    config = json.loads(build.read_text(encoding='utf-8'))
    text = json.dumps(config, separators=(',', ':')).replace(':' + base_version, ':' + version)
    if text.count(':' + version) != 2:
        raise SystemExit(f'{provider}: cloudbuild.json tag did not retag cleanly')
    build.write_text(text + '\n', encoding='utf-8', newline='\n')
    for stale in live.rglob('__pycache__'):
        shutil.rmtree(stale, ignore_errors=True)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--packs', required=True, type=Path)
    parser.add_argument('--base-version', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--owner-email', required=True)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--providers', nargs='+', default=list(PROVIDERS), choices=PROVIDERS)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'live-[0-9]{8}[a-z]', args.version) or args.version == args.base_version:
        raise SystemExit('version must look like live-YYYYMMDDx and differ from the base')
    if '@' not in args.owner_email or PLACEHOLDER == args.owner_email:
        raise SystemExit('owner-email must be the operator owner email')
    out = args.out or args.packs
    for provider in args.providers:
        target = refresh(provider, args.packs, args.base_version, args.version, args.owner_email, out)
        print(json.dumps({'provider': provider, 'pack': str(target), 'tag': f'{provider}-worker:{args.version}'}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
