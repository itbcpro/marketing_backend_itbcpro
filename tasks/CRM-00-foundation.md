# CRM-0 Foundation: rails before features

Source of truth: `docs/product/source/ghl-spec.md`, `docs/product/source/agent-playbook.md`, ADR 0007 to 0010.
Sizes: S under half a day, M up to 2 days, L up to 5 days. `Needs:` hard dependencies. Tasks contain no code.
Rules for every task: no hard-coded value (settings, enums or catalogs), English only, strings in catalogs,
reversible migrations, tests including a cross-tenant test, and `make lint typecheck layers test` green.

## CRM-0.1 Repository hygiene (S)
- CRM-0.1.1 Initial commit with clean ignore rules. [x] done
- [x] CRM-0.1.2 Root `AGENTS.md` and `CLAUDE.md` (rules, commands, definition of done).
- [x] CRM-0.1.3 `CODEOWNERS` for `contracts/`, `**/migrations/`, `crm/tenancy/`, `crm/sending/`, `infra/`.
- [x] CRM-0.1.4 PR template and task-file template under `docs/tasks/_template.md`.
- Done when: a new contributor can set up and verify from the root files alone.

## CRM-0.2 Product docs split (M)
- [x] CRM-0.2.1 One module doc per area in `docs/product/` (tenancy, records, contacts, pipelines, inbox, sms, email, whatsapp, meta, automation, reporting, security).
- [x] CRM-0.2.2 Glossary (`tenant` = workspace; one word per concept).
- [x] CRM-0.2.3 Mark every GHL behaviour "we do X / we do Y / not in MVP"; list "Decision needed" items per file.
- [x] CRM-0.2.4 Acceptance criteria in Given/When/Then per module.
- Done when: each module doc is under 300 lines and its criteria map to tests.

## CRM-0.3 JWT authentication (L) - ADR 0008
Needs: none.
- [x] CRM-0.3.1 Settings: issuer, audience, algorithm, access and refresh lifetimes, key source (all validated at startup).
- [x] CRM-0.3.2 Key management: active and retired keys, `kid`, JWKS endpoint, rotation test.
- [x] CRM-0.3.3 `RefreshToken` model (hashed, family id, rotated_at, expiry, reuse detection) and migration.
- [x] CRM-0.3.4 Token service: mint access token (one workspace), rotate refresh, revoke family, Redis deny-list.
- [x] CRM-0.3.5 Endpoints: token (after login and MFA), refresh, logout, switch-workspace, JWKS.
- [x] CRM-0.3.6 `JwtAuth` Ninja authenticator returning a `Principal`; added to `api_auth` beside session and `ApiToken`.
- [x] CRM-0.3.7 Tests: expired, bad signature, `alg=none`, algorithm confusion, wrong `aud`, unknown `kid`, revoked `sid`, refresh reuse kills the family, MFA gate.
- CRM-0.3.8 BFF: Next.js route handlers keep the pair in HttpOnly cookies and refresh transparently.
- Status: backend done (settings, keyring, refresh rotation, JwtAuth, JWKS, 22 tests); BFF cookie flow (0.3.8) pending.
- Done when: every API endpoint works with a bearer JWT and the web app no longer needs the Django session.

## CRM-0.4 Tenancy and RLS (L) - ADR 0009
Needs: CRM-0.3.
- [x] CRM-0.4.1 `Organization` model, `Workspace.organization` FK, plan and limit fields, data migration for existing workspaces.
- [x] CRM-0.4.2 `core.tenancy`: context manager and middleware that set `app.workspace_id` per transaction.
- [~] CRM-0.4.3 Separate DB roles (app role without BYPASSRLS, migrator role); compose and settings updates.
- [x] CRM-0.4.4 Reusable migration helper that enables RLS and creates the policy for a table.
- [x] CRM-0.4.5 Celery base task that requires `workspace_id` and enters the tenant context.
- [x] CRM-0.4.6 Cross-tenant test harness: `config/tests/test_crm_security_sweep.py` discovers every CRM route from the OpenAPI document and calls it as an anonymous caller and as a stranger from another workspace; both must be refused.
- CRM-0.4.7 Apply RLS to existing tenant tables in staged reversible steps (expand, verify, enforce).
- Status: Organization (expand + backfill), tenant context, middleware, TenantTask, RLS operation and runtime-role command done with tests.
  Open: 0.4.3 compose/prod wiring (runtime role as the app login), 0.4.6 endpoint-discovering harness (only the table-level guard exists), 0.4.7 RLS on legacy tables, 0.4.8 make Workspace.organization NOT NULL.
- Done when: with no tenant context every tenant table returns zero rows, proven by tests.

## CRM-0.5 CRM package skeleton and guardrails (M)
Needs: CRM-0.4.
- [x] CRM-0.5.1 `crm` package with module folders and a public `services.py` per module.
- [x] CRM-0.5.2 import-linter contracts: modules only via services, channels only via the gateway, `core` never imports `crm`.
- [x] CRM-0.5.3 Extend the permission catalogue: CRM module permissions and record scope (all, assigned only).
- [x] CRM-0.5.4 Event envelope schema in `contracts/events/`, outbox publisher helper, idempotent consumer helper.
- [x] CRM-0.5.5 CI: contracts check, cross-tenant suite, migration lint, coverage floor on changed lines.
- Status: events module (envelope, registry, publisher, idempotent consumers, schema export), CRM permissions and record scope, import contracts, CI drift check done. Modules join as their own Django apps (`crm.<module>`) with their own migrations.
- Done when: `make verify` exists and runs every gate.

## CRM-0.6 Local environment (S)
- [x] CRM-0.6.1 Compose services: Mailpit, MinIO. Provider simulators arrive with each channel task.
- [x] CRM-0.6.2 `make verify` target; Windows notes.
