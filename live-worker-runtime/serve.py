"""Controller image entrypoint: RUNCREW_ROLE selects the live broker or the fleet controller.

Both read only their root-owned protected config under /run/config and run as the
rootless service identity. --check verifies imports without any network call and
does not import google-auth, so the stdlib fleet image stays green. --check-broker
proves the broker image can verify an RS256 token offline.
"""
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), '/opt/runcrew/app']

ROLES = {'broker': ('dynamic_broker', '/run/config/live-broker.json'),
         'fleet': ('cloud_runtime', '/run/config/fleet.json')}


def main():
    if sys.argv[1:] == ['--check']:
        import dynamic_broker, cloud_runtime, fleet_controller  # noqa: F401
        print(json.dumps({'status': 'image_ok', 'roles': sorted(ROLES), 'credentials_included': False}), flush=True)
        return 0
    if sys.argv[1:] == ['--check-broker']:
        import broker_auth_check
        broker_auth_check.check()
        print(json.dumps({'status': 'broker_auth_ok'}), flush=True)
        return 0
    if sys.argv[1:]:
        raise RuntimeError('unsupported_arguments')
    role = os.environ.get('RUNCREW_ROLE', '')
    if role not in ROLES:
        raise RuntimeError('runcrew_role_required')
    module_name, config = ROLES[role]
    if not Path(config).is_file():
        raise RuntimeError('protected_config_missing')
    module = __import__(module_name)
    module.main()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'error': str(error)[:120]}), flush=True)
        raise SystemExit(1)
