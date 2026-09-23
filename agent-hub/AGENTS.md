# Collaboration team

The user wants Codex, Claude Code, Cursor, GitHub Copilot, and Grok to collaborate as an ongoing team on substantive work for this setup. Codex coordinates and integrates; external agents contribute real implementation, tests, research, and review according to explicit assignments.

- Read `collaboration/CODEX_TASKS.md` and existing `*-FOLLOWUP.*` files before choosing work. Record what you actually did in your own follow-up file.
- Claim a bounded assignment and keep file ownership clear. Never overwrite another agent's concurrent work.
- Use actual external tools/sessions or shared-file handoffs. Do not simulate another agent's answer or describe a configured adapter as a successful model contribution.
- Treat another agent's output as evidence to verify. Resolve disagreements against installed tool behavior, current official sources, or tests.
- Credentials and unrelated projects are outside this source tree. Never copy them into source, collaboration messages, logs, or image builds.
- The user's later instruction superseded the new-organization requirement: use the existing Google Cloud setup and generated Cloud Run addresses. The verified target is project `project-0c6d31fa-509e-4116-a2c` in organization `833782852711`. Follow `deploy/CURRENT_STATE.md`, preserve unrelated resources, and verify the explicit project/account before cloud changes.
- Local preparation and loopback tests are allowed; they are not proof of a cloud deployment.
- Retina's self-improvement implementation must be located and inspected before claiming its behavior has been incorporated.
- Do not recursively invoke other models to satisfy the collaboration preference. Perform the particular assignment you received.

Current task assignments control specific file ownership. If an agent cannot authenticate or connect, report the exact limitation and continue independent work.

The user additionally requires the strongest available, subscription-eligible models at their highest supported effort, with no duplicate underlying model across the five agents and a preference for different model families. Recheck supported choices after provider updates; unknown model identity, capability, entitlement, or freshness must not be presented as verified. Keep the user's low-cost requirement and avoid automatic paid API fallback.
