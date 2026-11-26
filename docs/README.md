# ITBCPro Marketing Platform — Project Plan

An internal, agent-operated marketing platform for itbcpro. A human manager gives goals in one command box;
a team of specialised AI agents plans, creates, publishes, monitors and optimises work across ads, website,
and social channels. Every meaningful action passes through one audited approval layer.

Plan baseline date: **2026-09-29**. Every external fact in this plan was verified on that date
(see `external-platform-facts.md`). Facts about third-party APIs decay fast: each phase starts with a
re-verification task and the platform pins API versions in configuration, never in code.

## Document index

| File | Purpose |
|---|---|
| `architecture.md` | System design, bounded contexts, domain model, agent runtime, memory, approvals, ADR list |
| `engineering-standards.md` | Language rules, SOLID rules, i18n, Tailwind, testing, security, Definition of Done |
| `external-platform-facts.md` | Verified facts, sources and open verification items for every external platform |
| `../tasks/P00-foundation.md` | Repo, environments, Django/Next skeleton, auth, secrets, i18n, CI/CD, access applications |
| `../tasks/P01-agent-core.md` | LLM gateway, prompts, tools, agent runtime, approvals, memory, learning loop, command box |
| `../tasks/P02-wordpress-seo-geo.md` | WordPress connector, content intelligence, SEO, GEO, article pipeline, contact tracking |
| `../tasks/P03-meta-ads.md` | Meta Marketing API: campaigns end to end |
| `../tasks/P04-google-ads.md` | Google Ads API: campaigns end to end |
| `../tasks/P05-openai-ads.md` | OpenAI Ads API: campaigns end to end |
| `../tasks/P06-instagram-facebook.md` | Posts, reels, stories, comments, DMs, Facebook pages |
| `../tasks/P07-linkedin.md` | LinkedIn organisation page publishing, engagement, analytics |
| `../tasks/P08-market-intelligence.md` | Trends, competitors, market gaps |
| `../tasks/P09-unified-reporting.md` | Cross-channel reporting, attribution, executive briefings |
| `../tasks/P10-hardening-launch.md` | Security, reliability, legal, staged autonomy rollout |

## Phase order and dependencies

```
P0 Foundation ──► P1 Agent Core ──┬─► P2 WordPress / SEO / GEO ──┐
      │                           ├─► P3 Meta Ads ───────────────┤
      │                           ├─► P4 Google Ads ─────────────┼─► P9 Unified reporting ─► P10 Launch
      │                           ├─► P5 OpenAI Ads ─────────────┤
      │                           ├─► P6 Instagram / Facebook ───┤   (P3 provides the Meta app + token layer for P6)
      │                           ├─► P7 LinkedIn ───────────────┤
      │                           └─► P8 Market intelligence ────┘
      └─► P0.20 Access applications (starts on day 1, runs in parallel with everything)
```

P2 goes first after the core because it has no external approval wait (the WordPress site is under our control),
so it delivers value while Meta / Google / LinkedIn reviews are pending.

## Long-lead items (start on day 1)

External approvals take weeks and are the real critical path. They are task `P0.20` and must be submitted
before any feature work depends on them: Meta app review and business verification, Google Ads developer
token, LinkedIn Community Management API access, OpenAI Ads account and API key, Google Cloud project
(GSC, GA4, PageSpeed), video-generation provider accounts.

## How to read the task files

- IDs: `P<phase>.<task>` → subtask `P<phase>.<task>.<n>` → microtasks as checkboxes.
- Size: `S` under half a day, `M` up to 2 days, `L` up to 5 days, `XL` must be split before starting.
- `Needs:` lists hard dependencies. `Done when:` lists testable acceptance criteria.
- `[spike]` marks a time-boxed investigation whose output is a written decision (ADR or fact update).
- `[decision]` marks a question only the owner can answer; see the list below.
- Tasks never contain code. Implementation details that matter are stated as constraints.

## Open decisions for the owner

1. Hosting target (own VPS / cloud provider / region). Affects P0.16.
2. Which SEO plugin is active on the WordPress site (Rank Math, Yoast, SEOPress) and whether Polylang or WPML is used. Discovered automatically in P2.1, confirm anyway.
3. Content languages and target markets (site languages, ad languages, UI languages). The application UI is English only; content languages for generated ads, posts and articles are configurable per workspace.
4. Existing ad accounts, Business Manager, Instagram / Facebook / LinkedIn page ownership and admin roles.
5. Monthly hard budget caps per platform and per day (the platform enforces them independently of the AI).
6. Who approves actions (single approver or roles). Default plan: owner plus optional delegates.
7. Video and image generation provider preference and budget (P3.8, P6.7).
8. Notification channels (email, Telegram, Slack, WhatsApp) for alerts and DMs escalation.
9. Legal review scope: Canadian anti-spam law (CASL) and privacy law (PIPEDA) for messaging and data retention.
10. Interpretation check: "track all website connects" is planned as tracking all inbound contact events (forms, emails, calls, chats) in P2.16. Confirm.

## Global Definition of Done

A task is done only when all of these hold: code merged with review, tests at the right level pass in CI,
no user-visible string is hard-coded (catalog entries exist), no non-English text
exists in source or comments, lint / type / security gates are green, migrations are reversible and
zero-downtime safe, observability (logs, metrics, traces) exists for new flows, docs / ADR / runbook updated,
and the audit trail records every state-changing action. Full list in `engineering-standards.md`.
