# ADR 0007 - CRM module inside the API (supersedes the "no CRM" scope of P2.16)

Status: accepted (2026-11-18)

## Context
The plan so far said the `inbound` app is "not a CRM". The product direction changed: the platform will include
a full CRM with an omnichannel inbox (contacts, companies, pipelines, conversations, workflows) and will be
sold to other businesses. The reference behaviour is documented in `docs/product/source/ghl-spec.md` and the
build method in `docs/product/source/agent-playbook.md`.

## Decision
- The CRM is a new bounded context, the Django package `crm` (sub-packages per module), inside `apps/api`.
  It is a modular monolith, not a separate service: identity, tenancy, connections, outbox, audit and the
  approval layer already live here, and a split would duplicate all of them. The module boundary is enforced by
  import-linter so that extraction later stays cheap.
- Tenant = `Workspace` (see ADR 0009). Every CRM table extends `TenantModel` and is protected by row level security.
- Every message to a customer, from any source, passes one send gateway (ADR 0010). Adapters never get called directly.
- The `inbound` app becomes a thin compatibility layer and is folded into `crm` (contact events become
  activities on a contact). Instagram DMs and comments keep their tables in `social`; `crm` links them to a contact
  through `crm.ContactIdentity` and mirrors them into the unified conversation (phase CRM-2).
- Public contracts: OpenAPI (`packages/api-client/openapi.json`) and event schemas in `contracts/events/`.
- UI lives under `apps/web/src/features/crm` and the route group `(app)/crm`.

## Alternatives considered
- Separate Django service (playbook default): rejected for now; the shared tenancy, auth and approval code would
  be copied or exposed as network APIs for no present benefit.
- Reuse `social.SocialConversation` as the inbox: rejected; it is account-bound and Instagram-specific.

## Consequences
- `tasks/CRM-*.md` become the work plan. P2.16 is superseded where it says "No CRM features".
- Outbound customer messages are risk class R3 for agents, but ordinary user sends are not approval-gated.
