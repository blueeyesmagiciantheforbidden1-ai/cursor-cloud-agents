# Hosted Qwen adapter

`agent_hub.qwen` implements one text request through Alibaba Cloud Model Studio. It returns text and accounting evidence; it does not execute generated code, invoke tools, search repositories, or automatically retry or change models. The initial profile is explicitly economical and does not establish frontier-quality equivalence.

This module is ready for controller integration. It does not create an Alibaba account, obtain a key, deploy itself, or prove a successful provider call. No provider call was made by its tests. An international pay-as-you-go Model Studio account/key still needs to be configured before live use. Supply the key through the deployment's secret mechanism; do not paste it into task messages, logs, source, or examples. Coding Plan keys beginning `sk-sp-` are rejected.

## Pinned profile

| Field | Value |
| --- | --- |
| Provider | Alibaba Cloud Model Studio |
| Region/scope | Singapore / international |
| API origin and path | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions` |
| Model snapshot | `qwen3.7-flash-2026-07-15` |
| Price revision | `alibaba-singapore-qwen3.7-flash-2026-09-20` |
| Input list price, first 32K tier | USD 0.030 per million tokens |
| Output list price, same tier | USD 0.130 per million tokens |
| Thinking | Explicitly disabled |
| Requested total completion limit | Default 512; configurable from 1 through 512 |
| Maximum call/daily policy caps | USD 0.01 / USD 0.10; caller may lower them |

The snapshot and positive list prices were checked against the official [Qwen3.7 Flash model page](https://www.alibabacloud.com/help/en/model-studio/qwen3-7-flash) on September 20, 2026. Altering the model, region, price revision, or prices requires a reviewed adapter update. The adapter accepts no custom API base or fallback model.

The official [Chat Completions API reference](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions) says the legacy international domain remains functional. The adapter uses `max_completion_tokens`, covering reasoning and answer together, and reserves the documented possible ten-token overrun. `enable_thinking: false` is a top-level HTTP field. It uses non-streaming text only, with search disabled. A `length` finish is reported as `truncated`.

## Bounds and accounting

Messages are limited to 16 text-only system/user/assistant entries and 8,192 total UTF-8 content bytes. At least one user message is required. Multimodal input, message extensions, tool definitions and arbitrary provider parameters are unavailable. Response bytes are capped at 1 MiB and returned answer text at 16,384 UTF-8 bytes. The network timeout defaults to 30 seconds, configurable from 1 to 60; it is a socket-operation timeout, not a distributed deadline. The caller still owns task deadlines and cancellation.

UTF-8 bytes are not an exact token count. Instead of a bytes-divided-by-four estimate, every request reserves the entire 32,768-token first pricing tier, leaving substantial headroom for the small text envelope and message formatting. This is a conservative operational allowance, not a formally verified tokenizer bound. If returned prompt usage lies outside that tier, the adapter refuses to claim a priced result and retains the reservation for reconciliation.

The default reservation is:

```
ceil(32768 * 0.030 + (512 + 10) * 0.130) = 1051 microUSD
```

Costs use positive `Decimal` prices and round upward once per request to integer microUSD. Settlement uses actual reported prompt and completion token counts at the pinned list rates. Provider discounts, cache savings, tax and invoice adjustments are not inferred; `cost_basis: pinned_list_price_upper_bound` distinguishes this budget accounting from an invoice. As shown in the official [thinking usage documentation](https://www.alibabacloud.com/help/en/model-studio/deep-thinking), reasoning tokens are a subset of completion tokens. They are charged once. Unexpected thinking is rejected as a profile violation while its valid usage remains charged.

Timeouts, HTTP errors, invalid responses, wrong model IDs, and absent or invalid usage leave the full reservation held. Valid usage attached to rejected output is charged. An output overrun records its actual known cost, including costs greater than the reservation. A provider error is never treated as a free call. The adapter never repeats a request after any of these outcomes.

## Controller contract

Construct `QwenAdapter(api_key, reserve=reserve, reconcile=reconcile, profile=QwenProfile())` and call `complete(messages, invocation_id=..., mode=...)`. Mode must explicitly be `hybrid` or `frontier`; `local_only` is denied before reservation or networking. Selecting this profile in either hosted mode must be explicit in the caller's routing policy. The adapter does not silently replace another selected model.

Both callbacks receive `(invocation_id, record)` and must return exactly `True` to acknowledge success. No truthy substitute is accepted. Invocation IDs contain 1–128 ASCII letters, digits, underscores or hyphens. IDs must remain unique across restarts and controller replicas.

`reserve` receives:

```text
profile: provider, model, region, api_base, price_revision,
         input_usd_per_million, output_usd_per_million,
         max_completion_tokens, enable_thinking
mode, request_sha256, input_bytes
input_token_reserve, output_token_reserve, reserved_microusd
per_call_limit_microusd, daily_limit_microusd
```

The callback must atomically persist a new invocation and reserve its cost before returning `True`. It must enforce controller-owned limits, concurrency, authority and current leases, and refuse every previously admitted ID, including uncertain and failed attempts. A callback error prevents dispatch. A crash after reservation must leave the hold durable. The fingerprint binds the exact request without storing its prompt; it is an identity check, not an authorization proof.

`reconcile` receives:

```text
profile, reserved_microusd
outcome: completed | truncated | failed | uncertain
error_code: bounded adapter code or null
usage: {prompt_tokens, completion_tokens, total_tokens, reasoning_tokens} or null
actual_cost_microusd: integer or null
cost_basis: pinned_list_price_upper_bound | unknown
over_reservation: boolean
```

`actual_cost_microusd: null` requires retaining the complete hold; it never means zero. For known usage, atomically replace the hold with independently recomputed spend. Preserve prior-day unresolved charges and failures. Settlement must be idempotent and reject conflicting duplicates. A settlement failure returns no successful result: `QwenError` carries the sanitized settlement evidence so the controller can reconcile it without another model call.

Provider text is returned only after successful reconciliation. `QwenError.code` is a fixed diagnostic, `charge_status` distinguishes `not_dispatched`, `uncertain`, and `settled`, and `.settlement` contains available accounting evidence. Raw provider error bodies and credentials are not exposed. The fixed HTTPS transport refuses redirects and ambient proxies. No module-level secret lookup, model discovery or network operation occurs on import.

The callbacks are a required interface, not an in-memory budget implementation. Production must attach the durable cloud ledger and authenticate the invocation before calling this module. Passing a callback that simply returns `True` defeats those guarantees. Existing hub rooms are unchanged by importing this adapter.

## Offline verification

Run `python -W error::ResourceWarning -m unittest discover -s tests -p test_qwen.py -v`. The suite blocks network connects and uses synthetic provider responses. It checks wire parameters, reservation ordering and duplicates, UTF-8 limits, fixed profile/positive prices, forbidden modes and keys, missing usage, uncertain costs, failed-output charges, thinking accounting, over-reservation costs, reconciliation failure, and redirect/error-body handling. Passing these tests does not establish provider availability, account access, or live billing correctness.
