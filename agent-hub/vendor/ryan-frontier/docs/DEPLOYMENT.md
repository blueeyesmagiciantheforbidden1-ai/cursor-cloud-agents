# Connecting the implementation to two VPS workers

The delivered program runs locally on Linux/POSIX with Python 3.11 or newer and no third-party runtime dependencies. The current session did not expose the user's computer, Desktop/API_KEYS, existing command-center repository, or either VPS. No remote software or account configuration was changed, and no paid model was invoked by the delivered program.

## Run the actual implementation

From the extracted project directory:

```bash
python3 -m unittest discover -s tests -v
python3 -m ryan_frontier demo --output runs/first
python3 -m ryan_frontier pilot --units 8 --budget 6 --output runs/pilot
python3 -m ryan_frontier route --config examples/providers.example.json
```

The last command deliberately abstains until provider capabilities and integrations have been verified. Placeholders are not working provider registrations. Capacities of 100 in the template are illustrative normalization units, not actual subscription quotas.

## Deployment contracts to implement next

Keep the SQLite controller and promotion registry on one host. Workers must use a controller API; never share the database over a network filesystem. API transport, worker authentication, heartbeats, remote dispatch, and provider invocation adapters are NOT delivered in this prototype. The methods below are the concrete API boundary for that next integration:

| Controller operation | Worker request | Required response and enforcement |
|---|---|---|
| Create task | Typed payload + idempotency key | Stable task identity; conflicting payload rejected |
| Claim task | Task ID + authenticated worker identity | Monotonic fence + expiry |
| Reserve invocation | Task ID, live fence, invocation ID, worst-case microdollar reservation | Dispatch allowed exactly once |
| Report ambiguous call | Invocation ID | Reservation remains held |
| Reconcile | Trusted billing evidence + actual cost | Idempotent settlement, including late charges |
| Complete | Task ID, live fence, artifact hashes | One accepted completion, settled accounting |

Worker A can perform proposal generation and builds. Worker B can independently verify proposed artifacts and collect reproducible traces. This is a logical role assignment, not evidence that either VPS has been reached. No public HTTP listener is included.

When wiring transport, add per-worker credentials, TLS or an SSH tunnel, bounded request sizes, cancellation, concurrency limits, and durable outbox dispatch. Establish a separate supervisor identity for accounting reconciliation. Validate stale-fence behavior with dropped requests and restarts before accepting production jobs.

## Existing subscriptions and highest effort

Use `examples/providers.example.json` as the capability register. For each installed provider app, verify its supported integration, exact current model identifier, actual effort controls, account entitlement, and observable usage. Record the selected strongest model and its maximum supported reasoning setting explicitly; the router cannot independently prove a vendor model is globally best. A provider that lacks an effort control must be represented as such in a reviewed integration, not given an invented setting.

Normal development routing balances normalized eligible allowance usage, subject to capability constraints. It excludes the ChatGPT connection reserve unless explicitly included. Provider cash budgets and subscription allowances are separate pools. Experiments freeze the complete routing configuration hash to avoid attributing provider changes to a research method.

The router is a planner: it neither opens applications nor converts subscription entitlements into API credentials. Production quota reservation must move into the controller's transactional authority; the router's supplied usage snapshot is not a distributed billing ledger.

## Alibaba deployment

Alibaba AI Gateway can be an API aggregation layer for approved API integrations. Subscription-funded native coding-agent workflows stay separately integrated through whatever supported interface each product offers. An API gateway does not combine subscription allowances or weights from different proprietary models.

Choose exact region, models, upstream protocols, availability, account authorization, and prices from current provider documentation and the user's account. Do not inherit the attachment's illustrative model IDs, blanket regional claims, or gateway pricing as deployment facts. See `RESEARCH_SOURCES.md` for checked primary documentation and restrictions.

## Protected execution boundary

The prototype accepts a closed workflow policy grammar and generates Python from trusted templates. It does not run arbitrary model-provided Python, shell commands, repository patches, or network calls. The finite grammar is the current execution boundary. The promotion registry explicitly assumes it is called by a trusted controller.

Before adding arbitrary-code candidates, place them in a separate non-root worker/container or VM with no controller database, evaluator holdout data, account credentials, or host Docker socket. Mount task inputs read-only and collect proposed changes as artifacts. External effects require a controller-issued operation grant bound to a task fence and reservation. A Python process boundary by itself is not a security sandbox.

## Operational acceptance before real deployment

1. Run the delivered tests and reproduce the example report.
2. Inventory the actual two VPS machines and supported provider interfaces.
3. Implement and fault-test transport, invocation dispatch, and usage reconciliation.
4. Run one paid provider call within a deliberately small explicit budget.
5. Run a repository-specific read-only diagnosis followed by an isolated candidate patch.
6. Hold deployment until independent repository checks and exact-version approval succeed.

The task's requested local implementation is complete independently of those future infrastructure integrations. Production deployment still requires access and those engineering additions.
