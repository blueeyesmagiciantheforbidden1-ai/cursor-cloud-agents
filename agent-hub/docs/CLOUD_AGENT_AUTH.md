# Cloud worker authentication

Research checked 2026-09-20 against official provider documentation. This is an implementation handoff, not evidence that any cloud worker is installed, authenticated, or connected. No credentials were inspected, logins started, packages installed, or inference requests made for this research.

## Recommended order

After Claude, implement **Codex, then GitHub Copilot**. Both have documented device login suitable for a short-lived private Linux enrollment job; Codex additionally documents preserving refreshed account credentials in private automation. Copilot has a documented token option and a CLI usage cap. Grok is a close third: its account flow and persistence are documented, but a reliable non-inference status/allowance probe still needs implementation. Cursor has an official browserless URL flow, but its credential-cache format and refresh contract are not established by the fetched docs.

Use one on-demand job stream per provider identity, with concurrency one, a bounded execution deadline and no automatic task retry after an uncertain result. Reuse the common worker image design, but give each provider a separate credential secret and service account. These are design recommendations, not deployed resources.

## Native login and installation

| Provider | Official Linux installation provenance | Enrollment on the cloud worker | Non-inference checks |
|---|---|---|---|
| Codex | Official [CLI installer](https://learn.chatgpt.com/docs/codex/cli): `curl -fsSL https://chatgpt.com/codex/install.sh \| sh` | `codex login --device-auth`; account/workspace must permit device login. Complete the displayed code in Chrome. | `codex --version`; `codex login status` |
| Copilot | [Official install docs](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli): `npm install -g @github/copilot` requires Node 22+, or the official `github/copilot-cli` release executable. Installer supports a pinned `VERSION`. | `copilot login --device-code`; complete the returned GitHub device authorization. | Version/help plus login success and sanitized credential-route inspection. Do not invent `copilot login status`; confirm against the pinned release's help. |
| Grok Build | [Official installer](https://docs.x.ai/build/overview): `curl -fsSL https://x.ai/cli/install.sh \| bash`; [enterprise docs](https://docs.x.ai/build/enterprise) also document `npm install -g @xai-official/grok`. | `grok login --device-auth`; complete the displayed URL/code. | `grok version`; `grok inspect --json` identifies effective configuration, not subscription balance or a verified login. |
| Cursor | [Official installer](https://cursor.com/docs/cli/installation): `curl https://cursor.com/install -fsS \| bash`; executable under `~/.local/bin`. | `NO_OPEN_BROWSER=1 agent login` prints a URL to open in Chrome. | `agent --version`; `agent status` reports authentication/account/endpoint. |

The installer commands above identify provider provenance; they were not run. For a reproducible image, download/review the official installer, pin the resolved package/release and record its digest. Never install an unrelated similarly named package. Recheck each adapter's flags with that Linux release's `--help` before deploying. Cursor's generic executable name requires an explicit trusted path, as the existing adapter already enforces.

## Credentials and subscription routing

**Codex.** [Authentication](https://learn.chatgpt.com/docs/auth) distinguishes ChatGPT subscription login from API billing. Set `forced_login_method = "chatgpt"` and `cli_auth_credentials_store = "file"` in the cloud worker's private configuration; keep a dedicated `CODEX_HOME`. `auth.json` must remain writable for refresh. Do not pass OpenAI API keys or custom-provider overrides. The supported device flow is `codex login --device-auth`.

The [private automation guide](https://learn.chatgpt.com/docs/auth/ci-cd-auth) explicitly describes restoring managed ChatGPT `auth.json`, letting Codex refresh it, and persisting the updated file. It requires a trusted private runner and serialized use of each credential copy. Repeatedly reseeding the original secret discards refreshes. Do not implement a separate OAuth refresh client. Confirm `auth_mode == "chatgpt"`, refresh-token presence, CLI status and effective model configuration without logging credential values. Subscription routing does not itself prove remaining allowance or prevent separately enabled paid credits.

**Copilot.** [Authentication documentation](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli) supports browser/device OAuth and personal-account fine-grained PATs with **Copilot Requests** permission. Classic PATs are unsupported. Linux uses libsecret; headless systems can prompt to store credentials in `~/.copilot/config.json`. Persist that private configuration, or inject the explicitly chosen token through `COPILOT_GITHUB_TOKEN`. Environment precedence is that variable, then `GH_TOKEN`, then `GITHUB_TOKEN`, ahead of stored login; reject unintended overrides. A PAT expires according to its configured lifetime and must be replaced deliberately. The docs do not promise a universal OAuth refresh lifetime. Every `COPILOT_PROVIDER_*` override must be rejected for this subscription route: BYOK model routing supersedes GitHub login.

**Grok Build.** Official [authentication source](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md) documents `~/.grok/auth.json`, owner-only permissions and automatic token refresh; failed refresh requires sign-in. Isolate `GROK_HOME` and preserve updated credentials between jobs. [Enterprise guidance](https://docs.x.ai/build/enterprise) documents device login for containers and precedence: model API key, model environment key, active session, then `XAI_API_KEY`. Inspect all effective model/config layers. Pin `disable_api_key_auth = true` under `[grok_com_config]` in root-owned requirements, and independently reject custom third-party endpoints; that policy does not disable BYOK endpoints.

**Cursor.** [Authentication](https://cursor.com/docs/cli/reference/authentication) documents `agent login`, `NO_OPEN_BROWSER=1`, `agent status`, local credential storage, and the distinct `CURSOR_API_KEY` user-key route. Prefer account login first. The fetched docs do not specify a portable OAuth cache path/format or automatic refresh contract: verify these using the pinned Linux CLI in an isolated private home before designing persistence. Do not copy the desktop editor's cookies or credentials. A Cursor-issued user key is not an OpenAI/Anthropic key; enable the existing `allow_cursor_user_key` gate only after explicitly verifying its provenance and account billing route.

## Cost controls to verify before the first task

| Provider | Verified subscription behavior | Required check beyond login |
|---|---|---|
| Codex | ChatGPT login uses account entitlements; API login is separately billed. | Account allowance and any paid-credit settings remain independent checks; reject API/provider overrides and stop on limit errors. |
| Copilot | Included monthly AI credits apply across CLI and other Copilot surfaces. Additional usage requires a spending budget. [Billing](https://docs.github.com/en/copilot/concepts/billing-and-usage/individuals/billing) | Use included allowance first, then only the existing credit/budget allowance authorized by the owner. Verify the actual payer and preserve provider spending limits; do not enable a new paid budget. Add `--max-ai-credits 30` only if the pinned CLI supports it: 30 is the documented minimum and the cap is **soft**, so an in-flight response may exceed it. [Session limits](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/set-session-limit) |
| Grok | Build participates in the paid weekly usage pool; extra credits and Auto Top Up can extend usage. [Official FAQ](https://docs.x.ai/grok/faq) | Verify the subscribed identity, weekly allowance and Auto Top Up/extra-credit policy. No purchase, upgrade or API fallback on exhaustion. |
| Cursor | The CLI is part of Cursor subscriptions. [CLI announcement](https://cursor.com/blog/cli) | Verify the account plan and existing spending controls. Included usage takes priority; existing purchased credits are authorized, but no new purchase or on-demand budget should be enabled. [Overages](https://prod.cursor.com/help/account-and-billing/overages) |

Do not equate a successful login or a token's existence with a zero-overage guarantee. If a provider lacks a supported non-inference allowance API, display **unknown** and use the provider's own spending controls; do not scrape browser cookies or guess balances. Confirm account-specific controls through the authorized provider UI before inference.

## Gaps in the current implementation

`agent_hub/adapters.py` creates bounded read-only commands for all five providers, and an explicitly configured Claude project-work mode with Read/Grep/Glob/Edit/Write/Bash. Claude now has a pinned rootless Linux profile and a same-process account/settings handshake before task release. The model policy permits included subscription usage followed by verified existing credits. Other providers still require trusted account/configuration preflights and verified project-work profiles. No production worker has completed an actual provider task. A hub request or model reply must never be treated as authentication or entitlement evidence.

Cloud jobs also need credential restore/writeback with refresh-safe serialization, permission-limited workspace delivery, an actual task trigger, and real completion/heartbeat reporting. A worker becomes connected only after its cloud process authenticates to the hub and reports actual telemetry; successful end-to-end provider output is separate evidence. Cloud storage for refreshed credentials must not be a public bucket, shared task artifact or read-only secret mount presented as writable persistence.

## Enrollment checkpoint — 2026-09-20 23:27 UTC

- The live MyHero connector returned Google Cloud hub status successfully at 23:27:17 UTC. Hub healthy; zero of five workers connected; account usage remains unconnected.
- Cursor's supplied user credential was verified with the official read-only `/v1/me` endpoint against the intended blueeyes owner. `/v1/models` returned recommended models; this is not a complete CLI entitlement catalog or billing verification. No hosted Cursor agent was launched.
- Claude browser evidence identifies personal Max 20x on the intended Ryan account, existing credits, a $40 monthly provider cap, and auto-reload off. A cloud OAuth credential has not yet been obtained.
- Codex and Grok's previous isolated device login processes exited without fresh credential files. Their old codes are historical and must not be reused. Restart only when the browser is ready.
- Chrome disappeared from the browser gateway after the session interruption. The user has been asked to reconnect it. Edge and the in-app browser do not establish the required Chrome session.
- Claude's cloud entrypoint accepts an explicitly configured staged fleet, validates it, and defaults to all five agents. This supports staged commissioning without claiming fleet-wide model diversity is already running.
- Focused offline verification: 14 packaging tests passed; 86 model/runtime/worker tests completed with one Windows capability skip. The first sandbox test attempt failed because temporary fixture directories were inaccessible; the authorized rerun passed.
