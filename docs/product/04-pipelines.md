# Module: Pipelines and opportunities
Status: built   Owner: Ahmad   Phase: 4 and 4b   Source: ghl-spec section 5

## Purpose
Track deals through ordered stages with independent status, history and forecasts, and give a team everything it needs to work them: filters, bulk actions, tasks, insights and export.

## In scope / Out of scope
In: pipelines, stages (probability, stale threshold, entry requirements), opportunities, Kanban, list and insights views, stage history, lost reasons, stale detection, tasks on deals, search and filters, saved views, bulk actions, forecast, funnel, CSV export.
Out: quotes and e-signature, product catalogue and line items, several contacts or a company per deal (needs a link table and a privacy review), push updates over websockets (the board refreshes when the tab regains focus).

## Data model
`Pipeline`, `Stage(position, probability, rotting_days, required_fields)`, `Opportunity(contact, pipeline, stage, status, value_micros, currency, assigned_to, source, expected_close_date, priority, tags, probability_override, lost_reason)`, `StageChange`, `LostReason`. A task may point to a deal (`Task.opportunity`).

## Business rules
1. Status (open, won, lost, abandoned) is independent of stage.
2. Every stage or status change writes a history row and an event.
3. Money is integer micros plus ISO currency.
4. Reordering stages never loses opportunities; deleting a stage requires a target stage.
5. Lists, summaries, insights and exports honour record scope.
6. A stage may require things of a deal before it can enter (`value`, `expected_close_date`, `owner`); the list of requirements is a registry in `crm.pipelines.rules`. A move or a bulk move that does not meet them is refused and names what is missing.
7. A deal's own `probability_override` replaces its stage's probability in every weighted figure.
8. A stage's `rotting_days` replaces the workspace default (`CRM_STALE_DEAL_DAYS`) for deals in it.
9. Filters, sort orders and bulk actions are registries (`filters.py`, `bulk.py`): adding one is adding an entry. Deleting deals in bulk needs the delete permission; every other bulk action needs edit.
10. Exports are formula-safe, limited by `CRM_EXPORT_MAX_ROWS` and audited.

## API (under `/api/v1/crm`)
Pipelines and stages: `pipelines`, `pipelines/{id}/stages`, `pipelines/{id}/stages/order`, `stages/{id}`, `stage-requirements`, `lost-reasons`.
Deals: `opportunities` (filters, `sort`, `limit`, `offset`), `opportunities/summary`, `opportunities/tags`, `opportunities/stale`, `opportunities/bulk`, `opportunities/{id}` and its `move`, `assign`, `win`, `lose`, `abandon`, `reopen`, `history`.
Reports: `pipelines/{id}/insights`, `opportunities-export.csv`.
People: `team` (workspace members, for owner pickers).
Deal fields can be narrowed by: pipeline, stage, status, contact, owner or unassigned, priority, source, tag, text, value range, created range and close date range.

## Events
`crm.opportunity.created|stage_changed|status_changed|assigned`.

## Acceptance criteria
- Given a lost deal in stage "Proposal", when read, then status is lost and stage is unchanged.
- Given a stage deletion with a target, when applied, then all its deals move to the target in one transaction.
- Given a stage that requires a value, when a deal without one is moved in, then the move is refused and the deal stays where it was.
- Given a deal with its own probability, when the board is totalled, then the weighted figure uses it.
- Given a stage with its own stale threshold, when deals sit there longer, then only those are reported stale.
- Given a bulk action over deals of several workspaces, when applied, then only the caller's workspace is touched.

## Open questions
Weighted forecast formula per currency (pipelines are expected to use one currency; sums are in the stored currency).

## Deal notes
A note may point at one deal of its contact (`Note.opportunity`, `opportunity_id` on create, filter on the contact notes list). The deal drawer shows only the notes of that deal; the contact page still shows all of them.

## Payments on a deal
A deal keeps a ledger of payments and refunds recorded by hand: amount in the deal currency, status (paid, pending, failed), method, reference and note. The drawer shows deal value, paid (net of refunds), balance and pending. A refund may not exceed what has been received, and a paid payment that refunds depend on cannot be marked failed or deleted. Editing needs the edit permission, deleting needs the delete permission. Recording publishes `crm.payment.recorded` and shows on the contact timeline; payments are part of the access export. Not built: online checkout, invoices, payment links and provider webhooks.
