# ADR 0009 - Multi-tenancy: Organization, Workspace and Postgres row level security

Status: accepted (2026-11-18)

## Context
The product must be sellable to many customers (and to agencies managing several client businesses). Tenant
isolation currently relies on `for_workspace()` in application code only.

## Decision
- Hierarchy: `Organization` (the paying customer or agency, owns billing and plan) -> `Workspace` (one business,
  the data tenant) -> `Membership`. The existing `Workspace` stays the tenant key on every table.
- Row level security on every CRM table: policy `workspace_id = current_setting('app.workspace_id')::uuid`.
  The request or task context runs `SET LOCAL app.workspace_id` inside its transaction (`core.tenancy.use_workspace`).
  The application connects with a role that is not owner, not superuser and has no `BYPASSRLS`; migrations run
  with a separate privileged role. Missing context means zero rows, never all rows.
- Cross-tenant access returns "not found" (already the platform convention).
- Plans and limits (contacts, messages, seats) live on `Organization` and are enforced in services.
- Every CI run executes the cross-tenant suite: each endpoint is called with tenant A's token and tenant B's ids.

## Consequences
- Celery tasks must receive `workspace_id` and enter the tenant context before touching data.
- Admin and reporting jobs that span tenants use an explicit, audited `system_context`.
