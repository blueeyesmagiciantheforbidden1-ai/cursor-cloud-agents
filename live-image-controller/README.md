# Controller image from this checkout

Same image as the `live-image-controller-live-20260922c` pack (Dockerfile layout, base `python:3.12-slim-bookworm`, non-root uid 10001, build-time test lines), with sources taken from this repository instead of a copied pack. The build context is the repository root.

Build (from the repository root, under the deployer identity, never the shared human account):

    gcloud builds submit --config live-image-controller/cloudbuild.json .

The build fails if `test_fleet_controller.py` (14 tests, including the reset checks), `test_dynamic_broker_review.py`, `test_dynamic_broker.py` or `serve.py --check` fail; confirm those RUN lines appear in the build log before deploying.

Deploy as a new revision of the service `runcrew-live-fleet`, keeping every existing environment variable name, the `/run/config/fleet.json` mount, the service account and `RUNCREW_ROLE=fleet`; only the image changes:

    gcloud run services update runcrew-live-fleet --region us-central1 \
      --image us-central1-docker.pkg.dev/project-0c6d31fa-509e-4116-a2c/runcrew-hub/controller-worker:live-20260923a

Do not set `RUNCREW_RESET_ENABLED` unless an operator is about to call `POST /reset`; remove it afterwards. Never print the service's environment values.

What this revision adds over `live-20260922c`: `runcrew_fleet_tick` records carry the controller's refusal code (`reason`) instead of a bare `controller_attention_required`; `POST /reset` exists behind the flag. See `STATUS.md`.
