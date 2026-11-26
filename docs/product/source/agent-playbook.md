# AI Agent Playbook — Building the CRM Engine

Nov 18, 2026 · @Ahmad

## 1. Summary

Build the CRM as a **separate engine** that sits beside your Django core and plugs into your Next.js front end through fixed contracts. Run AI agents as a disciplined team: you own architecture and contracts, agents implement small, well-specified tasks, and automated gates — not trust — decide what merges.

**The operating model in seven rules:**

1. **Contracts first.** Before any feature code, you approve the OpenAPI spec, event schemas and database ownership. Everything else is built against them.
2. **Spec → plan → code.** Every task starts from a written spec section, gets an agent-written plan you approve, and only then becomes code.
3. **Small tasks, fresh context.** One task = one branch = one pull request of a few hundred lines, done in a fresh agent session.
4. **The repo teaches the agent.** `AGENTS.md` / `CLAUDE.md` files, architecture decision records and module docs live in the repo, so every new session starts informed.
5. **Machines enforce, agents comply.** Tests, type checks, linters, tenant-isolation tests and security scans run in CI and hooks; an agent cannot merge a failing build.
6. **Two reviewers.** A separate review agent checks every PR before you do; you review plans and risky diffs, not every line.
7. **Humans decide what is irreversible.** Data-model changes, contracts, security, money and anything that sends messages to real customers need your sign-off.

The product spec this playbook builds is the companion doc: [GoHighLevel CRM & Omnichannel Messaging — Reverse-Engineering Specification](https://claude.ai/artifact/U1zQmR144pJAeiUjXtUsia), with its Automations, Gap analysis and Security tabs.

## 2. Target architecture

Run the CRM as its own Django service — **the CRM engine** — with its own database, workers and webhook ingress, talking to the core only through APIs and events; the Next.js app hosts the CRM screens as a module.

&#91;embedded content: target architecture · core, CRM engine, web app, providers\]

The engine never reads the core's database: it trusts the core's tokens for identity and learns about organisations and users from events, while all provider traffic enters and leaves through the engine alone.

### 2.1 Why a separate engine (and why still Django)

| Option | Pros | Cons | Verdict |
| --- | --- | --- | --- |
| CRM as apps inside the Django core | One deploy, shared ORM, simple joins | CRM workload (webhooks, queues, realtime, bulk sends) competes with the core; tangled models; agents edit core code by accident | Avoid |
| **Separate Django service (recommended)** | Same language, conventions and tooling as the core, so agents reuse patterns; independent scaling and deploys; hard boundary agents cannot cross | Needs contracts, auth between services, event bus | **Use** |
| Separate service in another stack (Node, Go) | Good for realtime-heavy parts | Two backend stacks for agents and humans to maintain | Only for a small realtime gateway, if ever |

Inside the engine, keep a **modular monolith**: one deployable with clear internal modules (contacts, conversations, channels, automation…) rather than many microservices. Split later only where load demands it.

### 2.2 Components

| Component | Tech | Responsibility |
| --- | --- | --- |
| Django core (existing) | Django | Identity provider, organisations, billing, everything that isn't CRM |
| Next.js app (existing) | Next.js (App Router) | All UI; the CRM is a route group (`/crm/...`) using a typed CRM client |
| CRM API | Django + DRF or Django Ninja, ASGI | REST API for the UI, the core and third parties; OpenAPI generated from code |
| Webhook ingress | Same codebase, separate process | Twilio, Meta, email providers; verify, dedupe, enqueue |
| Workers | Celery (or Dramatiq) + Redis; Temporal optional for the automation engine | Sends, status callbacks, imports, bulk jobs, workflow steps |
| Realtime gateway | Django Channels (or a small dedicated service) | WebSocket pushes to the inbox |
| CRM database | PostgreSQL (separate database or cluster) with RLS | All CRM data; the core never reads it directly |
| Event bus | Redis Streams, RabbitMQ or Kafka (start simple) | Core ↔ CRM events, internal events |
| Object storage | S3-compatible | Attachments, recordings, imports |

## 3. Integration contracts

Five contracts define how the engine joins the larger system. Write them first, as files in the repo, and change them only through a reviewed PR — agents build against them and never invent their own.

### 3.1 Identity

- The **Django core is the identity provider.** It issues short-lived JWT access tokens (claims: `sub`, `org_id`, `roles`, `scopes`, `exp`) and publishes a JWKS endpoint.
- The CRM engine only **validates** tokens (signature via JWKS, audience, expiry); it never stores passwords or runs its own login.
- Service-to-service calls (core ↔ engine) use separate machine tokens (OAuth client credentials) with narrow scopes.

### 3.2 Tenancy and users

- **One core organisation = one CRM tenant**, with the same UUID. The engine creates the tenant when it receives `org.created` and freezes it on `org.suspended`.
- Users are mirrored, not owned: the engine keeps a minimal `crm_users` table (id, name, email, avatar, active) updated from `user.*` events. CRM-specific roles and permissions live in the engine.
- Agency / sub-account hierarchy (main spec, section 2) maps onto core organisations if the core supports parent–child orgs; otherwise it lives in the engine.

### 3.3 APIs

- REST under `/crm/api/v1/`, described by **one OpenAPI file generated from code** and committed to `contracts/openapi/crm.yaml`; CI fails if code and file disagree.
- Conventions: cursor pagination, filtering syntax, RFC 9457 problem-details errors, `Idempotency-Key` header on POSTs that send or charge, ISO-8601 UTC timestamps, UUIDv7 IDs.
- The Next.js app uses a **TypeScript client generated** from the OpenAPI file (e.g. openapi-typescript or Orval) — agents never hand-write fetch calls or types.
- Breaking changes only in a new version (`/v2`); additive changes allowed.

### 3.4 Events

- One envelope for every event: `id`, `type`, `version`, `tenant_id`, `occurred_at`, `actor`, `data`; schemas as JSON Schema in `contracts/events/`.
- Published through a transactional **outbox** table so no event is lost or sent for a rolled-back change; consumers are idempotent (dedupe on `id`).

| Direction | Events |
| --- | --- |
| Core → CRM | `org.created`, `org.updated`, `org.suspended`, `user.created`, `user.updated`, `user.deactivated`, `membership.changed`, `plan.changed` |
| CRM → core | `crm.usage.recorded` (for billing), `crm.contact.created`, `crm.deal.won`, `crm.tenant.provisioned` |
| CRM internal | `message.inbound`, `message.outbound`, `message.status`, `contact.updated`, `opportunity.stage_changed`, `workflow.step_executed` |

### 3.5 UI integration

- CRM screens live in the Next.js app under `app/(crm)/crm/...`, built from the shared design system so the CRM looks native.
- The browser calls the CRM API with the user's access token (through your API gateway or a Next.js route-handler BFF); the inbox opens a WebSocket to the realtime gateway with the same token.
- Cross-links: the core can embed CRM widgets (e.g. a contact card) through exported React components from `packages/crm-ui`.

### 3.6 Data ownership

- Each service owns its tables; **no cross-database joins and no shared tables**. The core asks the CRM through the API; the CRM learns about the core through events.
- Reporting across both goes through events into an analytics store, not direct DB access.

## 4. Repository layout

Put the core, the CRM engine, the web app and the contracts in **one monorepo** if you can: agents work best when they can read the contract and both sides of it in one checkout. If the core must stay in its own repo, keep the CRM engine and a shared `contracts` package together and pull the core in read-only.

```
platform/
├─ AGENTS.md                  # root rules for every agent (section 6)
├─ CLAUDE.md                  # imports AGENTS.md + Claude-specific notes
├─ contracts/                 # source of truth, changed only by approved PRs
│  ├─ openapi/crm.yaml
│  ├─ events/*.schema.json
│  └─ CHANGELOG.md
├─ apps/
│  ├─ core/                    # existing Django core (agents: read-only unless the task says otherwise)
│  ├─ crm_engine/              # new Django service
│  │  ├─ AGENTS.md             # engine-specific rules
│  │  ├─ config/               # settings, urls, asgi, celery
│  │  ├─ crm/
│  │  │  ├─ tenancy/           # tenants, RLS helpers, provisioning consumers
│  │  │  ├─ records/           # generic record engine: objects, fields, associations
│  │  │  ├─ contacts/  companies/  opportunities/
│  │  │  ├─ conversations/     # threads, messages, inbox
│  │  │  ├─ channels/          # one sub-package per adapter: sms_twilio, email_ses, whatsapp_cloud, meta
│  │  │  ├─ sending/           # send gateway: consent, windows, throttling
│  │  │  ├─ automation/        # workflow engine
│  │  │  ├─ billing_usage/  reporting/  audit/
│  │  │  └─ events/            # outbox, publishers, consumers
│  │  └─ tests/
│  └─ web/                     # existing Next.js app
│     └─ app/(crm)/crm/...      # CRM routes
├─ packages/
│  ├─ crm-client/              # generated TypeScript API client
│  ├─ crm-ui/                  # CRM React components
│  └─ ui/                      # shared design system
├─ docs/
│  ├─ product/                 # the spec, split per module (section 5)
│  ├─ architecture/            # overview, ADRs
│  ├─ tasks/                   # task files (section 7)
│  └─ progress.md              # what is done, what is next
├─ .claude/                   # agents/, commands/, settings.json (permissions, hooks)
└─ infra/                     # docker-compose for local dev, CI, deploy
```

Each Django module owns its models, services, API views, tasks and tests. Cross-module calls go through a small public `services.py` per module, never through another module's models — the same boundary discipline you want between core and engine.

## 5. Turning the spec into agent-ready docs

The reverse-engineering spec describes what GHL does; agents need small, decided documents that say what **your** engine must do. Convert it once, before coding starts.

1. **Export** the spec (and its tabs) to Markdown from the doc's export menu.
2. **Split** it into one file per module in `docs/product/`, e.g. `01-tenancy.md`, `02-records-and-fields.md`, `03-contacts.md`, `04-pipelines.md`, `05-inbox.md`, `06-sms-voice.md`, `07-email.md`, `08-whatsapp.md`, `09-meta.md`, `10-automation.md`, `11-reporting.md`, `12-security.md`. An agent can do the split; you review it.
3. **Decide** every “GHL does X” into “we do X / we do Y instead / not in MVP”. Record each non-obvious choice as an ADR.
4. **Add acceptance criteria** to every module: testable statements (“Given a contact with SMS DND, when a workflow sends SMS, then the message is blocked and logged with reason `dnd`”).
5. **Keep a glossary** (`docs/product/glossary.md`) so agents use one word per concept: tenant, not location/sub-account/org.

**Module doc template** (`docs/product/NN-module.md`):

```markdown
# Module: <name>
Status: draft | approved   Owner: <you>   Phase: <0-5>
## Purpose (2-3 sentences)
## In scope for MVP / Out of scope
## Data model (tables, key fields, constraints, indexes)
## API (endpoints, request/response, permissions) -> link to contracts/openapi
## Events emitted / consumed -> link to contracts/events
## Business rules (numbered, each testable)
## Edge cases and failure handling
## Security notes (tenancy, permissions, PII)
## Acceptance criteria (Given / When / Then)
## Open questions
```

**ADR template** (`docs/architecture/adr/NNNN-title.md`): Context → Decision → Alternatives considered → Consequences. Agents must read relevant ADRs before planning and may propose a new ADR, but only you accept it.

Rule of thumb: a module doc an agent reads at the start of a task should be **under \~300 lines**. Link to details instead of pasting them, because context is the scarcest resource an agent has.

## 6. Agent instruction files

Use **AGENTS.md** as the single source of agent rules — [an open format used by over 60,000 open-source projects](https://agents.md/) and read by most coding agents — and a thin `CLAUDE.md` that imports it for Claude Code. Put a short root file at the top and a module-specific file in each app; nested files refine the root rules for their folder.

Keep each file short: guidance from practitioners is to [keep CLAUDE.md under about 150 lines for reliable adherence](https://mintlify.com/shanraisshan/claude-code-best-practice/tips/workflow-tips). Instruction files are advisory; [anything that must happen every time belongs in a hook or CI, not in prose](https://medium.com/@rajasekar-venkatesan/making-the-most-of-claude-code-best-practices-hidden-features-b5ed99fbd61f).

### 6.1 Root `AGENTS.md` (template)

```markdown
# Platform - agent guide
## What this repo is
Django core (apps/core), CRM engine (apps/crm_engine, Django), Next.js web (apps/web).
Contracts in /contracts are the source of truth between them.

## Before you code
1. Read docs/progress.md, the task file you were given, and the module doc it links.
2. Read relevant ADRs in docs/architecture/adr.
3. Write a plan (files to change, tests to add, risks) and wait for approval.

## Hard rules
- Never edit /contracts, apps/core, migrations of other modules, CI config or .claude/ unless the task says so.
- Every CRM table has tenant_id and RLS; every query runs inside a tenant context.
- No secrets in code, tests, logs or fixtures. Use env vars and the fake providers.
- Never call real Twilio/Meta/email APIs; use the simulators in tests and local dev.
- Do not delete or weaken tests to make them pass. If a test is wrong, say so.
- Keep PRs under ~400 changed lines; split otherwise.

## Commands
- Backend: make crm-test, make crm-lint, make crm-typecheck, make crm-migrations-check
- Web: pnpm --filter web test, pnpm --filter web lint, pnpm --filter web typecheck
- Contracts: make contracts-check  (OpenAPI matches code, client regenerated)
- Everything: make verify   (must pass before you open a PR)

## Definition of done
Acceptance criteria met, tests added (incl. a cross-tenant test for new endpoints),
make verify green, docs/progress.md updated, PR description uses the template.
```

### 6.2 `apps/crm_engine/AGENTS.md` (template)

```markdown
# CRM engine - agent guide
- Python 3.12, Django 5.x, DRF/Ninja, Celery, PostgreSQL 16, pytest.
- Module boundaries: import other modules only through their services.py.
- Models: UUIDv7 primary keys, tenant_id FK, created_at/updated_at, soft delete where the module doc says.
- Business logic in services, not in views or serializers. Views = auth + validation + call service.
- All outbound messages go through crm.sending.gateway.send(); never call an adapter directly.
- Side effects publish events via crm.events.outbox.publish() in the same transaction.
- Celery tasks are idempotent; pass IDs, not objects; use the idempotency key helper.
- Tests: pytest + factory_boy; use tenant fixtures; mark slow tests; no network.
```

### 6.3 `CLAUDE.md` and specialised sub-agents

- Root `CLAUDE.md`: `@AGENTS.md` plus a few Claude-specific notes (preferred plan format, which sub-agents exist).
- `.claude/agents/` holds reusable roles, each with its own prompt and tool limits:
  - `planner` — read-only; turns a task file into a plan.
  - `reviewer` — read-only; reviews a diff against the checklist in section 11.
  - `security-reviewer` — read-only; checks tenancy, auth, secrets, injection, SSRF.
  - `test-writer` — writes tests from acceptance criteria before implementation.
- `.claude/settings.json`: allowed commands, denied paths (`contracts/`, `apps/core/`, `.env*`), and hooks (format on edit, block commits when `make verify` fails).

## 7. Task breakdown

Break work into **Phase → Epic (one module) → Task (one PR)**. The task is the unit you hand to an agent; its quality decides the quality of the code.

### 7.1 Sizing rules

- One task fits one fresh agent session and one PR of roughly 100–400 changed lines.
- One task touches one module (plus its tests and, if needed, a regenerated client). Cross-module tasks are split.
- Backend first, then UI: (1) model + service + tests, (2) API endpoint + OpenAPI + client regeneration, (3) Next.js screen using the generated client. Contract changes are their own task, approved by you.
- Anything with “and” in its title is probably two tasks.
- Spikes (research, prototypes) are separate tasks whose output is a note or ADR, not production code.

### 7.2 Task file template (`docs/tasks/CRM-123-short-name.md`)

```markdown
# CRM-123: Block outbound SMS to contacts with SMS DND
Module: sending   Phase: 2   Depends on: CRM-118 (send gateway skeleton)
Spec: docs/product/06-sms-voice.md#opt-out   ADRs: 0007-consent-model

## Goal
The send gateway must refuse SMS to contacts whose SMS consent is opted-out.

## Acceptance criteria
- Given a contact with SMS DND, when send() is called for SMS, then no adapter call happens,
  a Message row is stored with status=blocked, reason=dnd, and event message.blocked is published.
- Given the same contact, email sending is unaffected.
- Works identically for inbox, bulk and workflow sources.

## Constraints
- Do not change contracts/. Do not touch channels/ adapters.
- Reuse crm.consent.services.get_status().

## Tests required
Unit tests for each criterion; one cross-tenant test; no network.

## Out of scope
STOP keyword handling (CRM-124).
```

### 7.3 Where tasks live

Keep tasks as issues in your tracker (GitHub Issues or Linear) **and** as task files in the repo; the agent reads the file, you track status in the tracker. Labels: `phase-N`, `module:x`, `needs-plan`, `plan-approved`, `in-review`, `human-only` (tasks agents must not do: production config, secrets, contract approval, data migrations on live data).

## 8. The agent work loop

Every task runs the same loop. It follows Anthropic's own guidance for Claude Code — [explore, plan, implement, commit](https://jc-aiwiki.pages.dev/claude-ai/claude-code-best-practices) — built around one constraint: [the context window fills up fast, and performance degrades as it fills](https://code.claude.com/docs/en/best-practices%20).

1. **Start fresh.** New session (or `/clear`) per task. Give the agent only the task file path; it reads `AGENTS.md`, `docs/progress.md`, the module doc and ADRs itself.
2. **Explore (read-only).** In plan mode the agent reads the relevant code and docs and asks questions. Use a sub-agent for wide searches so the main context stays clean.
3. **Plan.** The agent writes a plan: files to change, models/migrations, endpoints, tests per acceptance criterion, risks, and what it will *not* do. You approve, edit or reject. [Correcting a plan is far cheaper than correcting a diff](https://dev.to/galian/claude-code-workflow-best-practices-that-ship-code-na).
4. **Tests first.** The agent (or the `test-writer` sub-agent) writes failing tests from the acceptance criteria, and you glance at them — tests are the real spec.
5. **Implement.** Code against the plan in small commits, running the relevant tests after each step.
6. **Verify.** `make verify` (tests, lint, types, migrations check, contracts check, security scan) must be green. The agent shows the command output, not a claim that it passed.
7. **Self-review.** The `reviewer` and, for sensitive modules, `security-reviewer` sub-agents review the diff in fresh contexts; the implementing agent fixes their findings.
8. **PR.** The agent opens a PR with the template: what changed, how it was tested, screenshots for UI, risks, follow-ups. It updates `docs/progress.md`.
9. **Human review and merge.** You review the plan-vs-diff match and the risky parts (section 11). Squash-merge when CI is green.
10. **Capture learning.** If the agent made a mistake that a rule would have prevented, add one line to the relevant `AGENTS.md` or a hook — this is how the system improves over time.

When something goes wrong mid-task (wrong direction, context full, repeated failures), **stop, don't push through**: have the agent write a short handoff note, start a fresh session from it, or rewrite the task.

## 9. Running several agents in parallel

Parallel agents multiply output only when their work does not overlap; start with two or three and grow as your review capacity allows — your review time, not agent count, is the real limit.

- **One worktree per agent.** [Git worktrees give each agent its own checkout, so several agents can edit in parallel without conflicts](https://medium.com/@rajasekar-venkatesan/making-the-most-of-claude-code-best-practices-hidden-features-b5ed99fbd61f): `git worktree add ../platform-crm-123 -b crm-123`.
- **Partition by module.** Assign agents to different modules (e.g. one on `contacts`, one on `channels/whatsapp_cloud`, one on the Next.js inbox). Never two agents in the same module at once.
- **Contract-first enables parallelism.** Once an endpoint is in `contracts/openapi`, the backend agent implements it while the frontend agent builds the screen against a mock server generated from the same spec.
- **Serialise shared files.** Migrations, settings, `urls.py`, `package.json` and the contracts are touched by one task at a time; give migrations a numbering owner (you or one agent).
- **Merge often.** Short-lived branches (≤ 1–2 days); each agent rebases on `main` before opening its PR.
- **Orchestrator pattern (later).** One coordinating session reads the task board, hands tasks to worker sessions in isolated worktrees, and collects their PRs. Adopt it only after the single-agent loop is reliable.
- **Competing implementations** for hard problems: two agents attempt the same task with different approaches; you keep the better one.

| Good to parallelise | Keep sequential |
| --- | --- |
| Independent modules, channel adapters, UI screens against a fixed contract, tests for existing code, docs | Data model foundations, tenancy/RLS, contracts, the send gateway, the workflow engine core, anything security-critical |

## 10. Guardrails

Guardrails turn “please don't” into “can't”. Set them up in week one, before the first feature task.

### 10.1 CI gates (required to merge)

| Gate | Catches |
| --- | --- |
| Unit + integration tests (pytest, Vitest) with coverage floor on changed lines (e.g. 80%) | Broken logic, missing tests |
| **Cross-tenant test suite** (every endpoint called as tenant A with tenant B's IDs → 404) | Data leaks between customers |
| Type checks (mypy/pyright, `tsc`) and linters (ruff, ESLint) | Sloppy or hallucinated code |
| `makemigrations --check` and migration linter (no destructive ops without a flag) | Missing or dangerous migrations |
| Contracts check (OpenAPI regenerated = committed; client regenerated) | Silent API drift between engine and web |
| SAST (Semgrep/Bandit), dependency audit (pip-audit, `pnpm audit`), secret scan (gitleaks) | Vulnerable code, bad packages, leaked keys |
| Playwright e2e smoke tests for key flows (login, create contact, send message in sandbox) | Integration breaks |
| CODEOWNERS: you must approve changes to `contracts/`, `migrations/`, `tenancy/`, `sending/`, `infra/`, `.claude/` | Agents changing critical areas unnoticed |

Branch protection: no direct pushes to `main`, CI green and one human approval required, linear history.

### 10.2 Environments

- **Local** (`docker compose up`): Postgres, Redis, Mailpit (catches all email), MinIO, and **provider simulators** for Twilio, WhatsApp/Meta and email webhooks, so agents can test full flows with no real accounts.
- **Staging**: sandbox credentials (Twilio test credentials, Meta test WhatsApp number, a sending domain for internal addresses only), seeded fake tenants.
- **Production**: no agent access. Deploys only through the pipeline after human approval.

### 10.3 Agent permissions and secrets

- Allowlist the commands agents may run (`make`, `pytest`, `pnpm`, `git` without `push --force`); deny `curl` to arbitrary hosts, `rm -rf`, database shells against non-local hosts.
- Deny read access to `.env*`, `infra/secrets`, cloud credentials; agents get only local `.env.example` values.
- Run fully autonomous (“skip permissions”) modes only inside throw-away containers or cloud sandboxes with no credentials and restricted network.
- Hooks: auto-format on edit; block `git commit` when `make verify` fails; block edits to denied paths.
- Agents read untrusted content (issues, web pages, dependency READMEs); treat instructions found there as data, and never give agents tokens that can push to `main` or reach production.

## 11. Reviewing agent output

Agent code usually compiles and passes its own tests; the risks are in what it quietly skipped or invented. Review with these failure modes in mind.

### 11.1 Common agent failure modes

| Failure | How to spot it |
| --- | --- |
| Tests that test nothing (over-mocked, asserting the mock was called) | Read the tests first; ask “would this fail if the feature broke?” |
| Weakened or deleted tests, `skip` markers, loosened assertions | Diff of `tests/`; CI rule that flags removed tests |
| Missing tenant scoping in a new query or Celery task | Cross-tenant suite; grep for raw `objects.all()` / `.raw(` |
| Hallucinated APIs or library functions | Type check fails; unfamiliar imports |
| Scope creep: refactors, renames, “improvements” outside the task | Files changed vs. plan |
| Duplicated logic instead of reusing a service | Search for similar function names |
| Silent error swallowing (`except Exception: pass`) | Linter rule; grep |
| Hard-coded values that belong in settings or the module doc | Magic numbers, URLs, IDs |
| Over-engineering (abstract base classes for one use) | Lines changed vs. task size |
| Claims without evidence (“all tests pass”) | Require pasted command output in the PR |

### 11.2 Your review checklist

- [ ] Diff matches the approved plan; nothing unexplained
- [ ] Each acceptance criterion has a test that would fail without the change
- [ ] Tenancy, permissions and consent enforced in the service layer
- [ ] No secrets, no real provider calls, no PII in logs
- [ ] Migrations reversible and safe on large tables (no full-table locks)
- [ ] Events published via the outbox; Celery tasks idempotent
- [ ] API matches the contract; generated client used in the web app
- [ ] Errors handled and logged with context; user-facing messages sensible
- [ ] `docs/progress.md` and module docs updated

Risk-based depth: skim low-risk UI or test-only PRs; read every line of changes to tenancy, sending, billing, automation engine, auth and migrations.

## 12. Prompt templates

Short prompts that point at files beat long prompts that paste content. Save these as slash commands in `.claude/commands/` so you type one line.

**A. Split the spec into module docs**

```text
Read docs/product/source/spec.md (exported spec). Split it into module docs under
docs/product/ using the template in docs/product/_template.md, one file per module listed
in docs/product/index.md. Keep only what our engine must do; mark every GHL behaviour as
"Decision needed" when the spec does not say what we choose. Do not invent requirements.
List all "Decision needed" items at the end of each file.
```

**B. Generate tasks for a module**

```text
Read docs/product/05-inbox.md and the related ADRs. Propose tasks in docs/tasks/ using
docs/tasks/_template.md. Each task: one PR, <=400 lines, one module, testable acceptance
criteria, explicit dependencies. Order them. Output the list for my review; do not write code.
```

**C. Plan a task** (plan mode)

```text
Task: docs/tasks/CRM-123-block-sms-dnd.md. Read AGENTS.md, docs/progress.md, the linked spec
section and ADRs, then the relevant code. Write a plan: files to change, tests per acceptance
criterion, migrations, risks, and what you will not change. Ask questions if anything is
ambiguous. Do not edit files.
```

**D. Implement an approved plan**

```text
Implement the approved plan for CRM-123. Write the failing tests first and show them failing,
then implement until they pass. Run make verify and paste the output. Do not change files
outside the plan; if you must, stop and tell me why. Then open a PR using the template
and update docs/progress.md.
```

**E. Review a PR** (fresh session or `reviewer` sub-agent)

```text
Review the diff of branch crm-123 against main using the checklist in
docs/architecture/review-checklist.md and the task file. Report: blocking issues, risks,
missing tests, scope creep. Be specific with file:line. Do not fix anything.
```

**F. Security review**

```text
Act as a security reviewer. For branch crm-123 check tenant isolation, authz on every
endpoint, input validation, secrets, SSRF, injection, unsafe deserialisation, PII in logs,
and webhook signature handling. Rate each finding high/medium/low with a fix.
```

**G. Fix a bug**

```text
Bug: <symptom, steps, expected vs actual, logs>. First write a test that reproduces it and
show it failing. Find the root cause and explain it before changing code. Fix with the
smallest change, keep the test, run make verify.
```

**H. Handoff when a session gets long**

```text
Write docs/tasks/CRM-123.handoff.md: goal, what is done, what is left, decisions made,
files touched, failing tests and hypotheses. Keep it under 40 lines.
```

## 13. Your role as the agent manager

You are the architect, product owner and final reviewer; agents are fast junior-to-mid engineers with no memory between sessions. Spend your time where agents are weakest: deciding, specifying and verifying.

### 13.1 What stays with you

- Architecture, contracts, data model and ADR approval.
- Turning spec ambiguity into decisions (“Decision needed” items).
- Plan approval for every task; line-by-line review of high-risk areas.
- Anything touching production, secrets, money, real customers or legal compliance.
- Prioritisation: what goes into the next phase.

### 13.2 Daily and weekly cadence

| When | What you do |
| --- | --- |
| Morning (30 min) | Read `docs/progress.md` and open PRs; approve or reject plans waiting for you; hand out the day's tasks |
| During the day | Answer agent questions; review PRs as they come (plan match → tests → risky files) |
| End of day (15 min) | Merge green PRs; note recurring agent mistakes and add rules/hooks |
| Weekly | Groom the next week's tasks; resolve “Decision needed” items; review metrics; update ADRs and the roadmap |
| Each phase end | Demo the vertical slice end-to-end; run a security review agent across the module; refactor debt tasks |

### 13.3 Metrics to watch

- **Plan rejection rate** — high means tasks or docs are unclear.
- **PR rework rate** (PRs needing more than one revision) and **escaped defects** (bugs found after merge).
- **Cycle time** per task (ready → merged) and **review queue length** — if PRs pile up, reduce agent parallelism.
- **Test coverage on changed lines** and **CI failure causes**.

### 13.4 When to stop an agent

- It asks to change contracts, migrations or files outside the plan without a strong reason.
- The same test fails after two or three attempts — rewrite the task or give it a hint.
- The context is long and answers are getting worse — handoff note and fresh session.
- It proposes adding a new dependency or service — that is an ADR decision, not a task detail.

## 14. First two weeks

The first two weeks build the rails, not features: by the end you should have one thin vertical slice (create a contact in Next.js → stored in the engine with RLS → event published) passing every gate.

**Week 1 — Foundations (mostly you, agents assisting)**

- [ ] Decide monorepo vs. separate repo; create the layout in section 4
- [ ] Write ADR-0001 (separate CRM engine), ADR-0002 (identity via core JWT), ADR-0003 (tenancy + RLS), ADR-0004 (event bus + outbox)
- [ ] Draft `contracts/`: OpenAPI skeleton (health, tenant, contacts CRUD) and the event envelope schema
- [ ] Write root and engine `AGENTS.md`, `CLAUDE.md`, `.claude/settings.json` (permissions, denied paths, hooks)
- [ ] Agent task: scaffold the engine (Django project, settings per env, Celery, pytest, ruff, mypy, Docker Compose with Postgres/Redis/Mailpit)
- [ ] Agent task: CI pipeline with all gates from section 10.1 (cross-tenant suite can start empty)
- [ ] Agent task (prompt A): split the spec into module docs; you resolve the “Decision needed” items for tenancy and contacts

**Week 2 — First vertical slice**

- [ ] Agent task: JWT validation against the core's JWKS + tenant context middleware + RLS policies
- [ ] Agent task: consume `org.created` / `user.*` events to provision tenants and mirror users
- [ ] Agent task: generic record engine minimal version + Contact model, service and tests
- [ ] Agent task: contacts API matching the contract; regenerate the TypeScript client
- [ ] Agent task: Next.js contacts list and create form under `/crm` using the generated client
- [ ] Agent task: outbox + `crm.contact.created` event; consumer test
- [ ] You: end-to-end demo, review of tenancy code line by line, retrospective → update `AGENTS.md` with lessons

After that, follow the phases in section 16 of the spec and the security checklist in its Security tab, one module at a time.

## 15. Sources

- [Anthropic — Best practices for Claude Code](https://code.claude.com/docs/en/best-practices%20)
- [Summary of Anthropic's Claude Code best practices (explore → plan → code → commit)](https://jc-aiwiki.pages.dev/claude-ai/claude-code-best-practices)
- [AGENTS.md — open format for guiding coding agents](https://agents.md/)
- [Claude Code best-practice tips: CLAUDE.md size, monorepos](https://mintlify.com/shanraisshan/claude-code-best-practice/tips/workflow-tips)
- [Making the most of Claude Code: hooks vs CLAUDE.md, worktrees](https://medium.com/@rajasekar-venkatesan/making-the-most-of-claude-code-best-practices-hidden-features-b5ed99fbd61f)
- [Claude Code workflow: plan mode, verification loop](https://dev.to/galian/claude-code-workflow-best-practices-that-ship-code-na)
- [Companion spec: GoHighLevel CRM & Omnichannel Messaging — Reverse-Engineering Specification](https://claude.ai/artifact/U1zQmR144pJAeiUjXtUsia)
