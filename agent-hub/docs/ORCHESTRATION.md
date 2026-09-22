# One hub, separate providers

The owner wants one private cloud workspace for Codex, Claude Code, Cursor,
GitHub Copilot, and Grok Build. Alibaba is available but is not an exclusive
provider. Keep the existing Google Cloud deployment target and make hosted
Qwen optional. Do not migrate hosting or provision another always-on server
merely to add a model.

## Recommended operating model

The hub owns task state, permissions, bounded handoffs, cancellation, results,
and the status dashboard. Each coding agent runs through its vendor-supported
CLI or SDK, under its own credentials and resource limits. Provider credentials
stay with the appropriate worker. An Alibaba Model Studio key authorizes Qwen
model calls; it does not authenticate other vendors or provision Alibaba cloud
resources.

Use deterministic routing for routine work. Pick one explicitly configured
worker whose capability and availability match the task, and add a different
reviewer when the task warrants it. A model may propose a plan for more complex
work, but its output is untrusted data: it cannot modify credentials, budgets,
tool permissions, model choices, or executable command lines. Existing explicit
model and effort selections remain binding; no silent downgrade or fallback.

All five agents must contribute to the owner's projects. Give them distinct
project responsibilities and handoffs, rather than making each repeat every
task. Choose small, distinct tasks, share only necessary context, and keep repository
edits isolated until checked. Use existing subscriptions where the vendor's
supported authentication and plan permit it. Credits, quotas, usage, and bills
remain separate; API compatibility does not combine entitlements.

Keep initial costs bounded: scale the HTTP services to zero, use on-demand
workers, avoid GPUs and training, and do not introduce a paid gateway or a
second database for this first integration. An application budget bounds only
the paths that enforce it, not the whole cloud/provider account. The Qwen
ledger's limits do not yet cover CLI worker subscription consumption.

## What is implemented and what is not

- Ordered collaboration rooms, fenced leases, bounded history and cancellation
  are implemented and tested offline.
- Worker adapters initially perform restricted review/analysis. They do not
  establish autonomous repository editing or promotion.
- The dashboard has an agent roster; roster membership does not prove that a
  cloud worker is authenticated or connected.
- Hosted Qwen has a fixed model profile, durable budget accounting and a
  manager-only completion endpoint. It has not made a live call, and is not
  the mandatory coordinator.
- The separate routing and research modules remain offline mechanisms. They
  are not connected to every production dispatch path.
- No hub/dashboard Cloud Run deployment or five-agent cloud run is verified.
  Test each worker separately, then prove a two-agent handoff, before enabling
  a larger workflow. Preserve usage as unknown when the provider does not
  return authoritative numbers.

## Vendor interfaces checked on 2026-09-20

- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) supports application and
  workflow integration; the app server handles richer client interactions.
- [Claude Code programmatic mode](https://code.claude.com/docs/en/headless)
  supports scripted use.
- [Cursor CLI](https://prod.cursor.com/help/integrations/cli) supports headless
  automation and its own authentication.
- [GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/overview)
  supports programmatic sessions and tool permissions.
- [Grok Build headless mode](https://docs.x.ai/build/cli/headless-scripting)
  supports scripted runs and structured output.
- [Alibaba application types](https://www.alibabacloud.com/help/en/model-studio/application-introduction)
  documents a legacy Singapore Application Development restriction for accounts
  without applications created before April 21, 2025. Do not depend on that
  console for this newly registered account. The existing direct model API
  adapter is the intended Qwen integration.

These documents establish integration mechanisms, not this owner's cloud
authentication, account entitlements, successful deployment, or measured model
performance.
