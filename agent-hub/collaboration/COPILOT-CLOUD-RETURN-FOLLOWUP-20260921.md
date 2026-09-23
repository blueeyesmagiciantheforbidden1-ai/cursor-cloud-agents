# Copilot cloud return commissioning

Owner: Codex bounded Copilot coordinator, 21 September 2026. This is coordinator evidence, not an invented Copilot response.

The resumed user request is to finish the three remaining actual provider returns. Claude and Cursor already succeeded; do not replay those tasks.

Copilot's existing job was verified idle, with exactly its one successful native metadata execution. No stale local controller locks remained. The real GitHub browser receipt was refreshed at 21:15:43 UTC by the parent coordinator, showing the intended account, Copilot Max, included usage and additional spending disabled.

Added and tested a narrow refresh path for an already configured but never launched review job. It verifies the old exact template, successful terminal metadata execution, unchanged job UID and one execution, then creates a new immutable billing config version while preserving prior receipts. The job config refresh keeps the image, identity, resources, IAM and launch guards.

An initial configuration patch was rejected because Cloud Run Jobs PATCH does not support updateMask. A separate validation-only call proved HTTP 400 / unknown update_mask; live state remained version 2 and one metadata execution. The rejected intent was preserved. The supported correction copies the exact current template, changes only the protected review config version to 3, uses its etag and verifies the resulting entire template. No inference occurred in this correction.

Fourteen focused local tests passed. The exact job refresh subsequently verified successfully. The parent coordinator authorized one real no-tools review using Kimi K3 at maximum effort, included subscription only, room 65f8d056e2c34707923c4a60b6cee6e8. The single start controller is now executing; completion evidence will be appended separately after verification.

Official Cloud Run Jobs PATCH contract: https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs/patch

## Resumed recovery and second review

The first review execution `runcrew-worker-copilot-blueeyes-7wljl` failed at native `session.create` with `unexpected_native_frame`. The strict wrapper sequence had not reached `session.send`; internal work performed by the native session-create implementation is not proven absent. Credentials were quarantined without a new writeback. The exact failed execution terminated and no automatic retry occurred.

Added separately bounded handling for official SDK `session.lifecycle` notifications, with session binding, event limits, no callback approval and no conversion into model-output evidence. Native frame diagnostics now expose only fixed categories and a wrapper-send flag. The historical frame was not captured, so the lifecycle compatibility gap is established from source and tests, rather than claimed as an observed historical frame. Eighteen native transport and ten commissioning tests passed, plus the image's Linux tests and native image check.

Corrected immutable image: `copilot-worker@sha256:299029805e293c74ea50ba23897d9968aa91b3fb1238a439f4d8bac882588611`; build `22bba8c5-67ce-440d-8779-962871d2be64`. Source-only frozen overlay: `cloud-agent-online/copilot-commission-20260921f-source` in the shared WORK directory.

The parent coordinator supplied two fresh exact capabilities in shared broker configuration version 6, while retaining every prior binding. A separately reviewed operator-only primitive fenced the terminal quarantine into one metadata-only execution. Metadata recovery `runcrew-worker-copilot-blueeyes-427zb`, UID `8af87af0-b308-48e4-935c-9ebcf33c86f7`, succeeded at 21:54:31.980016 UTC. It verified the intended native account, 24 models, Kimi K3/max support, 99.1% included premium allowance remaining, zero model calls, zero sessions and committed credentials. The controller also verified durable credential release before preparing another task.

Isolated resume controller: `commission_copilot_resume.py`; durable journal: `cloud-agent-online/copilot-resume1.json`. Its five focused tests pass; the parent's fenced-recovery primitive has seven passing tests. Previous execution, result and room receipts remain intact.

New exact room: `3f9b39ae749a4fd38828520c8ca5ddd3`. New real review execution: `runcrew-worker-copilot-blueeyes-ms7dh`, UID `84bca694-e857-4650-ad48-17f3969d5543`, using corrected image f and a separate review-only capability. It was created at 21:58:44.229404 UTC and the actual hub room became running at 22:02:03 UTC, one attempt. A real 21:51:23 UTC GitHub billing observation authorized only included usage with additional spending disabled. No final task answer has been asserted at this checkpoint; await the exact room and cloud receipt.

## Uncertain second outcome and separate commissioning task

Execution `ms7dh` terminated failed at 22:02:56.899658 UTC. Its bounded diagnostic reported `unexpected_native_frame` at `session.send`, a valid-looking id-less method categorized as `other`, and `wrapper_send_may_have_started=true`. The historical raw method and payload were not retained. There is no recoverable answer in the retained logs, and provider usage for this attempt is unknown. The room remains failed; its prompt must not be replayed. A fresh durable read found credentials quarantined at fence 5, retained version 2, with no commit intent or writeback. Public evidence: `cloud-agent-online/copilot-uncertain-review-20260921.json`.

The offline parser correction follows official SDK behavior for bounded id-less notifications: harmless unregistered notifications are ignored, requests with IDs and tool/permission families remain rejected, and notification content is never logged or accepted as output. Diagnostics record a fixed method category or a method hash/length. A failed task may now retain refreshed credentials only after native identity verification, successful group termination and process wait, and acknowledged credential commit/release. The task remains failed/uncertain. Unknown owner, unconfirmed stop, or uncertain writeback still quarantines.

Twenty-four transport tests and ten commissioning tests pass, including stop/owner/writeback failure cases. The distinct task is room `d2f51585138743de828e3212f1243a98`, prompt SHA256 `558aa9a2f1adcdd2e53dc1b1f22f40f83e02ec2062e5cdd5b6a09a8fd0aebe5c`. It requests three honest readiness labels and one regression test, at most 180 words, without tools. This is not a replay of the uncertain prompt.

New isolated controller `commission_copilot_final.py` uses `cloud-agent-online/copilot-final1.json`, fresh unpublished client capabilities 5/6, the independently reviewed `copilot_uncertain_metadata_recovery.py`, and combined broker receipt `final-workers-broker-extension.json`. Twenty controller/recovery tests pass. New image g is building under build `2d30f07f-6fec-4f55-9993-178a379d6a6d`; no metadata or task execution has been started at this checkpoint. Root must confirm the combined live broker release before launch. Both previous failed rooms remain preserved.

Official SDK source: https://raw.githubusercontent.com/github/copilot-sdk/main/python/copilot/_jsonrpc.py and https://raw.githubusercontent.com/github/copilot-sdk/main/python/copilot/client.py

The corrected image g built successfully with Linux tests and native image verification: `sha256:f988a49626e93be1466abd92569a2f971d9b39c64b9cbeb4ff757e893505d6d3`. Root independently reviewed the parser and conditional failure writeback. The final controller now has seven focused passing tests.

Combined broker version 8 was verified live with unchanged service UID and all prior bindings preserved. One new metadata-only recovery, `runcrew-worker-copilot-blueeyes-pwz54` / UID `ef2b78b2-b44f-4c03-9705-fe50b1b08b19`, completed successfully at 22:29:09.738560 UTC. It verified the expected account, 24 models, Kimi K3/max eligibility, included allowance and no extra-usage route; zero model calls and zero sessions. Credential commit/release was separately observed in durable state at idle fence 7. The old uncertain task was not replayed.

Google's initially published execution status lagged the native result. The metadata timestamp was 22:28:58.003381 and result log 22:29:03.599595, about 5.6 seconds for that native section. The preceding delay is not attributed to native model work.

Fresh actual GitHub browser evidence at 22:26:42 UTC is preserved as `copilot-browser-evidence-review3-20260921.json`; it showed Max, 166/20,000 displayed included credits, additional spending disabled and $0/$0. Provider totals are not attributed to an individual uncertain task.

The distinct task is now launched once as `runcrew-worker-copilot-blueeyes-r67mw`, UID `8796c8bd-422e-4942-98b5-f255c1e3bc75`, using separate client version 6 and an exact prompt/config hash. Launch and capability publication are verified in `cloud-agent-online/copilot-final1.json`. At this checkpoint no final answer has been claimed; reconcile this execution and room rather than creating another.

## Actual Copilot return verified

Room `d2f51585138743de828e3212f1243a98` completed with exactly one attempt and exit code 0 at 22:35:27.153785 UTC. Google confirmed execution `r67mw` successful and terminal at 22:35:31.649606 UTC. The native receipt identifies `kimi-k3`, maximum reasoning effort, `is_auto=false`, `is_byok=false`, and committed credentials. Actual usage: 2,095 input tokens, 293 output tokens and 23 reasoning tokens. This is token usage, not a dollar charge.

The hub answer matches native SHA256 `f53ea371dffcee247d083c4844ebac97adedf9b275f9a8dfa22481feb8f3a9a2`. A fresh durable-state read at 22:37:36.039811 UTC verified idle fence 8, the exact successful execution UID, no quarantine, and credential version 2 equal to the acknowledged release version. Retained commit markers are historical receipt data; they do not represent a pending commit in this idle state.

Cross-checked public receipt: `cloud-agent-online/copilot-final-verified-return.json`. Parent independently saved the actual reply as `cloud-agent-online/completed-copilot-room-20260921.json`. The reply proposes separate authentication, last-task-completed and fresh-heartbeat labels, plus a regression test preventing a completed test from implying an always-on worker. Codex review: a fresh heartbeat proves recent liveness, not guaranteed future continuous availability. This successful one-shot task does not establish a continuously available coding worker.

Both old failed rooms remain preserved, the prior uncertain task's provider usage remains unknown, and no old prompt was replayed. No more inference was launched after this success. Copilot file ownership is released back to the parent coordinator for integration; immutable images, source snapshots and execution journals must remain unchanged.
