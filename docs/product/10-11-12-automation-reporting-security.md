# Modules: Automation, reporting, security
Status: draft   Owner: Ahmad   Phases: 7, 8   Source: ghl-spec sections 11, 14; playbook section 10

## Automation (phase 7)
Workflows with versions; a data-driven trigger registry (one class per trigger) and action registry; durable scheduled steps and event waits; stop on reply; quiet hours; execution log per contact. Every send goes through the gateway; agents reach workflows only through `ProposedAction`.
Acceptance: a contact enrolled twice with re-entry off runs once; a wait step survives a worker restart; a failed step retries with backoff then lands in the dead-letter table.

## Reporting (phase 8)
Event-fed analytics tables: first response time, delivery by channel and error, pipeline value and conversion, attribution by source, agent performance. Stage and status histories are stored, not only current values.
Acceptance: first response time is computed when the first outbound after an inbound is written.

## Security (cross-cutting)
- Tenant isolation: RLS plus a cross-tenant test for every endpoint.
- Secrets in the vault only; webhooks verify signatures and replay windows; raw payloads archived with retention.
- No message content or PII in logs; erasure removes data across messages, files and activity.
- Rate limits per user, token and tenant; idempotency keys on every unsafe POST that sends or charges.
- CI gates: lint, types, import contracts, migrations check, OpenAPI and event-schema drift, secret scan, dependency audit.
Acceptance: the cross-tenant suite runs in CI and fails on any leak.
