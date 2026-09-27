# Per-lane finalization budgets and admission floors

Status: T176 design only. No runtime, test, hub, or transport behavior changes in this document. The proposed allocations are **Claude 310 s, Codex 300 s, Copilot 310 s, Cursor 290 s, and Grok 300 s**. They are conservative policy budgets derived below, not measured production latencies. They become defensible bounds on the worker's attempted finalization only after the blocking-operation and deadline changes below land together with T175's continuing task-lease renewal. No finite reserve bounds the current implementation, and no timeout guarantees successful delivery through unavailable infrastructure.

Sources are the supplied worker e988be4, reference hub bc6172c, and transport snapshot (`CONTEXT.md:2-4`), in local base commit c91a544. T169 is background; its line references and its older snapshot description are not authoritative for this tree. Paths beginning `providers/`, `cursor_native/`, or bare `live_loop.py`, `broker_renew.py`, `credential_state_claude.py`, and `credential_state_cursor.py` are relative to `cca/live-worker-runtime/`. Other paths are workspace-relative. All durations below are seconds. **EST** means arithmetic from configured timeouts; **SIM** means an offline fake-clock execution; **NEW** means a proposed timeout or policy value, with its derivation and insertion point. None is a live latency measurement.

## Current common costs and missing bounds

The worker uses metadata authentication (`entrypoint.py:132-137`). Let **H = (5+10)+(5+10) = 30**: a cold identity fetch and hub request, followed by identity refresh and another request after authentication rejection. The configured socket timeouts are 5 and 10 (`cca/agent-hub/agent_hub/worker.py:251-254,281-320`). The identity cache lasts 480 (`:233`); a cached request is nominally 10, or 25 after an authentication retry. H deliberately treats calls independently. It is an EST envelope, **not** a wall-clock bound: DNS, repeated socket reads, response/error-body close, and local serialization lack an aggregate deadline.

Let **B = 15+15 = 30**: uncached broker identity exchange, then broker action exchange (`cca/agent-hub/agent_hub/credential_broker_service.py:629-632,675-697`). Each exchange has socket timeout at most 10 and watchdog 15; these overlap, so do not add 10 to 15 (`:107-153`). DNS is explicitly excluded from that watchdog (`:110-111`). Identity errors precede mutation; errors after dispatch can be `MutationUncertain`; do not blindly replay a commit (`:678-697`). Server-side work is inside the observed exchange, not an additional serial client allowance; a timed-out mutation can still finish remotely.

| Common term | Current cost and boundedness | Proposed bound and insertion point |
|---|---|---|
| Task heartbeat before adapter close | H; wall time unbounded. Guard reserves only the 10 s opener, not H (`live_loop.py:1019-1024,1044-1047`; `broker_renew.py:65-94`). | NEW dedicated heartbeat whole-operation cap15 including authentication/DNS, further clipped to remaining renewable lease time; derivation below. Move to the independent T175 renewal coordinator; no serial heartbeat wait inside native/credential finalization. |
| Nested worker report and compatibility retry | Up to 2H=60; wall time unbounded (`live_loop.py:947-1004,1090-1095`). | NEW zero synchronous network time in the answer-to-completion interval. Queue or drop optional telemetry. |
| Loop finishing heartbeat | H; wall time unbounded; skipped for a recent acknowledgement, transport failure, stopping, or the 10 s guard (`live_loop.py:903-933`). | Covered by the same independent coordinator, not a second serial request. |
| Due broker renewal in a native tick | B; combined callback can cost B+2H+1+2H=151 including ordinary heartbeat retry and reports (`broker_renew.py:120-140`; `live_loop.py:1033-1095`; lane callbacks below). Repetition and blocking remain unbounded. | NEW one strict broker-renew operation at the answer boundary, cap B=30. Disable synchronous broker/telemetry calls from subsequent finalization ticks; preserve ownership checks. This is a separate slot before the short native-tail timers. |
| Successful credential finish | assert-current B + commit B + release B = 90, plus local I/O; wall time unbounded. Action dispatch: `cca/agent-hub/agent_hub/credential_broker_service.py:725-745`; lane session citations below. | NEW 30 per complete broker operation, including authentication, DNS, response validation and close; aggregate 90. No hidden retries. |
| Failed writeback/quarantine | Ordinary post-answer finish failure can call quarantine twice: session `writeback_uncertain`, then adapter `provider_refresh_uncertain`; EST 2B=60, wall time unbounded. Lane citations below. | NEW shared post-answer quarantine cap 60, with at most the existing two calls. Retain the first reason and fencing. Reserve it even though successful finish and quarantine are normally exclusive. |
| Local credential reads/writeback | stat, symlink/type checks, open, read, JSON/identity validation are unbounded by elapsed time. Native credential writes must have stopped before reading. Remote writeback is the commit above. | NEW 5 aggregate for local credential I/O/validation across both adapter and session, at their close/finish boundary. Killable owned helper or equivalent isolation; timeout means uncertain cleanup, never successful writeback. |
| Completion | Three POSTs and sleeps 1,2: 3H+1+2=93, wall time unbounded today (`live_loop.py:1141-1191`). A definitive 409 stops earlier (`:1167-1175`). | NEW three 30 s whole-call caps plus the existing 1+2 sleeps, all under a shared 93 s deadline. Include DNS, auth refresh, reads, encoding/decoding and error-body close. Check remaining time before every attempt and sleep. |
| Logging/control/shutdown | Span logger is synchronous; `print(..., flush=True)` can block (`live_loop.py:823-828`; `entrypoint.py:135-137`). Forced report after completion is also synchronous (`live_loop.py:1584-1588`). | NEW 5 aggregate coordinator/IPC/serialization/cancellation overhead. Nonblocking telemetry enqueue gets at most 0.1 of that allowance; drop on congestion. No log flush or telemetry-network wait before completion. After completion, separately cap forced report at H=30; it is outside delivery F and cannot block worker exit indefinitely. |

The H=30 and B=30 NEW caps preserve the current independent socket/watchdog envelopes while adding an actual outer deadline. Implement deadline-aware, cancellable transport helpers in `cca/agent-hub/agent_hub/worker.py:225-320` and `credential_broker_service.py:107-153,675-697`. Cap DNS resolution at **5 NEW** inside, not in addition to, the operation cap (chosen from the existing metadata socket allowance at `worker.py:251`). Socket inactivity timers remain subordinate to remaining operation time. A thread `join(timeout)` that leaves a blocked request alive is not cancellation. Use supervised, owned processes or a proven cancellable transport, retain TLS/redirect/proxy restrictions, and include startup, IPC, cancellation and joining in the shared deadlines. Process death cannot undo a broker mutation; ambiguous receipts retain quarantine/fencing and prevent reuse. If the OS cannot confirm native stop, fail closed rather than declaring credentials clean. The wall budget describes how long the supervisor waits, not a promise that arbitrary kernel stalls or remote mutations are reversed.

### Current total accounting

Let A be Claude's answer-to-EOF/exit tail, P Copilot's getCurrent plus shutdown tail, and T Codex's finish_turn tail, **excluding** renewal callbacks. Let K be the number of due native-tail callbacks, each with the loose 151 envelope above; let Wc/Wk count writer threads. These are not fixed post-answer constants. Early-answer current success envelopes, with both finishing beats and due report compatibility retry, are:

| Lane | EST early-answer sum | Current strict bound |
|---|---|---|
| Claude | A + (12+0.1Wc) + 90 broker + 120 beats/reports + 93 completion + 151K = A+315+0.1Wc+151K | Unbounded |
| Codex | T + (6+0.5Wk) + 90 + 120 + 93 + 151K = T+309+0.5Wk+151K | Unbounded |
| Copilot | P + 5 reap + 5 drain + 90 + 120 + 93 + 151K = P+313+151K | Unbounded |
| Cursor | 5 stop + 90 + 120 + 93 = 308, plus local validation/I/O | Unbounded |
| Grok | 10 stop + 90 + 120 + 93 = 313, plus local validation/I/O | Unbounded |

The 120 is H adapter beat + 2H report/compatibility + H loop beat. Add up to 60 quarantine on error paths, but do not claim that success and every error branch happen together. Finishing guards may skip calls, caches shorten authentication, and native processes may already have exited. These sums expose configured costs; they are neither tight achievable maxima nor measured wall times. Native-tail failures can discard an answer before the adapter returns it. Lane details follow.

When the current execute-derived native deadline binds, an answer one second before it leaves only R+1=26 by default on Claude/Cursor/Grok/Copilot, or R+45+1=71 on Codex (`live_loop.py:317,1115`; `providers/codex.py:40,634`; other native deadline assignments `providers/claude.py:531`, `cursor.py:482`, `grok.py:496`, `copilot.py:849`). This is why moving only the response timer cannot protect the native tail. Normal second loop close is idempotent, not another full native/credential transaction (`live_loop.py:1437-1444` and each lane's close guards).

## Per-lane tables

The common proposed serial allowance is **C = 30 renew + 5 local credential I/O + 90 finish + 60 quarantine + 93 completion + 5 coordination = 283**. Each lane uses **F = 10 * ceil((C+N)/10)**, where N is its proposed native budget. Rounding to the next 10 is NEW policy headroom, not additional observed work. Counting both quarantine and full delivery deliberately avoids taking credit for mutually exclusive branches. Concurrent task renewal and deferred telemetry have zero serial allowance, but have the explicit operation caps above.

The small NEW limits are engineering cutoffs at the cited call sites, not performance claims: Claude tail5 and Copilot RPC5 use the existing clean-exit/reap/drain5 scale (`codex-cloud-transport/metadata.py:404`; `providers/copilot.py:358-361`). Local credential I/O5 and coordination5 each reuse the T150 margin scale (`live_loop.py:343`). Aggregate pipe/validation1, Copilot pipe/selector2, writer joins1, combined Codex joins2 and nonblocking enqueue0.1 are deliberate caps for local-only work; exceeding them fails closed or drops optional telemetry. They must be fault-injection tested and measured before reducing them. No existing source proves these new operations always succeed within those values.

### Claude

| Term | Current source, EST cost and boundedness | NEW allocation |
|---|---|---|
| Answer tail | `providers/claude.py:338-357`: waits for trailing frames/EOF and process exit using the generation deadline; ticks `:205-229,255-260`. A is unbounded as an independent post-answer term. | 5 for validation/EOF/clean exit after answer; separate native-finalization deadline. |
| Native close/drain | `providers/claude.py:359-382`: wait10 + reader joins1+1 + 0.1 per writer; streams close without timer. Wait/join terms individually bounded, total close unbounded. Normal initialize/settings/prompt flow creates three writers (`:247-249,315,472,477`), giving EST12.3 for those configured waits. | 14 aggregate: wait10 + readers2 + all writers1 + pipes/OS-close1. Thus N=5+14=19; do not reset on repeated close. |
| Renew | `_renew` at `providers/claude.py:412-415`; native ticks above. B or combined151 per due callback, unbounded overall. | Common30, outside the N=19 timer. |
| Credential writeback/finish/quarantine | `credential_state_claude.py:57-80` contains the post-stop credential read; adapter close `providers/claude.py:426-437`, fallback quarantine `:438-449`. Three B calls plus local I/O; two B quarantine calls on ordinary finish failure; unbounded wall time. | Common5+90+60. |
| Heartbeats, telemetry, completion, control | Common sources above: H+2H+H, 3H+1+2, synchronous logger; unbounded wall time. | Concurrent renewal, deferred telemetry, completion93, coordination5. |
| Total, admission, hub minimum | Current early success A+315+0.1Wc+151K, plus unbounded I/O. | 283+19=302; **F310**; **30+310+5=345** admission; **345+30=375** hub minimum. |

### Codex

| Term | Current source, EST cost and boundedness | NEW allocation |
|---|---|---|
| Answer tail | `providers/codex.py:654-665`; `codex-cloud-transport/transport.py:194-201`; `codex-cloud-transport/metadata.py:401-416`: stdin close, wait until min(native deadline, now+5), readers0.5+0.5, deadline tick and trailing validation. T has unbounded stdin/tick work; no fresh tail deadline. | 7 aggregate: stdin/validation1 + exit5 + readers1. |
| Native stop | `providers/codex.py:237-253`: wait5 + readers0.5+0.5 + each writer0.5, untimed pipe closes. Warm metadata refreshes accumulate writers (`:203,365,560`). Per-wait bounded; overall unbounded. | 8 aggregate: wait5 + all reader/writer joins2 + pipes1. Thus N=7+8=15. |
| Renew | `providers/codex.py:299-305,161-164`; current B/combined151 due tick; unbounded overall. | Common30, then no synchronous renew chain in native finish. |
| Credential writeback/finish/quarantine | `cca/agent-hub/deploy/codex-worker/credential_state.py:57-82`; `providers/codex.py:417-449`. Three B, local I/O, two B quarantine calls on ordinary finish failure; unbounded wall time. | Common5+90+60. |
| Heartbeats, telemetry, completion, control | Common H+2H+H and 3H+1+2, plus logger; unbounded wall time. Current extra native reserve45 and cap600 (`providers/codex.py:40,634`) do not bound finish. | Concurrent renewal, deferred telemetry, completion93, coordination5; replace the old45 rather than adding it again. |
| Total, admission, hub minimum | Current T+309+0.5Wk+151K plus unbounded I/O. | 283+15=298; **F300**; **30+300+5=335** admission; **335+30=365** hub minimum. |

### Copilot

| Term | Current source, EST cost and boundedness | NEW allocation |
|---|---|---|
| Model verification and shutdown RPCs | Answer at `providers/copilot.py:862`, getCurrent at `:863`, runtime.shutdown at `:864-865`; shared deadline set `:849`, checks `:381-389`, request/pump `:468-519`. P is unbounded as a separate tail; a late answer can still fail the native deadline. | 5 getCurrent + 5 shutdown, each clipped to a new native-finalization deadline. These values are NEW policy choices, matching the existing5 drain/reap scale, not measured RPC maxima. |
| Reap, terminal drain, pipe cleanup | `providers/copilot.py:351-379`: wait5 then separate drain5; latter does not call native tick. `:333-349`: failure close may wait5 and pipes/selector close have no timer. Individually timed waits; total unbounded. | One shared reap5 across success/failure paths + terminal drain5 + pipe/selector close2; N=5+5+5+5+2=22. Never restart reap allowance after failure. |
| Renew | `providers/copilot.py:596-599`; tick `:383-384` on cadence20 (`:28`). B/combined151 per due callback, unbounded overall. | Common30 before the N=22 phase; ticks cannot synchronously add another30 or report. |
| Credential writeback/finish/quarantine | `cca/agent-hub/deploy/copilot-worker/credential_state.py:59-82`; `providers/copilot.py:614-649`. Three B plus local I/O and two B quarantine calls on ordinary finish failure; unbounded wall time. | Common5+90+60. |
| Heartbeats, telemetry, completion, control | Common H+2H+H and 3H+1+2, plus logger; unbounded wall time. | Concurrent renewal, deferred telemetry, completion93, coordination5. |
| Total, admission, hub minimum | Current P+313+151K plus unbounded I/O. | 283+22=305; **F310**; **30+310+5=345** admission; **345+30=375** hub minimum. |

### Cursor

| Term | Current source, EST cost and boundedness | NEW allocation |
|---|---|---|
| Answer validation and native stop | Buffered validation `providers/cursor.py:488-495`; inherited close `:262-263`, `cursor_native/metadata.py:170-179`: wait5, selector/stream closes untimed. No extra waiting RPC after answer, but local validation/close unbounded. | N=6: wait5 + buffered validation/selector/pipes1, aggregate across retries. |
| Renew | `providers/cursor.py:310-313`; post-answer path does not require a native RPC tick. A newly needed boundary renewal still costs B; unbounded wall time. | Common30, before N. |
| Credential writeback/finish/quarantine | `credential_state_cursor.py:57-80` contains the post-stop credential read; adapter close/finish/fallback `providers/cursor.py:330-354`. Three B, local I/O and two B quarantine calls on ordinary finish failure; unbounded wall time. | Common5+90+60. |
| Heartbeats, telemetry, completion, control | Common H+2H+H and 3H+1+2, plus logger; unbounded wall time. | Concurrent renewal, deferred telemetry, completion93, coordination5. |
| Total, admission, hub minimum | Current308 plus unbounded validation/I/O. | 283+6=289; **F290**; **30+290+5=325** admission; **325+30=355** hub minimum. |

### Grok

| Term | Current source, EST cost and boundedness | NEW allocation |
|---|---|---|
| Answer validation and native stop | Answer path `providers/grok.py:490-528`; `_grok_protocol.py:193-198`: wait10 then untimed stdin/stdout close. Daemon reader (`:110-120`) is not joined today. No extra waiting RPC after answer; overall unbounded. | N=11: wait10 + validation/pipes/reader-stop1, aggregate across retries; confirm reader termination or fail closed. |
| Renew | `providers/grok.py:251-255`; post-answer path does not require another native RPC tick. Boundary B remains wall-time unbounded. | Common30, before N. |
| Credential writeback/finish/quarantine | `cca/agent-hub/deploy/grok-worker/credential_state.py:59-82`; `providers/grok.py:263-291`. Three B, local I/O and two B quarantine calls on ordinary finish failure; unbounded wall time. | Common5+90+60. |
| Heartbeats, telemetry, completion, control | Common H+2H+H and 3H+1+2, plus logger; unbounded wall time. | Concurrent renewal, deferred telemetry, completion93, coordination5. |
| Total, admission, hub minimum | Current313 plus unbounded validation/I/O. | 283+11=294; **F300**; **30+300+5=335** admission; **335+30=365** hub minimum. |

## Deadline and renewal contract

Use a monotonic deadline D mapped conservatively from the hub's deadline/server_time receipt, including elapsed heartbeat time as today (`live_loop.py:1105-1117`). NEW **G=30** for every lane takes Codex's existing useful generation minimum (`providers/codex.py:45`) as a cross-lane policy; the other lanes currently have only the loop's generic5 check (`live_loop.py:1116,1420-1422`). Preserve **M=5**, the T150 `EXECUTE_FLOOR_MARGIN_SECONDS` (`live_loop.py:339-343`), and hold it as deadline headroom:

```text
_adapter_execute_floor(lane) = GENERATION_MINIMUM[lane] + FINALIZATION[lane]
need = _adapter_execute_floor(lane) + EXECUTE_FLOOR_MARGIN_SECONDS
admit only if D - monotonic_now >= need
response_cutoff = D - FINALIZATION[lane] - EXECUTE_FLOOR_MARGIN_SECONDS
finalization_cutoff = D - EXECUTE_FLOOR_MARGIN_SECONDS
```

Admission happens before the actual native prompt and before setting `model_call_attempted=True`; refuse with `task_deadline_insufficient`, retaining pre-model classification where the hub supports it (`live_loop.py:1420-1427`; `runcrew/agent_hub/core.py:235-238`). Run an early check before expensive setup and recheck after the model-call heartbeat, broker asserts/renews, metadata refresh and prompt-file work, immediately before dispatch. The present5 margin cannot cover a cold broker/H call. A margin is not authorization to skip that last check. Guard failure after a model-call phase beat must not move phase backward (`live_loop.py:1410-1413`). No model call occurs on rejection, even when too little time remains to deliver its failure record.

F includes completion. Replace legacy R=25 (range15..30, `live_loop.py:317,326`) and Codex45 atomically; do not subtract them again. Preferred contract passes raw D and named response/finalization deadlines. If an intermediate implementation must keep E=D-R, its execute helper uses G+(F-R), then adds M; the response cutoff remains D-F-M. Update `_task_budget` (`live_loop.py:1198-1205`) to the same lane contract. Validate known lanes centrally; fake adapters must receive that contract too, rather than silently defaulting to zero finalization.

At the answer boundary, start one shared finalization clock. Optionally skip the allocated broker renewal only when fresh confirmed ownership proves enough credential lifetime; otherwise perform the bounded strict renewal before native tail timers. After a confirmed renewal, the longest native22 + credential I/O5 + finish90 = **117** fits the existing credential lease240 (`broker_renew.py:22`); including the renewal's own30 from its conservative start anchor gives **147 < 240** (`broker_renew.py:128-140`). Even adding both quarantine calls gives207. No second synchronous renewal is needed in that bounded successful finish window. Do not continue normal finish after an unconfirmed strict renewal or lost fence. The old close reserve60 and tolerance180 (`broker_renew.py:22-25`) do not establish this guarantee; phase transition must replace the old callback policy explicitly.

T175 task renewal must cover actual adapter-owned cleanup and completion, including retry sleeps, not only the loop's usually idempotent `close` (`live_loop.py:1437-1449`). Hub renewable lifetime45 is separate from absolute D (`runcrew/agent_hub/core.py:50,2120-2131`). Use a dedicated NEW heartbeat cap15, derived from the metadata5 plus one opener10 path (`worker.py:251,291`); any auth retry shares the same cap and can time out earlier. Cadence is at most10 after the previous response, using the existing finishing freshness scale (`live_loop.py:337`), with one request in flight and no nested report/serial retry. Previous-request15 + cadence10 + next-request15 =40 < 45 from the previous acknowledged request's start. A general H30 cap would not work: the prior response can itself arrive30 after server acceptance, leaving too little time for another30 call.

At coordinator entry and on every request also clip the cap to `min(15, last_ack_request_start+45-M-now, finalization_cutoff-now)`; a nonpositive cap means no new request. This handles an older acknowledgement inherited from generation. Only an acknowledged renewal updates that anchor; a timeout or unavailable hub cannot be treated as renewed. Start the coordinator before finalization while enough renewable lease time remains. Preserve absolute D clamping. Use a separate client: shared `HubClient` token/opener mutation has no thread-safety contract (`cca/agent-hub/agent_hub/worker.py:218-234,314-319`). Stop/join coordinator and helpers within the common5 allowance; do not acquire a lock held by a blocked completion. Fence the race with accepted completion so a subsequent heartbeat409 cannot overwrite its success. SIGTERM must retain fencing and the same outer deadline, not extend it (`live_loop.py:830-850`).

Timeout/uncertain native stop or writeback never permits success completion. Existing ordinary post-answer failed cleanup returns `credential_cleanup_failed` (`live_loop.py:1484-1517`); retain that behavior. The pre-prompt sign-out exception (`:1488-1511`) is not permission to deliver a paid answer after uncertain cleanup. Startup/pre-model cleanup needs its own bounded coordinator: T172's no-handle path can make additional quarantine calls (`live_loop.py:865-901`; `providers/codex.py:417-449`; `cca/agent-hub/deploy/codex-worker/credential_state.py:57-82`). The proposed two-quarantine post-answer allowance is not a claim that every startup failure has exactly two calls. Preserve first-reason semantics (`cca/agent-hub/agent_hub/cloud_credential_broker.py:803-813`).

### Copilot response cutoff is not its native finalization deadline

For answer time a <= response_cutoff, use a bounded boundary-renew slot ending no later than a+30, then a fresh native finalization deadline no later than a+30+22, always clipped to the global finalization cutoff. getCurrent5, shutdown5, reap5, drain5 and pipe/selector2 share N22. The response timer stops applying to these RPCs. Give drain the same outer deadline and cancellation checks; its current local now+5 does not renew or check the native deadline (`providers/copilot.py:361-389`). Preserve getCurrent verification, shutdown acknowledgement and trailing protocol validation. A late response is refused; a response just before cutoff has the full reserved tail. Failed verification remains a post-model failure, not a successful text shortcut (`providers/copilot.py:883-903`; `runcrew/agent_hub/core.py:235-238`). Apply equivalent phase separation to Claude EOF and Codex finish_turn.

## Hub minima and the pinned test

Current `runcrew/agent_hub/core.py:43-45,198-200` uses roster maximum with base60, Codex120 and Claude300. Proposed per-agent minima equal the admission floor plus **S=30 planning setup headroom**, inherited from the existing pin's explanation (`test_live_loop.py:774`). S is not a measured or enforced setup upper bound; actual worker admission still wins if setup takes longer. Keep base60 only as the generic helper fallback and set all live-lane entries explicitly. Empty/omitted room rosters expand to the full fleet (`core.py:1615-1620`), so their proposed effective minimum is375, not60:

| Lane | G | F | M | Admission | Current hub minimum | Proposed hub minimum = admission+S | Minimum room wall = 2*hub minimum |
|---|---:|---:|---:|---:|---:|---:|---:|
| Claude | 30 | 310 | 5 | 345 | 300 | 375 | 750 |
| Codex | 30 | 300 | 5 | 335 | 120 | 365 | 730 |
| Copilot | 30 | 310 | 5 | 345 | 60 | 375 | 750 |
| Cursor | 30 | 290 | 5 | 325 | 60 | 355 | 710 |
| Grok | 30 | 300 | 5 | 335 | 60 | 365 | 730 |

**The Grok/Cursor/Copilot 60 s minimum cannot remain an admitted native-call minimum.** The updated hub rejects explicit60 rooms for these lanes and uses the new roster maximum for mixed rooms. Omitted timeout currently selects max(default300, roster floor), so it also rises; the existing900 cap still contains all proposals (`core.py:1632-1640`). Room-wall minimum doubles the roster floor (`:1644-1646`), and claim/recovery checks use the actor's floor (`:1987-1990,1007-1008`). Raising a hub minimum does not create a reserve inside an existing lease. Preserve stored deadlines and let worker admission handle already-created short rooms. Update the explicit Cursor300 caller in `runcrew/agent_hub/study_bridge.py:157-159`, which would otherwise be rejected. The nearby comment claiming Claude600 (`core.py:1631`) is stale; actual policy is300.

The task's approximate test citation has moved: `test_live_loop.py:768-783` now pins R+5+30 <= 60 and Codex45+30+R <= 120. **Leave that pin unchanged until its lane-aware replacement lands in the same implementation change.** Its Codex inequality omits the later T150 margin and the general setup headroom. The replacement must:

1. Pin the complete table above from shared policy values, including room-wall doubling, roster max, omitted timeout and the explicit study caller. Verify exact hub minimum accepted and one less rejected.
2. For each lane, test raw remaining time equal to G+F+M, just below it, and loss of time during the final pre-prompt operations. Assert rejection has no native prompt and `model_call_attempted=False`. Preserve/extend existing T150 boundaries (`test_live_loop.py:4828-4888`).
3. Check response_cutoff, distinct native/outer deadlines, warm `_task_budget`, R endpoint compatibility if retained, and no double subtraction of R or Codex45. Exercise fake adapters through the same contract.
4. Turn T169 gates (`test_live_loop.py:340-403`, `test_f4_hub_e2e.py:753-783`) into ordinary passing tests only with sufficient admitted budget and the new shared deadline contract. Keep strict before-hub-deadline assertions; check acknowledgement/receive time as well as POST start. Add late-answer tail, real operation-timeout and continuous-renewal tests, including Copilot getCurrent/shutdown/drain, double quarantine, DNS stall, writer accumulation, SIGTERM and telemetry backpressure. Unavailable infrastructure must produce bounded uncertain/failure outcomes, never invented accepted delivery.

## Rollback to LIVE 9e0044c and plain b4c404d

These are distinct targets: LIVE is b4c404d plus the study overlay (`runcrew/docs/MYHERO_ROLLBACK_COMPAT.md:110-114`). Neither historical commit object/tree is supplied here; `git cat-file -t 9e0044c` and `git cat-file -t b4c404d` both fail. Historical minimum/expiry statements are secondary evidence from `T169-REVIEW.md:89-95,119-134,174-190`, not a fresh execution of either old hub. In that account old Claude admits60, as do Grok/Cursor/Copilot; do not depend on any proposed hub minimum surviving rollback.

Worker admission uses actual raw remaining time and therefore still rejects unsuitable work before the model call on **both** old hubs. Longer valid existing tasks can proceed; neither new admission nor T175 renewal extends an old absolute deadline. If the worker itself is rolled back, these protections disappear. Ordinary lease renewal remains necessary; first-loss auto/read-only expiry can clear the lease, so late success acceptance is not the budget strategy (`T169-REVIEW.md:119-142`). Old hubs omit lease-reason metadata, so ambiguous heartbeat409 can prevent failure completion (`MYHERO_ROLLBACK_COMPAT.md:624-630`; `live_loop.py:1537-1541`). Admission guarantees no paid call on inadequate time, not universal delivery of a pre-model refusal record.

Both historical targets can ignore a prior newer `post_model`/`worker_reported` retry when counting recovery, permitting another automatic started-call retry (`MYHERO_ROLLBACK_COMPAT.md:218-230`). Budgeting does not repair that state-machine difference. Preserve existing rollback preflight/manual-recovery guidance. Existing stored timing fields survive historical get/claim/complete in the documented probe (`:635-636`); new minimum policy must not depend on rewriting old rooms or on new wire fields understood only by the latest hub.

## Ordered implementation work (not performed here)

1. **Operation deadlines and cleanup ownership first.** Worker-pack client sources `cca/agent-hub/agent_hub/worker.py`, `credential_broker_service.py`; tests `cca/agent-hub/tests/test_worker.py`, `test_credential_broker_service.py`. Add whole-call/DNS bounds, isolated cancellable operations, mutation-uncertainty handling, and nonblocking telemetry at `cca/live-worker-runtime/entrypoint.py`, `live_loop.py`. Add focused tests in `test_live_loop.py` and `test_broker_renew.py`. Apply corresponding canonical client changes in `runcrew/agent_hub/worker.py`, `credential_broker_service.py` and their tests before refreshing the vendored worker copy; do not accidentally change the broker server's separate pre/post-mutation budget policy.
2. **Codex transport dependency.** Change `codex-cloud-transport/transport.py:194-201`, `metadata.py:401-416` and `test_transport.py` so finish_turn receives the later finalization deadline and bounded stdin/exit/join behavior. Preserve terminal/trailing-event checks. A worker-only override is possible but duplicates transport logic; preferred order is transport change and pin first, then worker consumption. Update pack transport integration/tests, not reference code in this design task.
3. **Native phase separation and bounded credential cleanup.** `cca/live-worker-runtime/providers/{claude,codex,copilot,cursor,grok}.py`, `providers/_grok_protocol.py`, `cursor_native/metadata.py`, `credential_state_{claude,cursor}.py`, `broker_renew.py`; `cca/agent-hub/deploy/{codex,copilot,grok}-worker/credential_state.py`. Add aggregate joins/pipe bounds, one answer-boundary renewal, all deadlines above, and a shared quarantine coordinator preserving first reason and no-success-on-uncertainty. Grok's reader needs cancellation-aware queue puts (`providers/_grok_protocol.py:110-120`), not only a timed join. Tests: `tests/test_{claude,codex,cursor,grok}.py`, `test_copilot_provider.py`, `test_broker_renew.py` and each affected deploy lane's `test_metadata.py`/`test_package.py`. Cover pre-model no-handle cleanup separately.
4. **Atomic admission/policy migration with T175 integration.** `cca/live-worker-runtime/live_loop.py`, all five providers, `broker_renew.py`, `test_live_loop.py`, `test_f4_hub_e2e.py`, `test_failure_injection.py`. Introduce one lane policy source, raw/response/native/outer deadlines, final pre-prompt recheck, warm gate, renewal through actual cleanup and delivery, bounded coordinator shutdown, and replacement of the old pin in the same change. Do not ship a constants-only reserve increase or remove expected failures before the behavior is fixed.
5. **Hub minima and callers.** `runcrew/agent_hub/core.py`, `mcp.py` (timeout help at `:111`), `study_bridge.py`; tests `test_hub.py`, `test_myhero_audit_fixes.py`, `test_myhero_recovery.py`, `test_myhero_c093bc38.py`, `test_mcp.py`, `test_myhero_connector.py`, `test_myhero_study_scope.py`; docs `ROOM_STATES.md`, `MYHERO_ROLLBACK_COMPAT.md`. Cover roster/default/room-wall/claim/recovery behavior and update fixed short-timeout fixtures intentionally. Deploy updated hub admission before enabling normal scheduling onto budgeted workers, while retaining worker enforcement for rollback.
6. **Package and verify together.** `cca/live-image-workers/refresh_packs.py`, `cca/tests/test_refresh_packs.py`, affected worker pack manifests/build definitions and runtime documentation as determined by the pack build. Run all six cca groups, F4 with the hub-first import path, transport tests, and runcrew full tests. Re-run historical compatibility probes only when actual target trees are available. Collect per-stage timings before any later reduction of these conservative allocations; aggregate loop close spans currently miss cleanup inside execute (`live_loop.py:1424-1444`).

## Offline evidence and reproducible probe

SIM results on this tree: a real `HubClient` plus fake openers consumes **83** fake-clock seconds for three completion attempts with one cold identity and two cached starts, each receiving an authentication retry: 30+25+25+1+2. The conservative independently-cold envelope remains93. Real broker `_post` plus a fake exchange consuming its configured watchdog values gives30 per operation,90 for finish,60 for two quarantines. These measure control flow with the real configured timeout arguments, not DNS, network throughput, actual sleeping, or production native processes. Native tails and local I/O are EST/NEW because offline fakes cannot prove their wall-clock maxima.

The existing T169 fake records fast close2 -> POST976, close30 -> POST1004, and close20 with a10 timeout -> POSTs994,1005 against hub deadline1000. Those timings remain an expected gap, not evidence that the proposed changes are implemented. The following exact PowerShell command uses only stdlib and in-memory fakes. It ran **6 tests, OK (expected failures=2)**; the imported T169 class is also discovered. Python emitted a ResourceWarning while implicitly closing a fake HTTP503 error; this does not establish a production leak, but the real error-body close path must be inside the proposed transport bound.

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; $env:PYTHONPATH='C:/cursor-tasks/T176/cca/agent-hub;C:/cursor-tasks/T176/cca/live-worker-runtime'; @'
import io, json, unittest
from unittest.mock import patch
from urllib.error import HTTPError
from agent_hub import worker as hw, credential_broker_service as bs
from live_loop import Worker, Settings
from test_live_loop import Clock, Adapter, Client, PostAnswerLeaseBudgetTests

class BudgetProbe(unittest.TestCase):
    def test_hub_auth_and_completion(self):
        clock = Clock()
        start = clock()
        class Response(io.BytesIO):
            headers = {'Metadata-Flavor': 'Google'}
        class Metadata:
            def open(self, request, timeout):
                self_timeout = timeout
                assert self_timeout == 5
                clock.sleep(timeout)
                return Response(b'fake-identity')
        class Hub:
            calls = 0
            def open(self, request, timeout):
                assert timeout == 10
                clock.sleep(timeout)
                self.calls += 1
                if self.calls % 2:
                    raise HTTPError(request.full_url, 401, 'fake', {}, io.BytesIO(b'{}'))
                if self.calls < 6:
                    raise HTTPError(request.full_url, 503, 'fake', {}, io.BytesIO(b'{}'))
                return Response(json.dumps({'room_id': 'a'*32, 'status':'completed'}).encode())
        client = hw.HubClient(hw.Config('https://hub.invalid', 'grok', 'fake', 'FAKE', {}, cloud_run_auth_mode='metadata'))
        client.opener = Hub()
        loop = Worker(Settings('grok', 'grok-live'), client, Adapter(), object(), clock=clock, sleep=clock.sleep)
        loop.task = {'room_id':'a'*32, 'lease_token':'fake', 'step':0}
        loop.cleaned = True
        with patch.object(hw, 'build_opener', return_value=Metadata()), patch.object(hw.time, 'monotonic', clock):
            loop.complete('answer', 0)
        self.assertEqual(clock()-start, 83)
        self.assertEqual(client.opener.calls, 6)
        print('hub simulated seconds: cold=30, cached auth retries=25+25, sleeps=3, total=83; independent cold envelope=93')

    def test_broker_default_exchange_envelope(self):
        clock = Clock()
        client = object.__new__(bs.BrokerHTTPClient)
        client.endpoint, client.host, client._grant = 'https://broker.invalid', 'broker.invalid', 'fake'
        default = bs.exchange.__kwdefaults__['timeout_seconds']
        seen = []
        def exchange(host, path, **kw):
            cap = kw.get('timeout_seconds', default)
            seen.append(cap)
            clock.sleep(cap)
            if kw.get('metadata'):
                return 200, {'Metadata-Flavor':'Google'}, b'a.b.c'
            return 200, {}, b'{"ok":true}'
        with patch.object(bs, 'exchange', side_effect=exchange):
            for action in ('renew', 'assert-current', 'commit', 'release', 'quarantine', 'quarantine'):
                before = clock()
                client._post(action, {})
                self.assertEqual(clock()-before, 30)
        self.assertEqual(seen, [15]*12)
        print('broker simulated seconds: renew=30, finish=90, two quarantines=60; socket/DNS not measured')

    def test_existing_late_answer_traces(self):
        probe = PostAnswerLeaseBudgetTests()
        for close, retry, expected in ((2, False, [976]), (30, False, [1004]), (20, True, [994,1005])):
            _, client, _, starts = probe._run(close, retry)
            self.assertEqual(starts, expected)
            print('existing fake: close=%s retry=%s POST starts=%s hub deadline=%s' % (close, retry, starts, client.task['deadline']))

unittest.main(verbosity=2)
'@ | python -
```
