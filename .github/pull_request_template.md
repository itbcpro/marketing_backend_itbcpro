## What and why

<!-- One or two sentences. Link the task file in `tasks/`. -->

## Checklist

- [ ] Acceptance criteria of the task are met
- [ ] Tests added, including a cross-tenant test for every new endpoint
- [ ] `make lint typecheck layers test check-i18n check-language` is green
- [ ] `make openapi` run if the API changed; event schemas regenerated if events changed
- [ ] Migrations are reversible; new CRM tables extend `TenantModel` and enable row-level security
- [ ] No secrets, real provider calls or hard-coded values; user-visible text comes from catalogs
- [ ] Docs and task checkboxes updated

## Risks and follow-ups

<!-- What could go wrong, what is deliberately left for later. -->
