# Platform - agent guide

## What this repo is
Django API (`apps/api`, includes the CRM package `crm`). The Next.js web app lives in its own repository
(`marketing_front_itbcpro`); this repo publishes the OpenAPI contract (`packages/api-client`). Contracts: `packages/api-client/openapi.json` and `contracts/events/`.

## Before you code
1. Read the task file in `tasks/`, the linked module doc in `docs/product/` and the ADRs in `docs/adr/`.
2. Write a short plan (files, tests, risks) before editing.

## Hard rules
- English only in code, comments, docs and UI (ADR 0002). User-visible strings come from catalogs.
- Nothing hard-coded: values live in settings, enums, data tables or catalogs.
- Every CRM table extends `TenantModel` and has RLS (ADR 0009). Every endpoint has a cross-tenant test.
- All outbound customer messages go through `crm.sending.gateway.send()` (ADR 0010). Never call an adapter directly.
- Module code imports other modules only through their `services.py`.
- No secrets in code, tests, logs or fixtures. No real provider calls in tests; use fakes and simulators.
- Never weaken or delete a test to make it pass. Migrations must be reversible.
- Keep a change under about 400 lines; split otherwise.

## Commands (see Makefile)
`make lint typecheck layers test check-i18n check-language`, `make openapi` after API changes.

## Definition of done
Acceptance criteria met, tests added (including cross-tenant), gates green, docs and task checkboxes updated.
