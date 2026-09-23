"""Default readiness report, or explicit no-inference cloud commissioning."""
import argparse
import json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--heartbeat-only', action='store_true')
    mode.add_argument('--once', action='store_true', help='Dispatch remains disabled')
    args = parser.parse_args(argv)
    if args.heartbeat_only:
        from heartbeat import main as heartbeat_main
        return heartbeat_main()
    print(json.dumps({'package': 'codex-linux-0.155.1', 'dispatch_enabled': False,
                     'profiles': ['ryan', 'blueeyes'],
                     'required_integration': ['durable_fenced_credential_broker',
                         'same_process_native_transport', 'room_pinned_model_plan',
                         'verified_kernel_and_secret_isolation', 'verified_plugin_isolation',
                         'verified_provider_credit_controls']}))
    return 2 if args.once else 0


if __name__ == '__main__':
    raise SystemExit(main())
