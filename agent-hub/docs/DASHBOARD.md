# Cloud-only operations dashboard

The dashboard monitors the Google Cloud hub. Personal-PC applications, processes, memory, CPU, disk space, saved collaboration receipts, and local account snapshots are not cloud health data and are excluded from its status projection and interface.

`Monitor` accepts agent, room, alert, and provider-usage telemetry only when the authenticated hub status endpoint identifies its deployment as `cloud`. A local simulator or missing cloud connection produces `not_cloud`/`not_connected` and no substitute agent or usage values. A preview is labeled **Preview only**. A dashboard running with Cloud Run's `K_SERVICE` reports its hosting location separately from the hub connection; hosting the website does not prove that the hub is connected.

The authenticated cloud `/v1/status` response is the source for worker heartbeats, provider authentication observations, current rooms, queue counts, alerts, and usage. Worker registration and provider authentication remain distinct signals. The dashboard does not infer worker deployment location from a desktop process. Cloud worker placement and access must be controlled by deployment and IAM configuration.

Cloud Run CPU, memory, instance counts, and request counts remain **Not connected** because the Cloud Monitoring adapter is not implemented yet. Project creation, a billing-account selection, or a deployment plan is not evidence that a running Cloud Run service exists. The page must not turn those preparations into healthy infrastructure metrics.

The previous local `enrich` callback is accepted temporarily for launcher compatibility but is never executed. Preview launchers should remove `LocalCollector` wiring entirely. A preview can point at the actual HTTPS cloud hub using the limited status credential and supported Google identity authentication; its location remains explicitly a preview. No local machine scan is needed to serve the page.

When the cloud hub becomes unreachable, the last successful cloud snapshot can be retained with an explicit stale marker. A missing initial connection keeps values unknown. Provider account allowance percentages and monetary credits are separate measurements; unsupported balances remain unavailable.

Production still requires a private Cloud Run service, direct IAP, verified signed IAP assertions, an HTTPS `HUB_URL`, a limited `HUB_STATUS_TOKEN`, and metadata identity authentication. The browser never receives the hub credential, prompts, or task results. See [deployment instructions](../deploy/DASHBOARD.md) for infrastructure configuration; this document records monitoring behavior rather than a deployment claim.

Run `python -m unittest discover -s tests -p test_dashboard.py -v`. The tests verify rejection of local telemetry, exclusion of personal-machine fields, nonexecution of legacy enrichment, honest missing cloud metrics, IAP signature validation, and existing HTTP/cache boundaries. Browser JavaScript is served at `/app.js` from `agent_hub/web/cloud.js`.
