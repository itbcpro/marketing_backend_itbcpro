# Database roles and row level security

Two roles are used (ADR 0009):

| Role | Purpose | Rights |
|---|---|---|
| owner (`app` in development) | runs migrations, owns tables | may bypass row level security |
| runtime (`APP_DB_RUNTIME_ROLE`, default `app_rt`) | serves requests and workers | DML only, NOSUPERUSER, NOBYPASSRLS |

Setup: run migrations as the owner, then `make db-roles` (set `APP_DB_RUNTIME_PASSWORD` to give the role a login).
In staging and production point `APP_DATABASE_URL` of the web and worker processes at the runtime role and
keep the owner URL only for the migration job. Policies use `FORCE ROW LEVEL SECURITY`, so even the table owner
is filtered; only superusers and `BYPASSRLS` roles see everything.

Every new tenant table: extend `TenantModel`, then add `EnableRowLevelSecurity("<Model>")` from
`core.migration_ops` to the same migration. `core/tests/test_tenancy.py` fails when a table is missing it.
Background tasks that touch tenant data use `core.tasks.tenant_task` and pass `workspace_id`.
