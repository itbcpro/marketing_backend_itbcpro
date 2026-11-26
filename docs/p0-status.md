# P0 Status (2026-09-29)

Legend: **Done** verified by tests or by running it · **Partial** core exists, listed gaps remain · **Not started**.

## Verified results

| Gate | Result |
|---|---|
| Backend tests | 143 passed, 93.7% line+branch coverage (gate 85%) |
| Ruff lint + format, mypy `--strict` (114 files) | clean |
| import-linter contracts | 2 kept, 0 broken |
| Migrations, i18n catalog, OpenAPI drift, language guard | up to date / consistent / no Arabic script in repo |
| Frontend | lint, prettier, `tsc`, 13 Vitest tests, `next build` all green |
| Real run | Login through the browser (Next → Django → Postgres 18 + Redis) worked end to end |
| Docker | API and web images build; API container ran in production mode (`/readyz` ok, non-root); web container served pages with security headers |

## Task status

| Task | Status | Notes / remaining work |
|---|---|---|
| P0.1 Repo and tooling | Partial | Repo, ignore files, Makefile, pre-commit config done. Missing: CODEOWNERS, PR/issue templates, commitlint install; pre-commit hooks are configured but not installed on this machine. |
| P0.2 Dev environment | Partial | Compose has Postgres 18+pgvector, Redis, Mailpit, MinIO; seed command done. Missing: app services in compose, observability stack, webhook tunnel doc. |
| P0.3 Django skeleton | Done | All apps, base models (UUIDv7, tenant), Money, Clock, errors, layering contracts. |
| P0.4 Configuration | Done | Validated env, feature flag model, provider version and model routing config. Flag selector/cache not yet written. |
| P0.5 Database | Partial | Extensions (vector, pg_trgm, btree_gin, citext, unaccent) verified on PG18. Missing: DB roles, partition helper, FTS config, backup/PITR drill. |
| P0.6 Identity | Partial | User model, session auth, Argon2, workspaces, roles, permission matrix with tests, audit of auth events. Missing: passkeys/TOTP, breach-password check, lockout beyond login throttle, invitations, password reset, machine tokens, session list. |
| P0.7 Vault and connections | Partial | Envelope encryption, rotation, connection and credential models, tests. Missing: OAuth client, token refresh scheduler, health-check tasks, rate-limit registry, settings UI contract. |
| P0.8 i18n and language guard | Done (English only, ADR 0002) | Gettext catalog, error-code catalog, guard tool, catalog checker, ESLint rules for hard-coded JSX text. Backend lint for untranslated literals not written. |
| P0.9 API layer | Partial | Ninja, problem errors, cursor pagination, idempotency, login throttle, OpenAPI export + generated TS types, drift check. Missing: filtering helper, ETag, body-size limits, general rate limiting, SSE endpoint. |
| P0.10 Jobs | Partial | Celery app, 13 queues, reliability defaults, outbox with backoff, DB scheduler, two scheduled tasks. Missing: retry helper and dead-letter alert, task base class, locks, monitoring UI, worker profiles, API drift watch job. |
| P0.11 Observability | Partial | structlog with secret scrubbing, request ids, liveness/readiness. Missing: OpenTelemetry, metrics, Sentry init, dashboards and alerts. |
| P0.12 Audit | Partial | Hash-chained log, trigger-enforced immutability, hourly verification. Missing: event bus abstraction, viewer API, retention job. |
| P0.13 Frontend skeleton | Partial | Next 16, Tailwind v4, next-intl, BFF auth, app shell, login, tests, build. Missing: shadcn/ui generator (primitives hand-written), data table, diff viewer, toasts, SSE hook, Playwright, browser Sentry. |
| P0.14 Design system | Partial | Semantic tokens (light/dark, risk colours). Missing: Storybook, chart wrappers. |
| P0.15 CI/CD | Partial | Workflow written but never executed (no remote yet). Dockerfiles verified. Missing: SBOM, signing, staging deploy, Renovate. |
| P0.16 Infrastructure | Not started | Needs the hosting decision. |
| P0.17 Security baseline | Partial | Security headers, CSP, session/CSRF hardening, secret scrubbing. Missing: threat model, SSRF guard, ZAP scan, admin lockdown docs. |
| P0.18 Test infrastructure | Partial | pytest fixtures and property tests. Missing: contract-test base classes (no ports yet), VCR fixtures, testcontainers, mutation job. |
| P0.19 Docs and ADRs | Partial | 5 of 10 ADRs, root README. Missing: runbook template, glossary, remaining ADRs. |
| P0.20 Platform access | Not started | Requires the owner: Meta, Google, LinkedIn, OpenAI Ads accounts and approvals. This is the critical path. |

## Defects found by actually running things

- Celery module selected development settings before `asgi.py` could pick production (ADR 0005, regression test added).
- Django 6.0 removed `CIEmailField`; replaced with lower-cased email plus a `lower(email)` unique constraint.
- Idempotency middleware left keys stuck "in progress" after a crash; now released (test added).
- `--frozen-lockfile` caught a stale lockfile; `.prettierignore` was missing and would have failed CI.
- The language guard crashed printing Arabic characters on a Windows console; it now prints code points only.

## Suggested next steps

1. Owner starts P0.20 (Meta, Google, LinkedIn, OpenAI Ads applications) immediately.
2. Finish the P0 gaps that later phases depend on: outbox-driven jobs are ready; add retry/dead-letter helper, connection health and rate-limit registry (P0.7, P0.10), SSE (P0.9), OpenTelemetry/Sentry (P0.11), threat model (P0.17).
3. Push to a remote so CI runs for the first time.
4. Begin P1.1 (LLM gateway) once the P0 gaps above are closed.
