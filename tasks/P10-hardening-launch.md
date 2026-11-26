# P10 — Hardening and Launch

Goal: make the platform safe to trust with real money, real customers and real brand reputation, then roll autonomy out in stages.
Needs: P0–P9 substantially complete (hardening tasks start earlier per channel; this phase is the final gate).

---

## P10.1 Security review (L)
- P10.1.1 Re-run the threat model from P0.17 against the built system; verify every mitigation has a test.
- P10.1.2 External penetration test scope and execution (web, API, agent prompt-injection paths, webhook endpoints, secrets handling); fix or accept each finding with owner sign-off.
- P10.1.3 Secrets audit: no secrets in repo, images, logs or prompts; rotation drill for every provider credential and the master key.
- P10.1.4 Access review: roles, tokens, admin surfaces, provider-side permissions (least privilege), stale connections revoked.
- P10.1.5 Supply-chain review: dependency licences, SBOM, pinned images, CI permissions, branch protection.
- Done when: no open critical or high findings; residual risks documented and signed.

## P10.2 Reliability and disaster recovery (L)
- P10.2.1 Load test (agents, ingestion, dashboards, webhooks bursts) with realistic data volumes; capacity plan.
- P10.2.2 Chaos and failure drills: database failover, Redis loss, worker crash mid-action, provider outage, rate-limit storm, expired tokens across all providers at once.
- P10.2.3 Backup restore drill from scratch including secrets and object storage; RPO and RTO measured against targets.
- P10.2.4 Runbooks for every alert; on-call rota and escalation; status page (internal).
- P10.2.5 Cost review: infrastructure, LLM, media generation, data vendors; budgets and alerts finalised.
- Done when: RPO / RTO targets met in drills and every alert has a runbook.

## P10.3 Correctness of money-moving paths (M)
- P10.3.1 Independent review of guardrails, budget math, micros / currency handling, idempotency and rollback with property tests and mutation tests to threshold.
- P10.3.2 Reconcile actions with provider audit trails (change history) for a full test month; zero unexplained changes.
- P10.3.3 Kill-switch drills (global, per provider, per agent) measured for latency.
- Done when: no path can spend beyond caps in tests, including under retries and concurrent runs.

## P10.4 Legal, privacy and platform compliance (M) [decision: counsel]
- P10.4.1 Review with counsel: CASL (commercial electronic messages, consent, identification, unsubscribe), PIPEDA (consent, purpose, retention, access and deletion), platform terms (Meta, Google, LinkedIn, OpenAI ads and automation policies), AI-generated content disclosure rules, advertising standards for the industry.
- P10.4.2 Privacy documentation: data inventory, retention schedule, processors list (including LLM and media vendors and their data handling), DPIA-style assessment for DM automation.
- P10.4.3 Implement outcomes: consent capture, opt-out handling, deletion tooling, banned-claims lists, disclaimers, cookie / consent integration for tracking.
- Done when: written sign-off with actions implemented.

## P10.5 Quality and evaluation gate (M)
- P10.5.1 Full eval run across all agents; thresholds for approval rate, factual accuracy, policy compliance, brand fit; injection red-team results.
- P10.5.2 Human review sample of outputs per channel scored by the owner; feed results into memory and prompts.
- Done when: thresholds met and the owner accepts the quality per channel.

## P10.6 Staged autonomy rollout (L)
Needs: P1.5.8 simulation mode, P10.1–P10.5.
- P10.6.1 Stage 0 — Shadow: everything runs in simulation, humans compare agent proposals with what they would have done; measure agreement.
- P10.6.2 Stage 1 — Assisted (`L2` everywhere): agents prepare, humans approve; track approval rate, edit distance, time saved.
- P10.6.3 Stage 2 — Conditional autonomy (`L3`) for low-risk kinds only after thresholds hold for a defined period (for example spam handling, pausing clearly failing ads within caps, publishing pre-approved content types on schedule); every grant has an expiry and review date.
- P10.6.4 Stage 3 — Broader autonomy for proven domains with tighter monitoring; never for R3 actions.
- P10.6.5 Automatic demotion rules verified; incident procedure ("stop, assess, restore, review, learn").
- Done when: each stage has entry and exit criteria, recorded evidence and owner sign-off.

## P10.7 Onboarding and documentation (M)
- P10.7.1 Owner handbook: how to command, approve, correct, teach the system, use kill switches, read reports.
- P10.7.2 Operator handbook: deployments, rollbacks, key rotation, provider outages, version upgrades.
- P10.7.3 Training material inside the product (empty states, tours, tooltips).
- P10.7.4 Changelog and release notes process.
- Done when: a new team member can operate the system from the handbooks alone.

## P10.8 Post-launch operating cadence (S)
- P10.8.1 Weekly: review approvals backlog, rejection reasons, memory proposals, failed runs, cost.
- P10.8.2 Monthly: API drift review, dependency updates, security updates, budget caps review, autonomy grant review.
- P10.8.3 Quarterly: strategy review with market report, agent roster review (add, retire, retune), roadmap.
- Done when: the cadence exists as recurring calendar items and dashboards.
