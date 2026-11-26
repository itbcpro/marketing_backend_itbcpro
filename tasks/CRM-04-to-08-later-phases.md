# CRM-4 to CRM-8 (outline, expanded to full task files when the phase starts)

## CRM-4 Pipelines and opportunities (needs CRM-1)
- [x] CRM-4.1 `Pipeline`, `Stage` (probability), `Opportunity` (status independent of stage), stage history.
- [x] CRM-4.2 Services: move, win, lose with reason, stale detection, events.
- [x] CRM-4.3 API: CRUD, search, bulk move, custom fields on deals.
- [x] CRM-4.4 Kanban board with drag and drop and a keyboard-accessible stage menu, list view with status filter, lost reason prompt.
- [x] CRM-4.5 Forecast (weighted stage totals, stale deals); richer reports come with CRM-8.

## CRM-5 SMS and voice (needs CRM-3.1, CRM-3.2)
- [x] CRM-5.1 Twilio adapter behind the send gateway, sealed per-workspace credentials, sender numbers, data-driven registration steps (Canada default) with a setup screen.
- [x] CRM-5.2 Segment and cost counter (GSM-7, UCS-2), MMS media, signed delivery callbacks, error taxonomy as data.
- [x] CRM-5.3 STOP, START, HELP through the consent ledger (inbound texts, verified end to end).
- [x] CRM-5.4 Voice: incoming calls with forward and ring timeout, voicemail with recording and transcript, missed-call text-back through the gateway, call log with timeline entries, click to call (agent first, then the customer) through the gateway; calls screen.
- [x] CRM-5.5 Simulator for local and CI (`crm.sms.simulator`).

## CRM-6 WhatsApp, Messenger, lead ads, CAPI (needs CRM-3.1 and the `/meta` connection layer)
- [~] CRM-6.1 Meta account with sealed keys, WABA and number registry, quality and tier status, embedded signup completion (code exchange and webhook subscription). Open: the Facebook login button of the signup dialog in the web app.
- [x] CRM-6.2 24 h window per contact (templates bypass it, replies need it), template manager with submit, approval webhook and hourly sync, template sender in the inbox composer.
- [x] CRM-6.3 Messenger adapter with human-agent tag window and signed webhooks; Instagram direct messages sent through the gateway using the social module's connection (no token copied).
- [x] CRM-6.4 Lead ads webhook (fetch by id, retried), per-form field mapping with tags and consent the form covers, attribution on the contact, Conversions API reporting by rule with consent check and hashed identifiers, setup screen.
- [x] CRM-6.5 Rate cards versioned by country and category: WhatsApp templates priced by their category, replies as service.

## CRM-7 Workflow engine (needs CRM-1 to CRM-3)
- [x] CRM-7.1 Definitions with validation (unknown steps, bad targets, cycles), immutable published versions, data-driven trigger registry (one class per trigger).
- [x] CRM-7.2 Runtime: enrolment with re-entry rules, durable waits in the database, wait for reply with timeout, stop on reply, step log, retries with backoff, dead letters, stable send keys.
- [x] CRM-7.3 Action registry (send, tag, task, owner, field, wait, wait for event, branch, finish); every send goes through the gateway.
- [x] CRM-7.4 Builder UI: list, form-based editor driven by the catalog (steps, branches, targets), draft and publish, test mode that walks a real contact without sending, replay of each run, automations tab on the contact.
- [x] CRM-7.5 Agents reach workflows only through `ProposedAction` (enrol is R2 with approval, exit is its R1 rollback).

## CRM-8 Reporting, plans, billing, hardening
- [x] CRM-8.1 Reports: first response time (event-fed table, closes at the first unblocked reply), delivery by channel and error, pipeline value and conversion, attribution by source and campaign with won value, agent performance; reports screen.
- [x] CRM-8.2 Plans as data with limits (messages, contacts, automations) enforced where they apply; monthly invoices per organization (plan fee plus usage, prepaid usage not billed twice); self-service sign-up with email confirmation (off until switched on); getting-started checklist. Payment collection is open until a provider is chosen.
- [x] CRM-8.3 Security review per module (docs/runbooks/crm-security-review.md), automated sweep of every CRM endpoint for anonymous and stranger access, dependency and secret scans in CI.
- [x] CRM-8.4 Privacy tooling: per-contact data export (audited), erasure follow-through for analytics tables, daily retention for webhook bodies, idempotency marks, finished runs, read notifications and recordings; audit export already existed.
- [x] CRM-8.5 Observability: counters for messages, callbacks and automation steps, backlog gauges (outbox lag, overdue runs, dead letters), alert rules in infra/alerts/crm.rules.yml checked against the metrics, health tab per workspace.
