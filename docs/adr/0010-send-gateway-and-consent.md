# ADR 0010 - One send gateway and a consent ledger

Status: accepted (2026-11-18)

## Decision
- `crm.sending.gateway.send()` is the only entry point for outbound messages (inbox, bulk, workflow, API, AI agent).
  Order of checks: permission, consent, channel window (WhatsApp and Messenger 24 h), quiet hours, plan limit and
  wallet, throttle. A blocked send is stored with a machine reason code and is never silently dropped.
- Consent is an append-only ledger per contact, channel and purpose: status, source, evidence, actor, timestamp.
  The current state is derived. Every opt-out (keyword, link, toggle, API) writes one ledger row and emits an event.
- Law profile per workspace: default Canada (CASL, PIPEDA), with US (TCPA, 10DLC) and EU (GDPR) profiles as data,
  not code branches.
- Provider adapters implement one port per channel and a contract test suite; fakes ship with the code.

## Consequences
- Adapters can never be imported outside `crm.channels`; import-linter enforces it.
