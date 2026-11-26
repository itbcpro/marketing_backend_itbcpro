# P0 — Foundation

Goal: a production-grade skeleton (backend, frontend, data, jobs, auth, secrets, i18n, CI/CD) on which every later phase is only additive.
Nothing here contains business features. Everything here is reused by every phase.

Legend: `S` < 0.5d · `M` ≤ 2d · `L` ≤ 5d · `[spike]` investigation · `[decision]` owner input.

---

## P0.1 Repository and tooling (S)
Needs: none.
- P0.1.1 Create monorepo structure (`apps/api`, `apps/web`, `packages/api-client`, `prompts`, `locale`, `infra`, `tools`, `docs`, `tasks`).
  - [ ] Initialise git, default branch `main`, `.gitattributes` (LF, binary rules), `.editorconfig`.
  - [ ] `.gitignore` for Python, Node, Django, Next, IDE, env files, media, coverage.
  - [ ] Root `README` with quickstart pointing to `docs/`.
  - [ ] `CODEOWNERS`, PR template (risk, rollback, test evidence, screenshots for UI), issue templates.
  - [ ] Conventional Commits config (commitlint) and commit-msg hook.
- P0.1.2 Toolchain pinning.
  - [ ] Python via `uv` with `.python-version`; Node via `.nvmrc` / `volta`; pnpm workspace; Turborepo for task graph.
  - [ ] `Makefile` (or `just`) with uniform commands: `setup`, `dev`, `test`, `lint`, `typecheck`, `format`, `migrate`, `gen-api`, `check-i18n`.
- P0.1.3 Pre-commit framework.
  - [ ] Hooks: ruff, ruff-format, language guard (P0.8), gitleaks, eslint, prettier, i18n key check, migration check, large-file guard, yaml/toml/json lint.
- Done when: fresh clone → `make setup && make dev` boots empty stacks; pre-commit blocks a non-English (Arabic script) comment, a secret and a bad commit message.

## P0.2 Local development environment (M)
Needs: P0.1.
- P0.2.1 Docker Compose stack.
  - [ ] Services: `postgres` (18 + pgvector image), `redis`/`valkey`, `api`, `worker`, `beat`, `web`, `mailpit`, `minio` (S3-compatible), optional `otel-collector` + `grafana` + `loki` + `tempo`.
  - [ ] Healthchecks, named volumes, `.env.example` with every variable documented, no real secrets.
  - [ ] Hot reload for api and web; debugger ports; seed command.
- P0.2.2 Dev seed data.
  - [ ] Fake workspace, users per role, fake provider adapters (used in tests and demos), sample content items, sample metrics.
- P0.2.3 Local tunnels for webhooks.
  - [ ] Documented procedure (cloudflared/ngrok) and a dev HTTPS setup because Meta and LinkedIn require HTTPS callbacks.
- Done when: a new developer gets a working stack with seeded data in under 15 minutes from the README.

## P0.3 Django project skeleton (M)
Needs: P0.1, P0.2.
- P0.3.1 [spike] Confirm supported matrix: Django 6.1.x, Python 3.13 or 3.14, Celery 5.6 compatibility, psycopg 3, Ninja, django-celery-beat, pgvector adapter. Output: pinned versions in ADR.
- P0.3.2 Project layout.
  - [ ] `config/` package with `settings/base|dev|test|prod`, `urls`, `asgi`, `wsgi`, `celery`.
  - [ ] Create all apps listed in `architecture.md` with empty `services`, `selectors`, `api`, `tasks`, `tests` packages and an `apps.py`.
  - [ ] ASGI as default entrypoint (SSE needs it); Granian or Uvicorn+Gunicorn decision recorded.
- P0.3.3 Base abstractions in `core`.
  - [ ] `BaseModel` (UUIDv7 default via database function, `created_at`, `updated_at`), `TenantModel` (`workspace` FK, manager that scopes queries).
  - [ ] `Money`/`Micros` value objects, `Locale` type, `Clock` protocol (system + frozen), `IdGenerator`.
  - [ ] Domain exception hierarchy and provider error taxonomy.
  - [ ] Service and selector base conventions (`Result` type, no exceptions for expected outcomes where sensible).
- P0.3.4 Static analysis config.
  - [ ] `ruff` strict rule set, `mypy --strict` with `django-stubs`, `import-linter` contracts for layering, `deptry`, coverage config with gates.
- Done when: `make lint typecheck test` green on the empty skeleton; import-linter fails when an adapter imports a service.

## P0.4 Configuration management (S)
Needs: P0.3.
- P0.4.1 Pydantic settings schema validated at startup; fail fast with a readable list of missing variables.
- P0.4.2 Feature flags (DB-backed, per workspace, cached) for autonomy levels, providers, experiments.
- P0.4.3 `PROVIDER_API_VERSIONS` and `MODEL_ROUTING` configuration structures (see architecture sections 6 and facts file).
- P0.4.4 Environment matrix documented: dev, test, staging, prod; settings per environment; secrets only via environment or secret manager.
- Done when: invalid config prevents boot; tests cover each settings module.

## P0.5 Database foundation (M)
Needs: P0.2, P0.3.
- P0.5.1 [spike] PostgreSQL 18 + pgvector availability on the chosen host / managed service; fallback plan.
- P0.5.2 Extensions and functions.
  - [ ] Enable `vector`, `pg_trgm`, `btree_gin`, `citext`, `pgcrypto` (if used), `unaccent`; migration to install idempotently.
  - [ ] UUIDv7 default function usage validated; fallback function if the host is on older Postgres.
- P0.5.3 Conventions and guards.
  - [ ] Naming conventions for constraints and indexes (Django `Meta` naming), `db_comment` on important columns.
  - [ ] Database roles: migration role, app role (no DDL), read-only reporting role; least privilege grants.
  - [ ] Statement and lock timeouts, idle-in-transaction timeout, connection pooling (pgbouncer or Django pool) decision.
- P0.5.4 Partitioning helper for time-series tables (monthly partitions, auto-create job, retention job) used later by `MetricSnapshot`.
- P0.5.5 Full-text search configuration for English (dictionary choice, tested with sample text).
- P0.5.6 Backup and restore.
  - [ ] Base backup + WAL archiving (PITR) configuration; restore runbook; first restore drill scheduled.
- Done when: migrations apply from empty on CI Postgres 18; partition helper tested; restore drill executed once.

## P0.6 Identity, workspaces, RBAC (L)
Needs: P0.3, P0.5.
- P0.6.1 Custom user model (email login, locale preference, timezone) created before the first migration.
- P0.6.2 Authentication.
  - [ ] Session auth for web, password hashing with Argon2, breach-password check, lockout and throttling.
  - [ ] Passkeys (WebAuthn) and TOTP 2FA; enforced for owner role; recovery codes; device/session list with revoke.
  - [ ] Machine tokens (hashed, scoped, expiring) for future integrations.
- P0.6.3 Workspace tenancy.
  - [ ] `Workspace`, `Membership`, request-scoped current workspace, default manager scoping, tests that prove no cross-workspace leakage.
- P0.6.4 Roles and permissions.
  - [ ] Roles: owner, admin, approver, editor, analyst, viewer; permission catalogue by capability (approve R2, approve R3, manage connections, edit memory, change autonomy, kill switch).
  - [ ] Policy layer (`can(user, action, object)`) with a permission matrix test generated from the catalogue.
- P0.6.5 Invitations, email verification, password reset with catalog-based email templates.
- P0.6.6 Audit hooks for login, permission changes, role changes.
- Done when: permission matrix test passes; cross-workspace access attempt fails in tests; 2FA enforced for owner.

## P0.7 Credential vault and connections base (L)
Needs: P0.5, P0.6.
- P0.7.1 Envelope encryption.
  - [ ] Data-key-per-credential, master key from environment/KMS, `key_version`, rotation command and re-encrypt job.
  - [ ] Credentials never returned by API, never logged, redacted in exceptions and Sentry.
- P0.7.2 `Connection` model and lifecycle.
  - [ ] States: `pending`, `active`, `degraded`, `expired`, `revoked`, `error`; scopes granted vs requested; last health check; api version.
  - [ ] Generic OAuth 2.0 client (authorization code + PKCE, state validation, refresh, revoke), provider config objects.
  - [ ] Token refresh scheduler with jitter and single-flight locking (no double refresh).
  - [ ] Health check task per connection kind; degrade and alert on failure.
- P0.7.3 Rate-limit registry: per connection and per bucket state, honoured by all adapters (token bucket with provider header feedback).
- P0.7.4 Connection settings UI contract (fields, validation, test-connection button) defined as schema so each provider plugs in its own form.
- Done when: a fake provider connects via the generic OAuth client, refreshes tokens, and rotation re-encrypts all credentials without downtime.

## P0.8 Internationalisation and language guard (M)
Needs: P0.1, P0.3.
- P0.8.1 Backend gettext setup: `locale/en`, extraction and compile commands, CI check for missing / fuzzy / unused entries.
- P0.8.2 API error code catalogue: stable codes, translated messages, documented in OpenAPI.
- P0.8.3 Notification and email templates rendered from catalog entries (single English locale, structure ready for more).
- P0.8.4 Language guard tool (`tools/check_language`).
  - [ ] Script scanning tracked files for forbidden Unicode ranges outside allowed directories; unit tests with fixtures; runs in pre-commit and CI.
  - [ ] Second check: forbid hard-coded user-facing literals in frontend components (lint rule for JSX text nodes and common attributes such as `title`, `aria-label`, `placeholder`).
  - [ ] Backend check: lint rule flags string literals passed to API error responses and notification senders without gettext.
- P0.8.5 Locale-aware formatting utilities (dates, numbers, currency, relative time, time zones) with tests.
- P0.8.6 Catalog workflow: how new keys are added, naming conventions per feature namespace, glossary of marketing terms stored as a catalog.
- Done when: adding an Arabic-script comment or a hard-coded JSX literal fails CI.

## P0.9 API layer (M)
Needs: P0.3, P0.6, P0.8.
- P0.9.1 Django Ninja project with `/api/v1`, auth dependency, workspace dependency, versioned routers per app.
- P0.9.2 Cross-cutting: problem-details errors, cursor pagination helper, filtering helper with whitelist, ETag support, request id middleware, body size limits.
- P0.9.3 Idempotency-Key support (store key + request hash + response) for unsafe POSTs that create work.
- P0.9.4 Rate limiting per user / token / IP with Redis, standard headers.
- P0.9.5 OpenAPI generation, published at `/api/schema`, CI diff for breaking changes, TypeScript client generation to `packages/api-client`.
- P0.9.6 SSE endpoint infrastructure: authenticated event stream, resumable via `Last-Event-ID`, backed by Redis pub/sub, heartbeat, per-workspace channels.
- Done when: sample endpoint round-trips through the generated TS client; breaking OpenAPI change fails CI.

## P0.10 Jobs and scheduling (M)
Needs: P0.3, P0.5.
- P0.10.1 Celery app with queues (`default`, `agents`, `ingest.*`, `publish.*`, `media`, `reports`, `low`), routing rules, per-queue concurrency.
- P0.10.2 Reliability defaults: `acks_late`, `reject_on_worker_lost`, time limits, retry policy helper (exponential backoff with jitter), dead-letter table and alert.
- P0.10.3 Outbox pattern: `OutboxMessage` written in the same transaction, dispatcher task publishes to Celery / event bus, exactly-once-ish via idempotent consumers.
- P0.10.4 `django-celery-beat` with schedules editable from the API, audit of schedule changes, timezone handling.
- P0.10.5 Task base class: correlation id propagation, structured logging, metrics, cancellation, run heartbeat.
- P0.10.6 Distributed locks (Redis or Postgres advisory) for single-flight jobs.
- P0.10.7 Job monitoring: Flower or custom UI decision, queue depth metrics, alert on stale schedules.
- P0.10.8 Worker deployment profiles (agent workers with higher memory, ingest workers with high concurrency).
- P0.10.9 Weekly "API drift watch" task: reads `PROVIDER_API_VERSIONS`, checks vendor changelog / sunset pages, alerts within 90 days of sunset.
- Done when: killing a worker mid-task results in exactly one completed effect; a stale schedule raises an alert.

## P0.11 Observability (M)
Needs: P0.3, P0.10.
- P0.11.1 Structured logging with `structlog`, correlation ids, secret scrubber, log levels policy.
- P0.11.2 OpenTelemetry traces (web → api → celery → outbound HTTP), sampling config, exporters.
- P0.11.3 Metrics: HTTP, DB, Celery, provider calls (latency, error class, rate-limit remaining), LLM (tokens, cost, cache hits).
- P0.11.4 Sentry integration with PII scrubbing and release tracking.
- P0.11.5 Dashboards and alerts (API errors, queue lag, job freshness, DB, provider failures) with runbook links.
- P0.11.6 Health endpoints (`/healthz` liveness, `/readyz` readiness with dependency checks).
- Done when: one request produces one connected trace across web, API and worker; alert fires in a fault-injection test.

## P0.12 Audit log and events (M)
Needs: P0.5, P0.6, P0.10.3.
- P0.12.1 Append-only `AuditEvent` (actor type human/agent/system, action, target, before/after, request id, ip, workspace) with hash chaining and DB-level protection (no UPDATE/DELETE grants).
- P0.12.2 Event bus abstraction (in-process + outbox), domain event catalogue.
- P0.12.3 Audit viewer API with filters, export, and retention policy job.
- Done when: attempting to modify an audit row fails at the database level; chain verification job passes.

## P0.13 Frontend skeleton (L)
Needs: P0.9, P0.8.
- P0.13.1 Next.js 16 project (App Router, TypeScript strict, pnpm), path aliases, env validation (Zod), Node runtime config.
- P0.13.2 Tailwind CSS v4 setup with CSS-first tokens; shadcn/ui installed and themed; logical-property lint rule; `tailwind-merge` and `cva` utilities.
- P0.13.3 `next-intl`: routing with a single `en` locale, message file, typed keys, `lang` on `<html>`, formatting helpers, missing-key CI check.
- P0.13.4 BFF and auth: session cookie handling, CSRF, server-side fetch wrapper to Django with workspace header, redirect to login, logout, session expiry UX.
- P0.13.5 App shell.
  - [ ] Layout with sidebar navigation (all sections from architecture §12), command palette shell, notification bell, workspace switcher, user menu, breadcrumbs.
  - [ ] Responsive behaviour (desktop first, usable on tablet/phone for approvals).
  - [ ] Dark and light themes from tokens; reduced-motion support.
- P0.13.6 Shared UI patterns: data table (server-side sorting/filtering/pagination), empty / loading / error states, confirm dialogs with typed confirmation for risky actions, toast system, diff viewer, JSON viewer, status badges, cost display (micros → currency), skeletons.
- P0.13.7 Realtime client: SSE hook with reconnect and backoff, event typing.
- P0.13.8 Error handling: route error boundaries, global error page, Sentry browser integration with scrubbing.
- P0.13.9 Frontend tests: Vitest setup, Testing Library, Playwright baseline (login flow, desktop and mobile screenshots), axe accessibility checks.
- Done when: logged-in user sees the app shell with no hard-coded strings; a11y checks pass.

## P0.14 Design system (M)
Needs: P0.13.2.
- P0.14.1 Token definitions (colour scales, semantic colours for status and risk R0–R3, spacing, radius, elevation, typography with a Latin font stack, motion).
- P0.14.2 Component inventory documented (Storybook or equivalent) with dark variants.
- P0.14.3 Data-visualisation palette (colour-blind safe), chart wrapper components (time series, bar, funnel, scorecard) with tooltips and accessible summaries.
- P0.14.4 Iconography and illustration rules; density settings.
- Done when: every shared component has stories in light/dark; visual regression baseline stored.

## P0.15 CI/CD (M)
Needs: P0.1–P0.3, P0.13.
- P0.15.1 Pipeline stages per `engineering-standards.md` §8 with caching and matrix (Python, Node).
- P0.15.2 Service containers (Postgres 18 + pgvector, Redis) for integration tests; parallel test sharding.
- P0.15.3 Docker build (multi-stage, non-root, pinned digests), SBOM, vulnerability scan, image signing / provenance.
- P0.15.4 Preview or staging deploy with automatic migration step and smoke tests; manual production gate.
- P0.15.5 Renovate configuration, security update fast path (includes the Next.js security release cadence).
- P0.15.6 Required status checks and branch protection documented and applied.
- Done when: a PR runs all gates; merge to main deploys to staging automatically.

## P0.16 Infrastructure and deployment (L) [decision: hosting]
Needs: P0.15, owner decision.
- P0.16.1 Environments: staging and production topology diagram; network, DNS, TLS (ACME), reverse proxy (Caddy or Traefik or managed).
- P0.16.2 Runtime: container orchestration choice (Compose on VPS with systemd, or managed containers) with rationale; horizontal scaling story for workers.
- P0.16.3 Managed services vs self-hosted: Postgres, Redis, object storage; backups and retention; cost estimate.
- P0.16.4 Secrets delivery to containers; master key storage; rotation runbook.
- P0.16.5 Static and media serving (CDN optional), upload limits, malware scanning for uploaded assets.
- P0.16.6 Zero-downtime deploy (rolling or blue/green), migration ordering (expand before code, contract after), rollback procedure tested.
- P0.16.7 Public webhook ingress (Meta, LinkedIn, WordPress plugin) with signature verification, replay protection, IP allowlists where published.
- Done when: staging is reproducible from code; a deliberate bad release is rolled back in under 10 minutes.

## P0.17 Security baseline (M)
Needs: P0.6, P0.7, P0.16.
- P0.17.1 Threat model for the platform (assets: ad accounts with spend, social accounts, tokens, customer messages) using STRIDE; abuse cases including prompt injection via DMs and competitor pages.
- P0.17.2 Web hardening: security headers, CSP (Django built-in), CORS closed, cookie flags, clickjacking, SSRF guard for any server-side fetch (allowlist, private-range block, DNS rebinding protection), upload validation.
- P0.17.3 Secrets hygiene: gitleaks in CI, pre-commit, history scan, rotation calendar.
- P0.17.4 Dependency policy: pip-audit / npm audit gates, licence allowlist, pinned versions, lockfile review.
- P0.17.5 Logging policy: what may never be logged (tokens, message bodies by default, PII).
- P0.17.6 Admin surface: Django admin locked behind 2FA and IP allowlist or disabled in production.
- Done when: threat model reviewed and mitigations tracked as tasks; ZAP baseline scan clean.

## P0.18 Test infrastructure (M)
Needs: P0.3, P0.13.
- P0.18.1 pytest configuration, factories (factory-boy), fixtures for workspace/user/connection, time freezing, deterministic ids.
- P0.18.2 HTTP recording and replay for adapters (vcr or respx fixtures) with secret scrubbing; fixture refresh command.
- P0.18.3 Fake adapters for every port living in `testing/` packages; contract-test base classes.
- P0.18.4 Testcontainers-based integration fixtures; database per worker for parallel runs.
- P0.18.5 Agent eval harness skeleton (datasets, rubrics, scorers, reports) — filled in P1.11.
- P0.18.6 Mutation testing job for `actions` and budget modules (scheduled, not per PR).
- Done when: contract-test base class exists and a fake adapter passes it.

## P0.19 Documentation and ADRs (S)
Needs: P0.3.
- P0.19.1 ADR template and the ten ADRs from `architecture.md` §14 written with the outcomes of spikes.
- P0.19.2 Runbook template (symptom, impact, diagnosis, mitigation, rollback, owner).
- P0.19.3 Glossary (campaign, ad set, ad group, creative, action, autonomy level, guardrail, memory item, outcome).
- P0.19.4 Contribution guide including the language rules and i18n workflow.
- Done when: docs build/lint in CI and links are checked.

## P0.20 Platform access applications (M, calendar-bound; start day 1) [decision: accounts]
Needs: owner accounts. Runs in parallel with everything.
- P0.20.1 Meta.
  - [ ] Confirm Business Manager ownership and admin access; complete business verification.
  - [ ] Create Meta app (business type), add Marketing API, Instagram, Pages, Webhooks products; privacy policy URL, data deletion callback, terms, app icon, category.
  - [ ] System user for server-to-server ads access; assign assets (ad accounts, pages, IG accounts, pixels/datasets).
  - [ ] Request permissions with use-case descriptions and screencast plan (ads_management, ads_read, business_management, pages_manage_posts, pages_read_engagement, pages_manage_metadata, pages_messaging, instagram_basic and business variants, instagram_content_publish, instagram_manage_comments, instagram_manage_messages, instagram_manage_insights). Confirm exact names in P3.1 spike.
  - [ ] Track the Marketing API Access Tier requirement (Limited → Full access thresholds) and plan the test-call volume needed.
  - [ ] Prepare test accounts / test ad account for reviewers; keep app in development mode until ready.
- P0.20.2 Google.
  - [ ] Create Google Cloud project (OAuth consent screen, APIs: Ads, Search Console, Analytics Data, PageSpeed, Indexing if needed, Natural Language optional).
  - [ ] Apply for Google Ads developer token on the manager (MCC) account; document the access level and quotas.
  - [ ] Set up test account(s) for safe experimentation.
  - [ ] Decide GA4 property access and Search Console verification for the site.
- P0.20.3 LinkedIn.
  - [ ] Create developer app linked to the company page; request Community Management API access (use case, screencast, test credentials); confirm admin roles on the page.
- P0.20.4 OpenAI Ads.
  - [ ] Open Ads Manager account, complete verification and billing setup by the owner (never by the agent); generate the advertiser API key; ask about partner / agency access and sandbox.
- P0.20.5 Anthropic.
  - [ ] Production API keys with spend limits, separate keys per environment, data-retention configuration recorded.
- P0.20.6 Media generation vendors.
  - [ ] Shortlist and open accounts for image and video generation (selected in P3.8 / P6.7 spikes); set spend caps.
- P0.20.7 SEO data vendors.
  - [ ] Confirm API access and credit limits for Ahrefs and SE Ranking (both connectors already exist in this workspace); decide primary vs secondary.
- P0.20.8 Tracking sheet in `docs/` (application, date submitted, status, blocker, next action) reviewed weekly.
- Done when: every application is submitted and tracked; the earliest expected approval dates are known and reflected in the plan.
