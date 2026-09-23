"""Offline review reproductions only; no network or provider imports."""
import json
from test_live_loop import LoopTests


def main():
    factory = LoopTests()
    worker, client, adapter, _ = factory.setup_worker()
    client.fail_completions = 3
    result = worker.run()
    print(json.dumps({'case': 'lost_success_ack', 'outcome': result['outcome'],
        'completion_exit_codes': [x['exit_code'] for x in client.completions],
        'distinct_completion_payloads': len({json.dumps(x, sort_keys=True) for x in client.completions})}))

    worker, client, adapter, _ = factory.setup_worker()
    client.room['purpose'] = 'improvement'
    original = client.post
    observed = []
    def post(path, value):
        if path.endswith('/complete'):
            observed.append({'closed_before_complete': 'close' in adapter.calls,
                             'exit_code': value['exit_code']})
        return original(path, value)
    client.post = post
    result = worker.run()
    print(json.dumps({'case': 'policy_rejected_after_claim', 'outcome': result['outcome'],
                      'completion_observations': observed}))


if __name__ == '__main__':
    main()
