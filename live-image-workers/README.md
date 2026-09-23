# Worker images from this checkout

The five worker images (claude, codex, copilot, cursor, grok) are not built directly from this repository: `providers/grok.py`, `tests/test_codex.py` and `tests/test_cursor.py` carry the operator's provider-owner email, which this public repository replaces with `cursor-owner@example.invalid`. Building from the repository as-is would pin the grok worker to a placeholder owner and refuse the real account.

They are built from packs kept outside git (`live-image-<provider>-<version>-source/`: `Dockerfile`, `cloudbuild.json`, `agent_hub/`, `live/`). `refresh_packs.py` makes a new versioned pack from a base pack and this checkout:

- `Dockerfile` and `agent_hub/` are taken from the base pack unchanged (each worker `FROM`s its provider's verified base image by digest; image-only modules such as `metadata`, `transport`, `protocol_gate` and the codex `credential_state` come from that base).
- `live/` is replaced with this checkout's `live_loop.py`, `provider_errors.py`, `entrypoint.py`, `dynamic_broker.py`, the provider adapter, `_grok_protocol.py` for grok, `credential_state_<provider>.py` renamed to `credential_state.py` for claude and cursor, `cursor_native/` for cursor, and the tests the Dockerfile runs.
- The owner pin is restored from `--owner-email` in the three pinned files. The tool refuses to overwrite an existing pack.

    python live-image-workers/refresh_packs.py --packs C:\API_KEYS\cloud-agent-online \
        --base-version live-20260922b --version live-20260923a --owner-email <owner email> [--out <dir>]

Verified 2026-09-23 on Alpha from the `live-20260922b` packs: for all five providers, `test_live_loop.py` and `test_dynamic_broker.py` pass with `python -I` in the merged image layout (`agent_hub/` + `live/`), and `test_copilot_provider.py` passes; the provider-specific tests (`test_claude.py`, `test_codex.py`, `test_cursor.py`, `test_grok.py`) need the base image and run in the build.

Build and deploy, one provider at a time, from the pack directory under the deployer identity (the deployer needs the same roles as for the controller; see `live-image-controller/README.md`):

    gcloud --configuration=runcrew-deploy builds submit --config cloudbuild.json . \
      --gcs-source-staging-dir=gs://project-0c6d31fa-509e-4116-a2c_cloudbuild/source
    gcloud --configuration=runcrew-deploy run jobs update runcrew-worker-<job> --region us-central1 \
      --image us-central1-docker.pkg.dev/project-0c6d31fa-509e-4116-a2c/runcrew-hub/<provider>-worker@<digest>

Updating a job's image changes its template digest. The slot's `template_sha256` in the controller's `fleet.json` has to match that digest, or the next tick refuses with `job_template_changed`. That digest is also part of `config_sha256` (policy + slot), which `runcrew_fleet_state` keeps from the tick that created it. A template-only change is re-keyed, not refused: when the slot is `idle` or `blocked` and the previous template still proves the stored digest, the controller CAS-saves the new `config_sha256` and launches on the new template. The state records `policy_sha256`, `slot_job_uid`, `slot_enabled`, and `slot_template_sha256` so the proof is the previous template filled back into the current policy and slot. A document written before those fields existed (only `config_sha256`) is handled in either of two orders:

- Update the slot digest while the job still has the previous template, and let one tick run. The controller derives that template from the live job, checks it recomputes to the stored `config_sha256`, and re-keys. Then update the image; the next tick launches.
- Or roll the image first and let one tick run before changing `fleet.json`. The digest still matches, so that tick records the previous template and then refuses with `job_template_changed`. Then record the new digest; the next tick re-keys and launches.

Deploy this controller and let it tick once before changing template digests if you can: after that, the image and the slot digest may change together. If both already moved and the document never recorded the previous template, the change is indistinguishable from a policy edit and stays `controller_config_changed`. Put the previous digest back for one tick (the image can stay on the new one), then set the new digest again. Any change other than `template_sha256` still refuses with `controller_config_changed`. Do not change the digest while a launch is in flight (`binding_intent`, `binding_ready`, `launch_intent`, `launch_submitted`, `grant_intent`): those phases are not re-keyed. An `active` slot refuses the new digest until that execution is terminal, then re-keys and replaces it. Do not `run jobs execute` by hand; the controller launches the next execution when the current one drains.

What these images add over `live-20260922b`: worker error codes reach the outcome record and the room message; a room missing `purpose` is refused as `purpose_missing` rather than the generic text; every provider error class carries the marker that keeps codes fixed.
