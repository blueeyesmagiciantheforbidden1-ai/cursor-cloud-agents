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

Updating a job's image changes its template digest, and the fleet controller refuses a job whose template digest differs from its slot configuration (`job_template_changed`). Update the controller's `fleet.json` slot `template_sha256` for that job in the same maintenance window, or the slot stops. Do not `run jobs execute` by hand; the controller launches the next execution when the current one drains.

What these images add over `live-20260922b`: worker error codes reach the outcome record and the room message; a room missing `purpose` is refused as `purpose_missing` rather than the generic text; every provider error class carries the marker that keeps codes fixed.
