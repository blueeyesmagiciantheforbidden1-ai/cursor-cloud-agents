# Controller images from this checkout

Two images share the pinned `python:3.12-slim-bookworm` digest, uid 10001, and the in-image unittest lines. Sources come from this repository. The build context is the repository root.

| Image | Dockerfile | What it installs | Cloud Run service |
| --- | --- | --- | --- |
| Fleet controller | `Dockerfile` | standard library only; no pip | `runcrew-live-fleet` (`RUNCREW_ROLE=fleet`) |
| Live broker | `Dockerfile.broker` | the six pinned distributions in `requirements-broker.txt` | `runcrew-live-broker` (`RUNCREW_ROLE=broker`) |

The fleet controller does not verify ID tokens. Only the broker image installs google-auth. `Dockerfile.broker` runs `serve.py --check-broker` at build time, and `dynamic_broker.main` runs the same offline RS256 check before it listens. A broker image without the library exits at startup, so Cloud Run keeps the previous revision, instead of answering `401 authentication_required` to every worker. `serve.py --check` does not import google-auth; the fleet image must still pass it. Do not deploy `Dockerfile.broker` to `runcrew-live-fleet`, and do not deploy the fleet `Dockerfile` to `runcrew-live-broker`.

## Fleet image

Same layout as the `live-image-controller-live-20260922c` pack, with sources taken from this repository.

Build (from the repository root, under the deployer identity, never the shared human account):

    gcloud builds submit --config live-image-controller/cloudbuild.json \n      --gcs-source-staging-dir=gs://project-0c6d31fa-509e-4116-a2c_cloudbuild/source .

The config runs the build as `runcrew-op-deploy` (`serviceAccount` in `cloudbuild.json`), because the project's default Cloud Build runtime account holds no roles. The explicit staging directory is required: the deployer identity deliberately lacks `storage.buckets.list`, which `builds submit` otherwise calls. The deployer needs object read on that bucket, `artifactregistry.writer` on `runcrew-hub`, `logging.logWriter`, and `serviceusage.serviceUsageConsumer`.

The build fails if `test_fleet_controller.py` (14 tests, including the reset checks), `test_dynamic_broker_review.py`, `test_dynamic_broker.py` or `serve.py --check` fail, so a build status of `SUCCESS` from `gcloud builds describe <id>` is the gate: Docker does not produce an image if any RUN line fails. Build logs go to Cloud Logging only (as in the pack); reading them is optional. The base image is the tag `python:3.12-slim-bookworm`, as in the pack; after the first build, pin the digest it resolved (from the build's pull line) in the Dockerfile for reproducibility.

## Broker image

Build with `cloudbuild.broker.json`. It uses `Dockerfile.broker` and the same `runcrew-op-deploy` service account pin as `cloudbuild.json`. The image tag is `controller-worker:live-20260924a-broker`.

    gcloud builds submit --config live-image-controller/cloudbuild.broker.json \
      --gcs-source-staging-dir=gs://project-0c6d31fa-509e-4116-a2c_cloudbuild/source .

Deploy that image only as a new revision of `runcrew-live-broker`, keeping the existing environment variable names, the `/run/config/live-broker.json` mount, the service account and `RUNCREW_ROLE=broker`. The build also runs the fleet image's `--check`, then `--check-broker`.

## Fleet deploy

Deploy the fleet image (not the broker tag) as a new revision of the service `runcrew-live-fleet`, keeping every existing environment variable name, the `/run/config/fleet.json` mount, the service account and `RUNCREW_ROLE=fleet`; only the image changes:

    gcloud run services update runcrew-live-fleet --region us-central1 \
      --image us-central1-docker.pkg.dev/project-0c6d31fa-509e-4116-a2c/runcrew-hub/controller-worker:live-20260923a

Do not set `RUNCREW_RESET_ENABLED` unless an operator is about to call `POST /reset`; remove it afterwards. Never print the service's environment values.

What this revision adds over `live-20260922c`: `runcrew_fleet_tick` records carry the controller's refusal code (`reason`) instead of a bare `controller_attention_required`; `POST /reset` exists behind the flag and reconciles a slot that is `blocked` or stuck in any intent phase (a lost or refused grant publication leaves `grant_intent` forever), provided its latest execution is terminal and the credential was released. See `STATUS.md`.

A worker-image rollout changes the job template digest and therefore the slot `template_sha256` inside `config_sha256`. Slots re-key that one change when they are `idle` or `blocked`: state stores `policy_sha256` and the slot fields, and a document that only has `config_sha256` is accepted when the previous template (saved on an `idle` or `blocked` tick of this revision, including the idle pass after an active drain, or still the live job template) recomputes to the stored digest. The new `config_sha256` is CAS-saved and the next launch uses the new template. Any other config change still comes back as `controller_config_changed`. Do not roll a template digest while a slot is mid-launch; an `active` execution is refused until it is terminal, then re-keyed. Rollout order is in `live-image-workers/README.md`.
