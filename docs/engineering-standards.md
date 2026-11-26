# Engineering Standards

## 1. Language and text rules (non-negotiable)

- **English only in source.** Code, identifiers, comments, docstrings, commit messages, PR text, log messages, exception messages, test names, docs under `docs/` and `tasks/`. The application UI is English only. Content the product generates for markets (ads, posts, articles) may be in other languages when a brief asks for it.
- **Enforcement**: a `tools/check_language` script runs in pre-commit and CI. It fails when any tracked file contains characters from the Arabic Unicode blocks (U+0600–U+06FF, U+0750–U+077F, U+FB50–U+FDFF, U+FE70–U+FEFF) or bidirectional control marks.
- **No user-visible string in code.** Every label, message, email, notification, validation error, enum display name and empty-state text comes from a catalog:
  - Backend: Django gettext (`locale/<lang>/LC_MESSAGES/django.po`). API errors return stable machine codes plus a translated message; clients may translate by code.
  - Frontend: `next-intl` ICU message files (`apps/web/messages/en.json`), namespaced per feature, typed keys, plural and select via ICU, no string concatenation, no inline fallback text.
  - Notification and email templates are catalog-based.
- **LLM prompts are files**, not strings in code: `prompts/<domain>/<name>/vN.md` with front matter (purpose, model tier, input schema, output schema, eval set). Prompts that produce user-facing text take the target locale as a parameter; the instruction text itself stays English.
- **Formatting**: numbers, currency and dates always go through `Intl` helpers (`apps/web/src/lib/format.ts`), money is never converted through floating point.

## 2. SOLID, made concrete

- **Single Responsibility**: one reason to change per module. Views only parse and delegate. Services implement one use case each. Selectors only read. Models hold invariants, not workflows.
- **Open/Closed**: new provider, new agent role, new audit rule, new action kind = add a class/registration, never edit a switch. Registries are populated by explicit registration at startup.
- **Liskov**: every adapter passes the shared contract test suite for its port; adapters may not narrow inputs or widen errors. Capability descriptors declare what an adapter cannot do instead of raising surprises.
- **Interface Segregation**: small ports (`AdsReader`, `AdsWriter`, `InsightsReader`) composed into larger ones; a read-only connection only receives reader ports.
- **Dependency Inversion**: domain depends on ports; adapters and infrastructure are injected through a lightweight container / factory configured in one composition root. No `import requests` or vendor SDK outside adapters.
- Additional rules: prefer composition over inheritance, no god services, functions under ~40 lines, cyclomatic complexity gate, no boolean-flag parameters that switch behaviour, explicit domain types (`Money`, `Micros`, `ProviderRef`, `Locale`) instead of primitives.

## 3. Python and Django practices

- Python 3.13 (verify against Django 6.1 support matrix at P0.3), managed with `uv`, lockfile committed, hash-pinned in CI.
- Typing: `mypy --strict` (or pyright strict) for `domain`, `services`, `adapters`; `django-stubs`; Pydantic v2 for all boundary data (API, tool inputs, provider payloads, LLM structured outputs).
- Lint and format: `ruff` (lint + format) with a strict rule set, `import-linter` contracts for layering, `bandit`-class security rules, `pip-audit`, `deptry`.
- Structure: services / selectors pattern; business logic never in views, serializers or signals; signals only for framework wiring; managers for query reuse.
- Database: constraints and indexes declared in models; no `null=True` on strings; `CheckConstraint` for enums and ranges; partial and covering indexes where measured; `select_related` / `prefetch_related` reviewed, N+1 detection (`django-zeal`) fails tests; long transactions forbidden around network calls; `transaction.on_commit` (or outbox) for task dispatch.
- Migrations: backwards-compatible steps (expand → migrate → contract), no table rewrites on hot tables, `CREATE INDEX CONCURRENTLY` via `AddIndexConcurrently`, migration linting (`django-migration-linter` or equivalent) in CI, every migration reversible unless explicitly justified.
- Money: integer micros plus currency, never floats. Time: timezone-aware UTC everywhere, injected clock for tests.
- Settings: 12-factor, validated at startup with Pydantic settings, no secrets in repo, separate settings for dev / test / prod through environment only.
- Logging: `structlog` JSON logs with correlation ids (request id, run id, action id), secret scrubbing processor, no PII by default.
- Errors: domain exception hierarchy mapped to API problem responses (RFC 9457 style); provider errors normalised to `RetryableProviderError`, `PermanentProviderError`, `RateLimited(reset_at)`, `AuthExpired`, `PolicyRejected`.

## 4. Celery practices

- Tasks are thin and idempotent: they take primitive ids, load state, call a service. Never pass model instances.
- `acks_late=True`, `reject_on_worker_lost=True`, broker visibility timeout above the longest task, soft and hard time limits on every task, retries with exponential backoff and jitter, max retries, dead-letter handling that creates an alert.
- Separate queues: `default`, `agents`, `ingest.<provider>`, `publish.<provider>`, `media`, `reports`, `low`. Per-queue rate limits mirror provider limits.
- Beat schedules stored in DB, changes audited. Long workflows are modelled as persisted state machines (not deep Celery chains) so they survive restarts.
- Dispatch after commit through the outbox to avoid phantom tasks.

## 5. API practices

- Django Ninja, versioned under `/api/v1`, OpenAPI published and diffed in CI (breaking change detection).
- Cursor pagination, filtering and sorting whitelist, consistent problem responses, ETags for cacheable reads, `Idempotency-Key` on all unsafe POSTs that create work, request size limits, per-user and per-token rate limits.
- Authorisation per object through a policy layer (not scattered `if user.is_staff`), tested with a permission matrix.
- Realtime via SSE with resumable event ids.

## 6. Frontend practices

- Next.js 16 App Router, RSC by default, client components only for interactivity, streaming and Suspense for slow data, error boundaries per route segment, `loading` and `not-found` handled.
- TypeScript `strict` plus `noUncheckedIndexedAccess`, ESLint with type-aware rules, no `any`, no non-null assertions without justification.
- **Tailwind CSS v4**, CSS-first config (`@theme` tokens): colours, spacing, radii, typography, motion are tokens; no arbitrary values (`[13px]`) except reviewed exceptions; variants through `class-variance-authority`; class merging via `tailwind-merge`; dark mode via tokens; container queries for components; no CSS-in-JS.
- Components: shadcn/ui primitives (Radix), one folder per component with tests and stories, accessibility WCAG 2.2 AA (keyboard, focus, contrast, aria, reduced motion), forms with React Hook Form + Zod schemas derived from OpenAPI where possible.
- Data: generated typed client, TanStack Query for client state, server actions only for BFF mutations, optimistic updates only where rollback is defined.
- Performance budgets (LCP, INP, CLS, bundle size) enforced in CI with Lighthouse CI and `next build` analysis.
- Testing: Vitest + Testing Library for components, Playwright for end-to-end (desktop and mobile viewports), axe checks in e2e.

## 7. Testing strategy

| Level | Tooling | What |
|---|---|---|
| Unit | pytest, hypothesis | Pure domain, policies, parsers, mappers, budget math (property tests for money and rate limiting) |
| Contract | pytest + respx/vcr + recorded fixtures | Every adapter against its port suite; provider payload schemas; nightly live smoke against sandbox accounts |
| Integration | pytest-django, testcontainers (Postgres 18 + pgvector, Redis) | Services with real DB, Celery eager and real worker tests, migrations up/down |
| Agent evals | custom harness | Golden tasks with rubrics, regression on prompt / model change, cost and latency tracked |
| End to end | Playwright | Command box → plan → approval → publish (against fakes), |
| Security | semgrep, bandit, gitleaks, pip-audit, npm audit, ZAP baseline | Static and dynamic |
| Load / chaos | k6, fault injection | Provider outage, rate limits, worker crash mid-execution |

Coverage gates: domain and services ≥ 90% line and branch, adapters ≥ 85%, frontend logic ≥ 80%; mutation testing (mutmut) on the actions and budget modules.

## 8. Git, CI/CD and release

- Trunk-based development, short-lived branches, Conventional Commits, PR template with risk and rollback sections, CODEOWNERS, required reviews, signed commits optional.
- Pre-commit: ruff, language guard, gitleaks, prettier / eslint, i18n key validation (missing or unused keys), migration check.
- CI stages: lint → type → unit → contract → integration → build images → security scans → e2e → OpenAPI diff → deploy to staging → smoke → manual gate → production.
- Images: multi-stage, non-root, pinned base digests, SBOM (CycloneDX), vulnerability scan, provenance attestation.
- Releases: versioned, feature flags for risky capabilities (autonomy levels, new providers), database migrations run separately and before code, rollback runbook per release.
- Dependency hygiene: Renovate weekly, pinned majors, API-version watch job (see `external-platform-facts.md`).

## 9. Observability and operations

- OpenTelemetry traces across web → API → Celery → provider calls, Prometheus metrics, Grafana dashboards, Sentry for errors, uptime and synthetic checks.
- LLM observability: every call recorded (prompt version, model, tokens, cost, latency, cache hit, outcome). Cost dashboards per agent, per feature, per workspace with budget alerts.
- SLOs: API availability, job freshness (metrics ingested within X hours), publish success rate, approval latency. Alerts are actionable and each has a runbook.
- Backups: PostgreSQL PITR, tested restores monthly, object storage versioning, secrets recovery procedure.

## 10. Security and compliance

- OWASP ASVS L2 as the baseline, threat model per phase (STRIDE), dependency and container scanning, secret scanning, least-privilege IAM, network segmentation, TLS everywhere, HSTS, security headers, CSP.
- AI-specific: prompt-injection tests in the eval suite, tainted-run policy, output filters for banned claims, PII redaction before LLM calls where not needed, no training on customer data assumptions documented, vendor data-retention settings recorded.
- Compliance: CASL (consent, identification, unsubscribe for commercial electronic messages), PIPEDA (purpose limitation, access and deletion requests, retention), platform policies (Meta, Google, LinkedIn, OpenAI ads policies and automation rules), scraping only within site terms and robots.txt.
- Audit: append-only, tamper-evident (hash chaining), retention configured, exportable.

## 11. Definition of Done (per task)

1. Acceptance criteria met and demonstrated.
2. Tests written at the correct levels, green in CI, coverage gates met.
3. Static gates green: ruff, types, import-linter, eslint, language guard, i18n key check, migration lint, security scans.
4. No hard-coded user text; catalogs updated; responsive layout verified for UI work.
5. Observability added: logs with correlation ids, metrics, traces, alerts where relevant.
6. Security review checklist done (authz, input validation, secrets, untrusted content handling).
7. Docs updated: ADR if a decision was made, runbook if operations change, API docs regenerated.
8. Feature flag and rollback plan defined for anything that spends money, publishes publicly or messages people.
9. Audit events emitted for every state change.
10. Reviewed and merged; staging verified.
