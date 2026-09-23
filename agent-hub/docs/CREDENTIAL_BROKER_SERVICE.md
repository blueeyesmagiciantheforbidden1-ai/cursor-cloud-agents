# Private credential broker HTTP boundary

`agent_hub.credential_broker_service` is an executable Cloud Run HTTP service and
a worker client. The service calls the existing fenced `CloudCredentialBroker`;
the worker client calls only the metadata identity endpoint and its configured
private broker HTTPS origin. It never discovers ambient application credentials
or calls Firestore or Secret Manager.

This code has **not been deployed or verified against live Google identity**.
Tests exercise real local HTTP, actual `google-auth` signature verification using
generated test keys, and the existing broker against fault-injected Google wire
responses. They do not establish cloud IAM, provider identity or enrollment.

## Identity and execution authorization

The production service verifies Google JWT signatures with `google-auth`, using
Google's fixed OAuth2 certificate URL. It verifies issuer, exact audience,
expiry and a protected allowlist of numeric service-account subjects. A signed
service-account email must also match that configured subject. Caller emails in
JSON, forwarded identity headers and decoded unsigned JWTs are never authority.

A Google service-account ID token does not prove a particular Cloud Run execution
UID. Every protected policy therefore also contains a controller-issued,
cryptographically random execution grant's SHA256, approved job/profile,
canonical provider account reference and expiration. After launching a job, the
controller publishes its exact execution name/UID in an immutable Firestore grant
document. The worker sends
the opaque grant in `X-RunCrew-Execution-Grant`; it never chooses the account,
profile or execution in HTTP JSON. The broker independently reads the configured
Cloud Run execution on every operation and checks its UID, service account and
nonterminal state before accessing credentials.

This proves possession of a controller-assigned capability, not hardware
attestation of a container. The controller must issue at least 256 bits of random
grant entropy, never reuse a grant, and keep it out of provider child processes,
model tools, project files and logs. A compromised trusted supervisor holding
both its service identity and grant can act for that granted execution.

Use the `Authorization: Bearer ...` header. Cloud Run documents that it strips
the signature from `X-Serverless-Authorization` before forwarding that header;
this service will reject unsigned/stripped tokens. Application verification is
additional to Cloud Run's IAM front door, not a substitute for deploying with
authentication required and restricted ingress.

## Protected configuration

Service JSON has exactly `schema_version: 1`, `audience`, and `bindings`. Audience
is the canonical HTTPS `run.app` service origin without a trailing slash. Each
binding has exactly:

```json
{
  "profile": {
    "provider": "copilot",
    "profile": "blueeyes",
    "account_ref": "<verified-owner-sha256>",
    "canonical_account_ref": "<verified-immutable-provider-identity-sha256>",
    "secret_id": "runcrew-credential-copilot-blueeyes",
    "job_id": "runcrew-worker-copilot"
  },
  "caller_subject": "<Google-service-account-numeric-unique-ID>",
  "caller_service_account": "runcrew-worker-copilot@project-0c6d31fa-509e-4116-a2c.iam.gserviceaccount.com",
  "grant_sha256": "<SHA256-of-controller-random-grant>",
  "expires_at": 0,
  "lease_seconds": 180
}
```

Placeholders are deliberately not runnable enrollment evidence. Expiration must
be in the future and no more than 24 hours ahead at request time. The loader
accepts at most 32 bindings. Canonical account identity must come from previously
verified provider evidence; mutable email alone is insufficient.

Worker JSON has exactly `schema_version: 1`, `endpoint`, `profile` (the same six
fields), `grant` (the random value, not its hash), and
`bootstrap_timeout_seconds` (integer 1..300; suggested commissioning value 120).
It contains no guessed execution UID. `load_client_config(path)` derives the
execution name from the configured job and the platform `CLOUD_RUN_EXECUTION`
environment variable, waits for the broker's authenticated bootstrap response,
then returns a `BrokerHTTPClient` directly. It has
`.config`, `.execution`, `.execution_uid`, and the existing broker-compatible
`acquire(execution, request_id)`, `assert_current`, `renew`, `commit`, `release`
and `quarantine` methods. `Lease` remains the existing concrete type; its
credential bytes are excluded from its representation.

Both loaders require an absolute Linux root-owned regular file, not a symlink,
at most 128 KiB, no group/other write bit and no write access by the worker.
Launch the service as a nonroot user. The immutable mount/controller is part of
the trust boundary; these filesystem checks do not themselves prove who wrote
the configuration.

## Execution sequence and deployment prerequisites

The executable entry point is:

```text
python -m agent_hub.credential_broker_service --config /run/config/broker.json
```

It binds `0.0.0.0:$PORT` (default 8080) only in a nonroot Linux Cloud Run service
environment (`K_SERVICE` and `K_REVISION`). Those environment checks prevent an
accidental local launch; they do not replace Google deployment/IAM verification.
Existing application requirements already include `google-auth` and its
cryptographic verification dependency. The HTTP implementation uses stdlib.

Before running it in Google Cloud, the controller/deployment must:

1. Provision the explicitly approved `runcrew-provider-auth` Firestore database;
   the backend and grant store must both use that exact database. A separate
   broker database avoids granting unrelated hub or OAuth services access to
   provider credential locks and grants.
2. Enroll the exact credential version and independently verified canonical
   provider account into `CloudCredentialBroker.initialize_binding`. This is a
   separate operator operation, never an HTTP task endpoint.
3. Give the broker identity only the required Firestore control-document access,
   access/add-version permissions on its configured credential secrets, and
   read access to configured Cloud Run executions. The runtime needs Firestore
   get/update for pre-enrolled locks and get for grants; create belongs to the
   separate enrollment/controller identity. Give worker identities only
   `roles/run.invoker` on the private broker; they must not receive broad
   Firestore or Secret Manager permissions.
4. Deploy with the IAM check enabled, no unauthenticated invoker, internal
   ingress, an appropriate internal worker network route, concurrency at most
   eight and an immutable reviewed image/configuration. Google network routing,
   authenticated audience and mounted-file ownership still need live checks.
5. Assign the grant to one native launch, persist its acquisition request UUID
   before HTTP acquisition, and perform native work only after the lease arrives.
   A response loss must not cause another native launch. Finish the native
   process before durable credential commit and release.

Cloud Run job execution names/UIDs are assigned after `jobs.execute`. Both
service policy and worker grant configuration can now be prepared before that
launch. After the journaled launch returns, the trusted controller calls:

```text
python -m agent_hub.credential_broker_service publish-execution --config /run/config/broker.json --execution <actual-full-resource-name> --execution-uid <actual-UID> --grant-sha256 <configured-grant-digest>
```

Alternatively a trusted controller imports
`ExecutionGrantStore(binding).publish(execution, execution_uid)`. This performs
an authenticated Google Cloud Run GET to verify the actual UID, approved job,
service account and active state, then one Firestore `exists:false` conditional
creation of `runcrew_execution_grants/<grant_sha256>`. It neither overwrites a
grant nor creates a credential binding. A lost acknowledgement is resolved only
by a strong read of that exact immutable record; uncertainty otherwise remains
an error. The operator must journal the original job launch and reconcile any
uncertain launch instead of starting a duplicate job.

During this interval, the worker performs only bounded `/bootstrap` readiness
polls; absent grant documents yield 202, whereas denied Firestore access is an
error. Bootstrap authenticates the same SA/grant and verifies the controller's
execution against Google. It returns only execution metadata. No provider CLI,
credential read/refresh, lease acquisition or credential write occurs while
waiting. The client accepts only its own platform execution name and the
server-verified UID. Publication must complete before the configured bootstrap
deadline or the supervisor stops without launching a provider process.

The database, IAM roles, container images, protected grants, grant-publication
controller invocation and private network still require actual deployment.
Neither this CLI nor passing tests claim that those resources exist.

## HTTP and failure behavior

Only `POST /v1/credentials/{bootstrap,acquire,assert-current,renew,commit,release,quarantine}`
exists. JSON rejects duplicate keys, nonfinite values, unknown action fields,
chunking/compression and duplicate authorization headers. Request/response size
is at most 100 KiB; decoded opaque credentials are at most Secret Manager's
64 KiB limit. Leases carry only fence, exact numeric version and acquisition ID;
account and execution fields are reconstructed from trusted binding state.

Commit reads the current fenced original version server-side, preserving the
no-refresh optimization without trusting worker claims about original bytes.
The existing broker still performs its phase, version and Firestore CAS checks.
No initialize, takeover, skip-fence or reconciliation operation is exposed.

The client uses HTTPS without proxies or redirect following. Only metadata-only
bootstrap readiness may poll before a lease exists; acquire and all credential
operations have no automatic retries.
The metadata endpoint is the only HTTP exception and requires Google's metadata
response header. Each transport has a 10-second socket timeout and 15-second
socket watchdog; DNS resolution remains a platform limitation. A response loss,
invalid successful mutation receipt or server error is reported as uncertain,
not success. Backend lost writeback acknowledgements remain quarantined; they
do not trigger a second Secret Manager add-version call.

Responses include `Cache-Control: no-store`; application request/error logging
is disabled and errors use fixed codes. Credentials and grants are never put
in URLs. Cloud infrastructure, tracing and access-log configuration must also
avoid capturing authorization headers or bodies. Cloud Run terminates TLS before
the local HTTP container listener; this is the documented private Cloud Run
deployment boundary, not application-level end-to-end encryption into Python.

Primary references checked 2026-09-21:

- [Cloud Run service-to-service authentication](https://docs.cloud.google.com/run/docs/authenticating/service-to-service)
- [Google token types](https://cloud.google.com/docs/authentication/token-types)
- [Official google-auth ID-token verification implementation](https://github.com/googleapis/google-auth-library-python/blob/main/google/oauth2/id_token.py)
- [Cloud Run execution resource and UID](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs.executions)

Focused offline validation:

```text
python -m unittest tests.test_credential_broker_service -v
```
