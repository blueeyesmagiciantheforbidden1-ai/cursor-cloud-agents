# Dedicated-project Google Cloud deployment

This is a deployment runbook, not a record of a live deployment. The user now authorizes the existing Google Cloud setup: create a **new dedicated project in the exact selected existing organization**, with separate service identities and storage. Use the standard Cloud Run `run.app` website URL. Domain purchase and creation of a new Google organization are canceled for this deployment. These files do not create cloud resources merely by existing.

The initial cloud service is a private collaboration API: Cloud Run holds no model-provider keys and executes no VPS commands. Windows workers call the hub over HTTPS and run their installed agents locally. Firestore stores rooms/messages/tasks. SQLite is for local tests only; the Cloud Run filesystem is ephemeral.

## 1. Verify the existing organization and authorization

Record the selected organization's exact numeric ID and display name from an authenticated read, the exact authorized deployment account, and an open billing account owned by that organization. The saved CLI authorization exposes organization `833782852711` (LanegecKo). The browser's other existing organization `1091262552755` belongs to a different signed-in account; it is not an interchangeable target. Select one explicitly, and do not move or overwrite existing projects.

The selected operator needs permission to create a project, attach the selected billing account, enable services, create the listed resources, and manage project IAM. The account also needs read access to organization and billing metadata. Stop on permissions or organization-policy errors. Project separation does not remove inherited organization policies or make this a separate identity organization.

The target guard defaults to the earlier, stricter `--isolation organization` mode, which rejects both known existing organizations and requires a new-domain identity. **This authorized deployment must explicitly use `--isolation project`.** That mode permits the exact existing organization/account and a pinned existing credential-file authorization, while requiring the dedicated project to have been created since this task began. It still rejects arbitrary token files, access-token environment overrides, impersonation, and unrecognized authentication overrides.

## 2. Select and verify the target

The following commands are **PowerShell**. Run from the `agent-hub` source directory with Python 3.12+ and a current Google Cloud CLI installed. Replace every uppercase placeholder first. Keep the same shell session throughout. No command changes the default gcloud project.

```powershell
$ErrorActionPreference = 'Stop'
$HubSource = (Get-Location).Path
$TargetOrg = 'EXACT_SELECTED_ORG_ID'
$ExpectedOrgName = 'EXACT_DISPLAY_NAME_FROM_AUTHENTICATED_READ'
$CreatedAfter = '2026-09-20T00:00:00Z'
$HubProject = 'NEW-GLOBALLY-UNIQUE-PROJECT-ID'
$BillingAccount = 'EXACTB-ILLING-ACCTID'
$Operator = 'EXACT-AUTHORIZED-ACCOUNT@example.com'
$InvokerMember = "user:$Operator"
# Use the exact existing file authorized for this account, or $null for ordinary gcloud auth login.
$CredentialFile = 'C:\ABSOLUTE\PATH\TO\AUTHORIZED\gateway-credentials.json'
$Region = 'us-central1'
$Service = 'agent-hub'
$Repository = 'agent-hub'

function Invoke-HubGcloud {
    & gcloud --account=$Operator --project=$HubProject --quiet @args
    if ($LASTEXITCODE -ne 0) { throw 'gcloud failed; stop this deployment.' }
}
function Assert-HubTarget {
    $CredentialArguments = @()
    if ($CredentialFile) { $CredentialArguments = @('--credential-file', $CredentialFile) }
    & python .\deploy\check_target.py --isolation project --organization $TargetOrg `
        --expected-org-name $ExpectedOrgName --created-after $CreatedAfter `
        --project $HubProject --billing-account $BillingAccount --account $Operator @CredentialArguments @args
    if ($LASTEXITCODE -ne 0) { throw 'Isolation check failed; no further changes are allowed.' }
}

if ($InvokerMember -cne "user:$Operator") {
    throw 'Initial invocation access must use the exact selected operator.'
}
if ($CredentialFile) {
    if (-not [System.IO.Path]::IsPathRooted($CredentialFile) -or -not (Test-Path -LiteralPath $CredentialFile -PathType Leaf)) {
        throw 'The explicitly authorized credential file must exist at an absolute path.'
    }
    $env:CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE = $CredentialFile
} else {
    & gcloud auth login $Operator
    if ($LASTEXITCODE -ne 0) { throw 'Authentication failed.' }
}
Assert-HubTarget --before-create
```

The checker reads exact organization ID/display name, billing account, project parent, label, creation time, and billing linkage. It rejects a folder/no-organization/different-organization target, a project created before the cutoff, or a billing account owned elsewhere. `--created-after` can tighten the default task cutoff but cannot move it earlier. `--before-create` skips only the not-yet-created project checks; it still checks organization, billing, and authorization. [Billing parent metadata](https://docs.cloud.google.com/billing/docs/reference/rest/v1/billingAccounts)

Every read pins `--account` and `--project` and rechecks auth configuration. `--credential-file` permits exactly the named normalized absolute override and requires it to be active; it neither activates a credential itself nor silently falls back to a login. The file's contents are never printed or copied by the guard. This pins the user-authorized credential source; it does not independently prove the email encoded by a credential file. The operator must establish that association from the authorized setup. No successful target check is a full IAM or organization-policy audit.

## 3. Create only the new project

Run once with a new project ID. `projects create` must succeed; an existing project or any other error stops the run. After an interrupted run, inspect the partially created new project and resume only the specific unfinished steps after re-running the target checker. Do not ignore errors or blindly rerun creation commands.

```powershell
Invoke-HubGcloud projects create $HubProject --organization=$TargetOrg --labels=agent-hub-isolated=true
Assert-HubTarget --unbilled
Invoke-HubGcloud billing projects link $HubProject --billing-account=$BillingAccount
Assert-HubTarget
Invoke-HubGcloud services enable run.googleapis.com firestore.googleapis.com `
    secretmanager.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com `
    iam.googleapis.com logging.googleapis.com storage.googleapis.com
```

The explicit `--organization` argument prevents accidental placement in another hierarchy. This creates a dedicated project under the selected existing organization. [Project creation](https://docs.cloud.google.com/sdk/gcloud/reference/projects/create)

## 4. Create storage and dedicated identities

```powershell
Assert-HubTarget
$Runtime = "hub-runtime@$HubProject.iam.gserviceaccount.com"
$Builder = "hub-builder@$HubProject.iam.gserviceaccount.com"
$SourceBucket = "gs://$HubProject-hub-source"

Invoke-HubGcloud iam service-accounts create hub-runtime --display-name='Agent Hub runtime'
Invoke-HubGcloud iam service-accounts create hub-builder --display-name='Agent Hub image builder'
Invoke-HubGcloud projects add-iam-policy-binding $HubProject `
    --member="serviceAccount:$Runtime" --role=roles/datastore.user --condition=None
Invoke-HubGcloud projects add-iam-policy-binding $HubProject `
    --member="serviceAccount:$Builder" --role=roles/logging.logWriter --condition=None
foreach ($Identity in @($Runtime, $Builder)) {
    Invoke-HubGcloud iam service-accounts add-iam-policy-binding $Identity `
        --member="user:$Operator" --role=roles/iam.serviceAccountUser --condition=None
}

Invoke-HubGcloud firestore databases create --database='(default)' `
    --location=$Region --type=firestore-native --edition=standard --delete-protection
Invoke-HubGcloud firestore indexes composite create --database='(default)' `
    --collection-group=agent_hub_rooms --query-scope=collection `
    --field-config='field-path=next_agent,order=ascending' `
    --field-config='field-path=created_at,order=ascending'
Invoke-HubGcloud artifacts repositories create $Repository `
    --repository-format=docker --location=$Region
Invoke-HubGcloud artifacts repositories add-iam-policy-binding $Repository `
    --location=$Region --member="serviceAccount:$Builder" --role=roles/artifactregistry.writer --condition=None
Invoke-HubGcloud storage buckets create $SourceBucket --location=$Region `
    --uniform-bucket-level-access --public-access-prevention
Invoke-HubGcloud storage buckets add-iam-policy-binding $SourceBucket `
    --member="serviceAccount:$Builder" --role=roles/storage.objectViewer
```

The runtime has project-local Firestore access; it will receive access to one named secret in the next step. The builder can write the one image repository, read the source bucket, and write build logs. Neither identity receives Owner/Editor. The operator must be allowed to act as the two service accounts to build/deploy. [Firestore creation](https://docs.cloud.google.com/sdk/gcloud/reference/firestore/databases/create), [custom build identity](https://docs.cloud.google.com/build/docs/securing-builds/configure-user-specified-service-accounts)

The composite index is required for the worker queue query: `next_agent == actor`, ordered by `created_at` ascending. Its collection is `agent_hub_rooms` and both fields use ascending order. Wait until this index is `READY` before enabling workers; inspect its state with `Invoke-HubGcloud firestore indexes composite list --database='(default)' --filter='COLLECTION_GROUP:agent_hub_rooms' --format=json`. If the collection environment setting changes, create the equivalent index for that collection. [Composite index creation](https://docs.cloud.google.com/sdk/gcloud/reference/firestore/indexes/composite/create), [listing indexes](https://docs.cloud.google.com/sdk/gcloud/reference/firestore/indexes/composite/list)

## 5. Provision application tokens

Use the hub CLI to generate distinct random tokens for `manager`, `status`, `codex`, `claude`, `cursor`, and `copilot`. Keep the generated files in a private, non-synced folder outside the source tree. The example first restricts a **new** Windows directory to the current user. Do not use the API key workspace, OneDrive, a Git repository, or Cloud Build source for this directory.

```powershell
$WorkerWorkspace = 'C:\Path\To\Existing\Project'
$PrivateFolder = Join-Path $env:LOCALAPPDATA ('AgentHubPrivate-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $PrivateFolder | Out-Null
$PrivateAcl = New-Object System.Security.AccessControl.DirectorySecurity
$PrivateAcl.SetAccessRuleProtection($true, $false)
$CurrentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$PrivateAcl.SetOwner($CurrentSid)
$PrivateRule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $CurrentSid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$PrivateAcl.AddAccessRule($PrivateRule)
Set-Acl -LiteralPath $PrivateFolder -AclObject $PrivateAcl
& python -m agent_hub.cli init --output-dir $PrivateFolder --workspace $WorkerWorkspace
if ($LASTEXITCODE -ne 0) { throw 'Token generation failed.' }
$TokenFile = Join-Path $PrivateFolder 'hub-tokens.json'
$TokenJson = Get-Content -LiteralPath $TokenFile -Raw
$TokenMap = $TokenJson | ConvertFrom-Json
$RequiredPrincipals = @('manager', 'status', 'codex', 'claude', 'cursor', 'copilot')
$TokenProperties = @($TokenMap.PSObject.Properties)
if ($TokenProperties.Count -ne 6) { throw 'Unexpected principal set.' }
foreach ($Principal in $RequiredPrincipals) {
    $Value = $TokenMap.$Principal
    if ($Value -isnot [string] -or $Value.Length -lt 32) { throw 'Missing or weak application token.' }
}
if (@($TokenProperties.Value | Select-Object -Unique).Count -ne 6) { throw 'Tokens must be unique.' }

Assert-HubTarget
$TokenJson | & gcloud --account=$Operator --project=$HubProject --quiet secrets create hub-tokens `
    --data-file=- --replication-policy=user-managed --locations=$Region
if ($LASTEXITCODE -ne 0) { throw 'Secret creation failed.' }
Remove-Variable TokenJson, TokenMap, TokenProperties, Value
Invoke-HubGcloud secrets add-iam-policy-binding hub-tokens `
    --member="serviceAccount:$Runtime" --role=roles/secretmanager.secretAccessor --condition=None
```

The JSON travels over standard input, never as a command-line token value. Secret version `1` is created by the successful first creation. Give each worker only its own application token through its local protected environment/configuration; keep the manager token separate. After deployment, set each production GCE Windows worker config's `hub_url` to the Cloud Run HTTPS origin, `cloud_run_auth_mode` to `"metadata"`, and `workspaces` to the allowed paths on that VM before enabling it. Attach a dedicated worker service account to the VM and grant it only service-level `roles/run.invoker` on this hub. The hub uses the Secret Manager value as `HUB_TOKENS_JSON`. AI subscriptions and provider authentication remain with the Windows agents. [Secret creation from stdin](https://docs.cloud.google.com/sdk/gcloud/reference/secrets/create)

## 6. Build the allowlisted source and deploy

The build context includes only `agent_hub`, its Python requirements, Dockerfile, ignore files, and the Cloud Build config. Confirm the list contains no credentials before submitting. Build and deployment may incur Google Cloud charges; configure the desired project budget/alerts on the selected billing account. The instance limit below controls compute scaling, not all spending.

```powershell
Assert-HubTarget
Invoke-HubGcloud meta list-files-for-upload
$ImageTag = (Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss')
$Image = "$Region-docker.pkg.dev/$HubProject/$Repository/hub:$ImageTag"
Invoke-HubGcloud builds submit $HubSource --config=deploy/cloudbuild.yaml --region=$Region `
    --service-account="projects/$HubProject/serviceAccounts/$Builder" `
    --gcs-source-staging-dir="$SourceBucket/source" --substitutions="_IMAGE=$Image" --ignore-file=.gcloudignore

Assert-HubTarget
$ExistingServices = Invoke-HubGcloud run services list --region=$Region --format=json | ConvertFrom-Json
if (@($ExistingServices | Where-Object { $_.metadata.name -eq $Service }).Count -ne 0) {
    throw 'The initial deployment must not overwrite an existing service.'
}
Invoke-HubGcloud run deploy $Service --image=$Image --region=$Region --platform=managed `
    --service-account=$Runtime --port=8080 --cpu=1 --memory=512Mi `
    --min=0 --max=2 --concurrency=16 --timeout=60 `
    --no-allow-unauthenticated --invoker-iam-check --ingress=all `
    --set-env-vars="HUB_BACKEND=firestore,GOOGLE_CLOUD_PROJECT=$HubProject,HUB_FIRESTORE_COLLECTION=agent_hub_rooms" `
    --set-secrets=HUB_TOKENS_JSON=hub-tokens:1
Invoke-HubGcloud run services add-iam-policy-binding $Service --region=$Region `
    --member=$InvokerMember --role=roles/run.invoker --condition=None
$HubUrl = Invoke-HubGcloud run services describe $Service --region=$Region --format='value(status.url)'
$HubUrl
```

`ingress=all` permits Windows VPS clients to reach the HTTPS endpoint. Access still requires **both** Google IAM authentication and the application's `X-Hub-Token`. The explicit invoker check must remain enabled, and there must be no `allUsers` or `allAuthenticatedUsers` invoker binding. The app token is not an alternative to Cloud Run IAM. [Cloud Run deployment/auth flags](https://docs.cloud.google.com/sdk/gcloud/reference/run/deploy), [Secret Manager integration](https://docs.cloud.google.com/run/docs/configuring/services/secrets)

## 7. Verify before enabling workers

Re-run `Assert-HubTarget`; inspect the new service's IAM policy and revision configuration with `run services get-iam-policy` and `run services describe`. Confirm the correct runtime identity, Firestore backend, FIFO composite index in `READY` state, Secret Manager version, and invoker check. An unauthenticated HTTPS request must be denied. A request with a valid Google ID token but a missing/wrong `X-Hub-Token` must also be denied. A properly authenticated manager request should succeed, followed by a task sent to one worker and its result returned. Run the worker in manual/read-only mode for this initial round trip.

For production GCE Windows workers, `cloud_run_auth_mode: "metadata"` obtains an audience-bound ID token from the VM metadata server using the VM's attached service account and `hub_url` as its audience. This mode needs no gcloud user session or downloaded service-account key. It does not work on VPSs outside Google Compute Engine. The worker still requires its own application token. [Metadata ID tokens](https://docs.cloud.google.com/docs/authentication/get-id-token)

Use `cloud_run_auth_mode: "gcloud"` only for a human-authenticated development test. The local CLI must use the selected authorized account; the worker captures the result of `gcloud auth print-identity-token` internally without printing it. The legacy `cloud_run_auth: true` setting selects this development mode, so it is not the production setting. [Developer authentication](https://docs.cloud.google.com/run/docs/authenticating/developers)

Administer GCE Windows workers through IAP TCP forwarding for RDP, then sign into a local Windows account on the VM. Configure the IAP access policy and firewall to permit only the intended administrators; no public RDP listener is needed. Windows RDP account setup is separate from the attached service account used by the worker. OS Login is not the Windows sign-in mechanism. VM provisioning and this RDP setup are separate from the Cloud Run deployment above. [Windows RDP and IAP](https://docs.cloud.google.com/compute/docs/instances/connecting-to-windows)

## Validation and remaining work

Run offline isolation checks with `python -m unittest discover -s deploy -p test_isolation.py -v`. These tests use fake metadata and prove rejection paths; they do not verify a real cloud deployment. Docker build, real Firestore transactions, IAM propagation, Cloud Run startup, and the authenticated VPS round trip must be verified after the selected account and project pass the target checks. This starter uses the Python standard-library HTTP server; review capacity, logs, retention, abuse controls, backup/recovery, and token rotation before broader production use. No custom domain or TLS certificate purchase is needed for the Cloud Run URL.

The retained organization-isolation mode is a separate, stricter option for a future expressly authorized new-organization deployment: use `--isolation organization --organization NEW_ID --expected-domain VERIFIED_NEW_DOMAIN --account OPERATOR_AT_NEW_DOMAIN`, omit `--credential-file`, and authenticate with ordinary `gcloud auth login`. That mode continues to reject both known existing organizations. It is not the mode selected by the current user instruction.
