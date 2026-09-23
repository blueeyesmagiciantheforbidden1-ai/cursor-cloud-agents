# Separate private status website

This runbook deploys `agent-dashboard` as a **separate Cloud Run service in the new isolated organization**. It is prepared source, not evidence of deployment. Complete the new organization, billing, target checks, hub deployment, and image build in [README.md](README.md) first. Existing organization `833782852711` remains forbidden.

The browser receives a bounded status projection: worker health, provider authentication observations, room counts, alerts, and reported usage. It never receives the hub credential, prompts, agent results, or a worker's configuration. Refresh is every 15 seconds while the tab is visible. Failure preserves the last snapshot with an explicit stale warning. Unavailable credits remain unknown; a heartbeat is not proof of provider authentication. Alerts appear in this website; email, push, and SMS delivery are not implemented.

## Local preview

The current development launcher also supplies a read-only local collector. It reads process **image names only**, known executable paths, CPU/RAM/disk counters, explicitly selected collaboration receipts and an explicit usage snapshot file. It does not read process command lines, environment variables, provider credential stores or arbitrary user files. Executable detection currently includes the verified installed Claude/Copilot versions; after upgrades it may show "Not detected" until those paths are updated. A running process is evidence of app presence only.

Local diagnostics refresh independently of hub reachability and retain their own observation time. Codex account quota is a timestamped snapshot from the Codex app's official usage tool, not a continuously polled provider feed. It becomes stale after 15 minutes or when its reported window resets. Extra purchased credits and included subscription allowance remain distinct. Other provider balances are unavailable until a supported collector is connected.

These local observations are **not yet a deployed cloud worker telemetry feed**. Cloud workers already report basic heartbeat/task/auth observations through their authenticated worker endpoint. Migrating the richer collector requires a dedicated worker observation adapter; never run it inside Cloud Run and label container readings as Windows VPS measurements.

Run from the source directory with Python 3.12 and the project requirements installed. With the local hub running, set `HUB_URL` to its loopback origin and `HUB_STATUS_TOKEN` to the distinct `status` token from your private configuration, using an environment variable or a protected launcher. Then run:

```powershell
python -m agent_hub.dashboard --port 8090
```

Open `http://127.0.0.1:8090`. Local mode binds only to loopback. Without a token the page deliberately shows an unconfigured connection; it does not generate sample balances or fake connected agents. Local-only `HUB_MANAGER_TOKEN` fallback exists for compatibility, but the deployed website requires the limited `status` principal.

## Deploy with a separate service identity

Continue in the PowerShell session from the main runbook; it defines `Invoke-HubGcloud`, `Assert-HubTarget`, `$Operator`, `$HubProject`, `$Region`, `$Service`, `$Image`, `$HubUrl`, `$PrivateFolder`, and `$TokenFile`. The generated hub token map must contain a unique `status` token and the deployed hub must use that same map. Stop if any placeholder remains or an operation fails.

```powershell
Assert-HubTarget
$Dashboard = 'agent-dashboard'
$DashboardIdentity = "hub-monitor@$HubProject.iam.gserviceaccount.com"
$ProjectNumber = Invoke-HubGcloud projects describe $HubProject --format='value(projectNumber)'
if ($ProjectNumber -notmatch '^[0-9]+$') { throw 'Project number is missing.' }
$DashboardAudience = "/projects/$ProjectNumber/locations/$Region/services/$Dashboard"
if ($HubUrl -notmatch '^https://[^/]+$') { throw 'Use the deployed hub HTTPS origin.' }

Invoke-HubGcloud services enable iap.googleapis.com
Invoke-HubGcloud iam service-accounts create hub-monitor --display-name='Read-only hub dashboard'
Invoke-HubGcloud iam service-accounts add-iam-policy-binding $DashboardIdentity `
    --member="user:$Operator" --role=roles/iam.serviceAccountUser --condition=None
Invoke-HubGcloud run services add-iam-policy-binding $Service --region=$Region `
    --member="serviceAccount:$DashboardIdentity" --role=roles/run.invoker --condition=None

$MonitorMap = Get-Content -LiteralPath $TokenFile -Raw | ConvertFrom-Json
$MonitorToken = $MonitorMap.status
if ($MonitorToken -isnot [string] -or $MonitorToken -notmatch '^[!-~]{32,256}$') {
    throw 'Generate a distinct status token in the private hub configuration first.'
}
if (@($MonitorMap.PSObject.Properties | Where-Object { $_.Value -eq $MonitorToken }).Count -ne 1) {
    throw 'The status token must not be shared with another principal.'
}
$MonitorTokenFile = Join-Path $PrivateFolder 'dashboard-status-token.txt'
if (Test-Path -LiteralPath $MonitorTokenFile) { throw 'Inspect the existing private temporary token file first.' }
try {
    [System.IO.File]::WriteAllText($MonitorTokenFile, $MonitorToken, [System.Text.UTF8Encoding]::new($false))
    Invoke-HubGcloud secrets create hub-status-token --data-file=$MonitorTokenFile `
        --replication-policy=user-managed --locations=$Region
} finally {
    if (Test-Path -LiteralPath $MonitorTokenFile) { Remove-Item -LiteralPath $MonitorTokenFile }
    Remove-Variable MonitorMap, MonitorToken
}
Invoke-HubGcloud secrets add-iam-policy-binding hub-status-token `
    --member="serviceAccount:$DashboardIdentity" --role=roles/secretmanager.secretAccessor --condition=None

Assert-HubTarget
$DashboardServices = Invoke-HubGcloud run services list --region=$Region --format=json | ConvertFrom-Json
if (@($DashboardServices | Where-Object { $_.metadata.name -eq $Dashboard }).Count -ne 0) {
    throw 'Initial deployment must not overwrite an existing service.'
}
Invoke-HubGcloud run deploy $Dashboard --image=$Image --region=$Region --platform=managed `
    --command=python --args=-m,agent_hub.dashboard --service-account=$DashboardIdentity `
    --port=8080 --cpu=1 --memory=256Mi --min=0 --max=2 --concurrency=16 --timeout=30 `
    --no-allow-unauthenticated --invoker-iam-check --iap --ingress=all `
    --set-env-vars="HUB_URL=$HubUrl,HUB_CLOUD_RUN_AUTH_MODE=metadata,DASHBOARD_IAP_AUDIENCE=$DashboardAudience" `
    --set-secrets=HUB_STATUS_TOKEN=hub-status-token:1
Invoke-HubGcloud run services add-iam-policy-binding $Dashboard --region=$Region `
    --member="serviceAccount:service-$ProjectNumber@gcp-sa-iap.iam.gserviceaccount.com" `
    --role=roles/run.invoker --condition=None
Invoke-HubGcloud iap web add-iam-policy-binding --region=$Region --resource-type=cloud-run `
    --service=$Dashboard --member="user:$Operator" --role=roles/iap.httpsResourceAccessor
$DashboardUrl = Invoke-HubGcloud run services describe $Dashboard --region=$Region --format='value(status.url)'
$DashboardUrl
```

Direct Cloud Run IAP protects the default service URL and authenticates the browser's Google account. Grant website access only to the intended new-domain operator or group. The dashboard service account receives invocation access to the hub and access to its own status-token secret; it needs no Firestore role or access to the full hub token secret. [Google's direct Cloud Run IAP setup and access commands](https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run)

The application additionally verifies the IAP assertion's ES256 signature, issuer, audience, lifetime, and user identity. It does not trust the unsigned email header. Its audience is the exact Cloud Run resource path above. Do not remove `DASHBOARD_IAP_AUDIENCE` or enable anonymous invocation to solve a login error. The public-key cache lasts five minutes; no assertion or token is logged. [Google's signed-header requirements](https://docs.cloud.google.com/iap/docs/signed-headers-howto)

## Verify the deployed service

Re-run `Assert-HubTarget`, inspect the dashboard's service configuration and IAM policy, and confirm IAP is enabled. Open the URL signed into the new-domain operator account. A signed-out request must encounter Google sign-in or denial. An unauthorized account must be denied. The page must load a real snapshot from the separate hub. If the hub credential is missing/wrong, the page must show an authentication or unavailable warning without exposing the credential.

In the browser's network inspector, `/api/status` must contain no prompts, results, provider keys, or hub tokens. The dashboard's mutation endpoints return `405`; the limited status token must be rejected by the hub's room creation and worker-claim endpoints. Check one real worker heartbeat and one intentionally disconnected worker before treating the dashboard as operational. Confirm unsupported provider balances read unavailable, and that a genuine provider observation is labeled with scope/source/time.

Offline HTTP, cache, projection, and signed-assertion tests run with `python -m unittest discover -s tests -p test_dashboard.py -v`. These tests do not verify live Google IAM/IAP policy, real provider account limits, or real Cloud Run deployment.
