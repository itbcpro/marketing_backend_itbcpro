# Architecture

## 1. Principles

1. **Agents propose, the action layer disposes.** No agent talks to an external platform directly. Every state-changing call is a `ProposedAction` that passes policy, budget guardrails, approval, idempotent execution and audit.
2. **Ports and adapters.** The domain knows abstract capabilities (`AdsProvider`, `SocialProvider`, `CmsProvider`, `LlmGateway`, `MediaGenerator`, `SecretStore`, `Notifier`). Each vendor is one adapter. Adding a platform never edits existing code (Open/Closed).
3. **Memory is data, not model weights.** Company taste, rules and outcomes live in Postgres, are inspectable, editable and versioned, and are injected into every agent run by a retrieval step.
4. **Everything external is untrusted.** DMs, comments, competitor pages, scraped text and API payloads are data. They never become instructions (prompt-injection boundary, see section 9).
5. **Idempotent, resumable, observable.** Every job can be retried safely, every run is fully traced (inputs, tool calls, outputs, cost), every decision is explainable.
6. **Configuration over code.** API versions, model routing, budgets, thresholds, schedules, prompts and all user-facing text live in configuration, prompt files and locale catalogs.

## 2. System context

```
                    ┌──────────────────────────── Human manager ────────────────────────────┐
                    │  command box · approvals inbox · dashboards · memory editor · alerts  │
                    └───────────────▲──────────────────────────────────────┬────────────────┘
                                    │ Next.js 16 (BFF, RSC, next-intl)     │
                                    ▼                                      ▼
                        ┌───────────────────── Django 6.1 API (Ninja) ─────────────────────┐
                        │  identity · workspaces · connections · agents · memory · actions │
                        │  ads · social · content(seo) · intel · reporting · audit         │
                        └──────┬───────────────────────┬──────────────────────┬────────────┘
                               │ outbox                │ SQL                  │ pub/sub
                        ┌──────▼──────┐        ┌───────▼────────┐      ┌──────▼──────┐
                        │ Celery      │        │ PostgreSQL 18  │      │ Redis/Valkey│
                        │ workers+beat│        │ + pgvector     │      │ broker/cache│
                        └──────┬──────┘        └────────────────┘      └─────────────┘
                               │ adapters (ports)
   ┌──────────┬───────────┬────┴─────┬───────────┬────────────┬───────────┬─────────────────┐
   │ Meta     │ Google    │ OpenAI   │ Instagram/│ LinkedIn   │ WordPress │ Anthropic API,  │
   │ Ads API  │ Ads API   │ Ads API  │ Facebook  │ Community  │ REST/MCP  │ SEO data, video │
   │          │           │          │ Graph API │ Mgmt API   │           │ + image gen     │
   └──────────┴───────────┴──────────┴───────────┴────────────┴───────────┴─────────────────┘
```

## 3. Backend bounded contexts (Django apps)

Each app owns its models, services (write use cases), selectors (read queries), API routers, tasks and tests.
Cross-app calls go through published service functions or domain events, never through another app's models.

| App | Responsibility |
|---|---|
| `core` | Base models (UUIDv7 PK, timestamps), money type, clock, ids, error types, feature flags, settings loaders |
| `identity` | Users, roles, permissions, sessions, passkeys / 2FA, API tokens for machine access |
| `workspaces` | Workspace (brand) as tenancy boundary, membership, per-workspace settings and budgets |
| `connections` | Provider connections, credential vault, token refresh, health checks, scopes, rate-limit state |
| `llm` | LLM gateway port, Anthropic adapter, model routing, cost tracking, prompt registry, structured outputs |
| `agents` | Agent definitions, runs, steps, tool registry, orchestrator, schedules, evals |
| `memory` | Brand profile, memory items, feedback events, retrieval, reflection, consolidation |
| `actions` | ProposedAction, policies, autonomy levels, approvals, executor, rollback, kill switch, budget guardrails |
| `ads` | Provider-neutral ad domain plus `providers/meta`, `providers/google`, `providers/openai_ads` adapters |
| `social` | Provider-neutral social domain plus `providers/instagram`, `providers/facebook`, `providers/linkedin` |
| `content` | WordPress adapter, content inventory, versions, SEO/GEO audits, article pipeline, publishing |
| `intel` | Trends, competitors, market gaps, monitors, sources registry |
| `inbound` | Contact-us events, DMs and comments as inbound conversation events (not a CRM) |
| `reporting` | Metric store, dashboards data, reports, briefings, alerts, anomaly detection |
| `notifications` | Channels, templates (catalog based), digests, escalation policy |
| `audit` | Append-only audit log, outbox, event bus, retention |
| `media` | Asset library, storage abstraction (S3-compatible), image/video generation port and adapters |

Layering rule (enforced by import-linter): `api → services/selectors → domain/ports ← adapters`. Adapters depend on
ports, never the reverse. Models do not import services.

## 4. Provider ports (interfaces)

- `AdsProvider`: `list_accounts`, `sync_structure`, `fetch_insights`, `validate_plan` (dry run), `create_plan`, `activate`, `pause`, `update_budget`, `update_creative`, `archive`, `list_policy_issues`.
- `SocialProvider`: `publish`, `schedule`, `fetch_media`, `fetch_insights`, `list_comments`, `reply_comment`, `hide_comment`, `list_conversations`, `send_message`, `subscribe_webhooks`.
- `CmsProvider`: `list_content`, `get_content`, `create_draft`, `update_content`, `publish`, `schedule`, `upload_media`, `set_seo_meta`, `set_schema`, `revisions`, `restore_revision`.
- `SearchDataProvider`: keywords, SERP, backlinks, rankings, AI-answer visibility.
- `MediaGenerator`: `generate_image`, `generate_video`, `poll`, `fetch`, capability descriptor (aspect ratios, durations, cost).
- `LlmGateway`: `complete`, `stream`, `batch`, `embed`, `count_tokens`, capability descriptor, cost estimation.
- `SecretStore`: `put`, `get`, `rotate`, `delete` with envelope encryption.
- `Notifier`: `send(channel, template_key, context, locale)`.

Each port ships a **contract test suite** that every adapter must pass, plus a fake adapter for tests.

## 5. Core domain model

Conventions: UUIDv7 primary keys (native in PostgreSQL 18), `created_at` / `updated_at`, `workspace_id` on every tenant table,
money as integer micros plus ISO currency, timestamps in UTC, provider payloads kept in JSONB `raw` columns for forensics,
constraints in the database (CheckConstraint, unique, FK) not only in code.

**Identity and tenancy**: `User`, `Workspace`, `Membership(role)`, `Role`, `PermissionGrant`.

**Connections**: `Connection(provider, external_account_id, status, scopes, expires_at, health, api_version)`,
`Credential(encrypted blob, key_version)`, `RateLimitState(connection, bucket, remaining, reset_at)`.

**Ads (provider neutral)**: `AdAccount`, `Campaign(objective, status, budget, schedule, provider_ref)`, `AdGroup` (ad set),
`Ad`, `Creative(asset refs, copy variants, locale)`, `Audience`, `ConversionEvent`, `ExternalEntityMap` (internal id ↔ provider id),
`MetricSnapshot(entity_type, entity_id, date, metric_key, value, currency, breakdown JSONB)` partitioned by month,
`AdPolicyIssue`, `CampaignBrief` (structured output of the command box), `CampaignPlan` (validated intended structure).

**Social**: `SocialAccount`, `ContentCalendarItem`, `Post(type, caption, assets, schedule, status)`, `PostVariant(locale)`,
`Conversation`, `Message`, `Comment`, `EngagementMetric`.

**Content / WordPress**: `Site`, `ContentItem(wp_id, type, url, locale, status, content_hash)`, `ContentVersion(body, seo_meta, captured_at)`,
`ContentEmbedding`, `InternalLink`, `SeoAudit`, `SeoFinding(severity, rule_key, evidence)`, `GeoAudit`, `KeywordTarget`,
`TopicCluster`, `ArticlePlan`, `ArticleDraft`, `AiVisibilitySample(engine, prompt, brand_mentioned, cited_urls, sampled_at)`,
`CrawlQueueItem(priority, next_due_at)`.

**Intelligence**: `Competitor`, `CompetitorSnapshot`, `TrendSignal`, `MarketGap`, `Source`, `MonitorRule`.

**Inbound**: `InboundEvent(kind: form | email | call | chat | dm | comment, channel, attribution JSONB, status)`.

**Agents**: `AgentDefinition(role, prompt_key, toolset, model_tier, autonomy_policy, budgets)`, `AgentRun(status, goal, cost, tokens)`,
`AgentStep(kind: llm | tool | decision, input, output, latency, cost)`, `AgentSchedule`.

**Memory**: `BrandProfile(version)`, `MemoryItem(kind, scope, text, structured JSONB, embedding vector, confidence, status, source_ref,
valid_from, valid_to, supersedes_id)`, `FeedbackEvent(action_ref, verdict, reason, edit_diff)`, `OutcomeLink(action_ref, metric_window, result)`.

**Actions**: `ProposedAction(kind, payload, risk, before, after, status, requested_by_agent, expires_at, idempotency_key)`,
`ApprovalDecision`, `ExecutionAttempt`, `RollbackRecord`, `GuardrailPolicy`, `AutonomyGrant(agent, action_kind, level)`.

**Audit**: `AuditEvent` (append-only), `OutboxMessage`.

## 6. Agent runtime

- **Roles**: Orchestrator, Ads Strategist (per-platform skills), Creative Producer, Social Manager, Community Responder, Content Strategist, Article Writer, SEO/GEO Analyst, Market Analyst, Reporter. Agents are data (`AgentDefinition`), not classes.
- **Loop**: build context (goal + brand profile + retrieved memories + task data) → LLM step → tool calls (validated by Pydantic schemas) → observe → continue until done, budget hit, or approval required. Each step persisted.
- **Delegation**: Orchestrator decomposes a goal into sub-goals for role agents through a `delegate` tool; results return as structured summaries. Depth and fan-out are bounded.
- **Tools** carry metadata: `read | write`, risk class, required autonomy level, timeout, idempotency behaviour, untrusted-output flag. Write tools never execute; they create `ProposedAction` records.
- **Model routing** by task tier from configuration: strategy and planning use the strongest model, bulk classification and extraction use the small fast model, embeddings use a dedicated embedding model. Prompt caching is on for stable prefixes (brand profile, tool definitions).
- **Runs execute in Celery** with time limits, resumable checkpoints (step log is the checkpoint), cancellation, and per-run token and cost budgets.
- **Structured outputs** are mandatory for decisions consumed by code (plans, classifications, verdicts).

## 7. Memory and learning

Three layers, all persisted:
1. **Brand profile** (owner defined): voice, values, audience, do / don't lists, banned claims, competitors, pricing stance, legal constraints.
2. **Feedback memory** (learned): every approval, rejection and edit is a `FeedbackEvent`. A structured reason is requested on rejection (one-tap categories plus optional text). Edits are diffed and summarised into candidate rules.
3. **Outcome memory** (measured): actions are linked to metric windows (for example CPA of an ad 7 days after launch, engagement of a post after 48 hours). Outcomes are stored as evidence, distinct from taste.

Retrieval: hybrid (metadata filters by scope and channel, full-text, vector similarity with pgvector), re-ranked, capped by a token budget, and each retrieved item is cited in the agent's rationale.
Learning loop: a weekly reflection job proposes new or updated `MemoryItem`s with evidence; they enter as `proposed` and become active only after human approval. Items expire or get re-validated by age. An exploration budget (configurable, default 15%) forces a share of proposals to be labelled experiments so the system does not collapse into repeating past taste.
A transparency page lists everything the system believes about the company, with edit, retire and provenance controls.

## 8. Approval and action lifecycle

```
DRAFTED → VALIDATED → (POLICY_BLOCKED | AWAITING_APPROVAL | AUTO_APPROVED) → APPROVED → EXECUTING
        → (SUCCEEDED | FAILED_RETRYABLE → EXECUTING | FAILED_FINAL) → (VERIFIED | ROLLED_BACK)
        AWAITING_APPROVAL → REJECTED | EXPIRED
```

- **Validation** uses provider dry-run where available (for example Google Ads `validate_only`), otherwise schema and rule validation.
- **Risk classes**: `R0` read-only, `R1` reversible and free (draft, pause), `R2` reversible with cost or public visibility (publish post, activate low budget), `R3` financial or irreversible (raise budget beyond threshold, archive, delete, DM broadcast).
- **Autonomy levels** per agent per action kind: `L1` suggest only, `L2` prepare and wait for approval, `L3` execute `R0-R1` alone and `R2` within limits, report after, `L4` full within guardrails. Defaults: everything starts at `L2`.
- **Guardrails** are independent of the AI: daily and monthly spend caps, max single change percentage, per-platform kill switch, quiet hours, blocked keyword and claim lists, mandatory human approval for `R3`.
- **Execution** is idempotent (stored idempotency key, provider `Idempotency-Key` where supported), verified by re-reading the provider state, and recorded with before / after snapshots so a `RollbackRecord` can restore.
- **Global kill switch** stops all writes instantly (checked inside the executor, not only in UI).

## 9. Security architecture

- **Prompt-injection boundary**: untrusted text is wrapped and labelled in prompts, tools that ingest untrusted content are flagged, and a run that has read untrusted content cannot invoke `R2+` write tools without human approval ("tainted run" rule). Classifier pre-screen on inbound DMs and comments. Never place secrets or full credentials in any prompt.
- **Secrets**: envelope encryption (data key per credential, master key from the host secret manager or environment KMS), key versioning, rotation jobs, no secrets in logs (structured log scrubbing).
- **Least privilege**: request minimal OAuth scopes per feature; separate connections per capability where the provider allows; read-only connections for analytics.
- **Web security**: session cookies (`Secure`, `HttpOnly`, `SameSite`), CSRF, strict CSP (Django 6 built-in CSP support), rate limiting, audit of admin actions, 2FA required for owner accounts.
- **Data protection**: PII minimisation in memory and logs, retention schedules, export and delete tooling (PIPEDA), consent and opt-out handling for messaging (CASL).

## 10. Scheduling

- Celery Beat with database-backed schedules (`django-celery-beat`) editable from the UI. Queues per provider with per-queue concurrency and rate limits so one platform's throttling cannot starve others.
- Daily cycle example: metric ingestion (per provider, restatement window re-pull) → anomaly detection → per-channel agent review → proposals into approval inbox → digest notification → weekly reflection and report.
- Missed-run policy: on worker downtime, jobs coalesce (run once, not N times). Every schedule has an owner, a timeout and an alert when it has not run.

## 11. Metrics store

`MetricSnapshot` is narrow (entity, date, key, value) for provider-neutral querying, with provider-native columns in JSONB.
Partitioned by month, BRIN or btree indexes on `(entity, date)`, retention and roll-up policy, restatement handling
(late attribution updates overwrite the last N days per provider rules), currency stored explicitly. Materialised views for dashboards, refreshed by jobs.

## 12. Frontend architecture

Next.js 16 App Router, React Server Components by default, TypeScript strict, Tailwind CSS v4 with CSS-first design tokens,
shadcn/ui primitives, TanStack Query for client cache, `next-intl` with an `en` message catalog,
typed API client generated from the Django OpenAPI schema, SSE for live agent runs and approvals. Next.js acts as a BFF:
browser talks only to Next; Next talks to Django with a same-site session, so tokens never live in browser JavaScript.

Navigation (each channel owns a section): Command · Approvals · Agents · Memory · Ads (Meta, Google, OpenAI) · Website (content, SEO, GEO, inbound) ·
Social (Instagram, Facebook, LinkedIn) · Market · Reports · Connections · Settings · Audit.

## 13. Repository layout (monorepo)

```
apps/api            Django project (settings, apps as above)
apps/web            Next.js application
packages/api-client generated TypeScript client + types
prompts/            versioned prompt templates (files, not code)
locale/             backend gettext catalog (en)
apps/web/messages   frontend message catalog (en)
tasks/              phase task files (this plan)
docs/               architecture, standards, ADRs, runbooks
infra/              docker, compose, CI, deployment manifests
tools/              lint scripts (language guard), codegen scripts
```

## 14. Architecture decision records (to be written in P0.19)

| ADR | Decision | Status |
|---|---|---|
| 001 | Django 6.1 + Django Ninja (typed, OpenAPI) for API | Proposed |
| 002 | Celery 5.6 + Redis/Valkey for jobs and schedules; Django 6 `django.tasks` used only as a thin enqueue API if it adds value | Proposed |
| 003 | LLM gateway port; default adapter uses Anthropic Messages API with tool use inside workers; Claude Agent SDK evaluated in a spike (`P1.1.9`) for open-ended agents | Proposed |
| 004 | Next.js as BFF, session cookie auth, no tokens in browser | Proposed |
| 005 | PostgreSQL 18 with pgvector, UUIDv7 keys, native partitioning for metrics | Proposed |
| 006 | Envelope encryption for credentials with rotatable master key | Proposed |
| 007 | Prompts as versioned files with tests and evals, never inline strings | Proposed |
| 008 | i18n: gettext in Django, ICU messages in Next, English-only source code | Proposed |
| 009 | Workspace tenancy from day one (single workspace now, cheap to extend) | Proposed |
| 010 | Outbox pattern for events and Celery dispatch after commit | Proposed |
