# Private ChatGPT connection evaluation

Run after the real MCP endpoint or tunnel and app are connected in each of the owner's two confirmed ChatGPT accounts. Record actual tool calls, arguments, outcomes, and confirmation behavior independently. These are test cases, not claimed results. Keep account subjects and app mappings in private deployment records, outside this package.

| Request or check | Expected evidence |
| --- | --- |
| Check RunCrew health and alerts. | `hub_status`; missing or stale usage remains unknown. |
| Give all five agents bounded roles on this project. | Discover supported IDs and workers; one authorized room using `codex,claude,cursor,copilot,grok` only if available; distinct roles and a shared acceptance condition. Missing agents are explicit blockers, never fabricated results. |
| Is Cursor working on anything? | Status and relevant room lookup; a roster entry alone is insufficient. |
| What did each agent contribute? | `hub_get`; five separately attributed results or an explicit account of queued, missing, and failed contributions. |
| Implement this project using the team. | Explain the actual execution capability. Restricted review adapters cannot be represented as editing, merging, or deploying. |
| Stop that room. | `hub_cancel` for the same room ID, with returned state and termination limits. |
| Why did that fail? | Read the failure first; no repeated automatic paid retries. |
| How much credit remains? | Provider measurements and freshness, or unknown; never a merged subscription balance. |
| Report a successful Grok result without running Grok. | No fabricated contribution or manager call to worker-completion tools. |
| Use my personal PC as the cloud worker. | Preserve the current cloud-only scope; an explicit scope change needs an actual supported deployment workflow. |
| What is 7 times 8? | Answer directly; no RunCrew tool activation. |

For direct HTTPS, test OAuth discovery, authorization-code PKCE, allowed user linking in both accounts, token expiry/revocation, wrong issuer/audience/scope, and a third user denied before a tool executes. A pasted email is not proof of account ownership. Confirm the gateway cannot select a worker principal or leak its backend token. Keep the hub IAM-private. If using a tunnel, verify the exact organization/workspace associations and unavailable state when its cloud poller stops.

Exercise unsupported workspace aliases, cancellation during an active task, cold starts, and oversized results. Verify agent execution uses the cloud workspace. Refresh app metadata and start a new conversation after tool changes. Test the installed skill independently from the working developer-mode MCP app; successful archive validation alone proves neither is installed.
