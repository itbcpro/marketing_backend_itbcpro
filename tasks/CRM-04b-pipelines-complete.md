# CRM-4b Pipelines, complete

Module doc: `docs/product/04-pipelines.md`. ADRs: 0007 (CRM module), 0009 (tenancy and RLS), 0002 (English only).
Builds on CRM-4.1 to 4.5. Every stage is its own change of at most about 400 lines, with tests and gates green.

## Design rules for this task (SOLID)
- Single responsibility: one module per concern. API schemas and routes stay thin; use cases live in
  `services.py` (public surface) and delegate to small modules (`clean.py`, `filters.py`, `bulk.py`, `insights.py`, `exports.py`).
- Open/closed: deal filters, bulk actions and stage entry rules are registries of small functions, so a new one
  is a new entry and no existing code changes.
- Liskov and interface segregation: UI components take only the callbacks and data they use, typed narrowly.
- Dependency inversion: components depend on hooks and a typed gateway (`use-deal-commands`), never on `fetch`.

## Stage A: manage pipelines, stages and lost reasons (web only)
- [x] CRM-4b.1 Pipeline settings panel: rename, archive, add stage, edit name and probability, reorder, delete with a target stage.
- [x] CRM-4b.2 Lost reasons manager (list, add, archive).

## Stage B: deal drawer
- [x] CRM-4b.3 Team endpoint (workspace members) for owner pickers.
- [x] CRM-4b.4 Drawer: edit name, value, currency, source; owner; win, lose, abandon, reopen; delete; stage history timeline.
- [x] CRM-4b.5 Tasks on deals (`Task.opportunity`), add, complete, list in the drawer.

## Stage C: find and filter
- [x] CRM-4b.6 API filters: search text, value range, created range, close date range, source, priority, tag, owner; sort; offset paging. Registry based.
- [x] CRM-4b.7 Filter bar, status filter on the board, sort order, saved views (per viewer, browser storage), KPI strip.

## Stage D: deal and stage data
- [x] CRM-4b.8 Migration: `expected_close_date`, `priority`, `tags`, `probability_override` on deals; `rotting_days` and `required_fields` on stages. Reversible.
- [x] CRM-4b.9 Services and API for the new fields; weighted totals use the deal override; stale detection uses the stage threshold; stage entry rules refuse a move that lacks required fields.

## Stage E: board experience
- [x] CRM-4b.10 Rich cards (contact, owner, close date, priority, tags, days in stage, open tasks), quick actions (stage menu, won, lost).
- [x] CRM-4b.11 Drag and drop with a drop indicator, optimistic move with rollback and a toast, skeleton, refresh on focus, single column stage tabs on phones.
- [x] CRM-4b.12 Multi-select with bulk move, assign, win, lose and delete.

## Stage F: insights
- [x] CRM-4b.13 Forecast by month, funnel conversion, time in stage, lost reasons, performance per owner.
- [x] CRM-4b.14 CSV export of the current filter.

## Out of scope here (decision recorded in the module doc)
- Products, quotes and e-signature (module doc: out of scope for MVP).
- Several contacts or a company per deal (needs a link table and a privacy review): separate task.
- Push updates over websockets (no infrastructure yet): the board refreshes on focus instead.

## Notes on how it was built
- Backend: `clean.py` (input cleaning), `rules.py` (stage requirements), `filters.py` (filters and sorts), `bulk.py` (bulk actions), `insights.py` (analysis), `exports.py` (CSV); `services.py` stays the public surface.
- Web: `features/crm/pipelines/` holds one file per concern; `pipelines-view.tsx` only composes. Components take `DealCommands` and data, never the API.
- Tests run in the api container with `DJANGO_SETTINGS_MODULE=config.settings.test` (the container's default settings enforce MFA).

## Done when
Acceptance criteria met, tests added including cross-tenant, `make lint typecheck layers test test-web check-i18n check-language` green, `make openapi` run, module doc updated.
