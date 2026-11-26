# Module: Tenancy, identity and permissions
Status: approved   Owner: Ahmad   Phase: 0   Source: ghl-spec section 2; ADR 0008, 0009

## Purpose
Isolate customers from each other and let one organization run several workspaces, with short-lived tokens and explicit permissions.

## In scope for MVP / Out of scope
In: Organization, Workspace, Membership with role and record scope, JWT access tokens, refresh rotation, RLS on all CRM tables, audit log.
Out for MVP: white-label domains, snapshots (templates of a workspace), per-agency branding, SSO. (Phase CRM-8.)

## Data model
`Organization(name, slug, plan_key, limits, is_active)` -> `Workspace(organization, ...)` -> `Membership(user, role, record_scope)`. Every CRM table has `workspace_id` and an RLS policy.

## API
Auth under `/api/v1/auth`: token, refresh, token/revoke, logout. JWKS at `/.well-known/jwks.json`. Workspace endpoints under `/api/v1/workspaces`.

## Events
Consumed by CRM: none yet. Emitted: `workspace.created` through the audit log.

## Business rules
1. A token is valid for exactly one workspace; switching workspace mints a new token.
2. A user without membership gets "workspace required" for any workspace id (no enumeration).
3. Refresh tokens are single use; reuse revokes the family.
4. Permissions come from role; record scope narrows owned records (contacts, opportunities, conversations).
5. Without tenant context the database returns no rows.
6. Plan limits (contacts, messages, seats) are enforced in services, never in views.

## Edge cases
Removing a member revokes their refresh tokens on next refresh; disabling a user rejects access tokens immediately; cache outage fails closed.

## Security notes
Algorithm is pinned server side. Private keys come from the secret store. MFA enrolment gate applies before tokens are issued.

## Acceptance criteria
- Given a token for workspace A, when it calls a workspace B endpoint, then the answer is "workspace required".
- Given a member with scope `assigned`, when listing contacts, then only contacts assigned to them are returned.
- Given no tenant context, when a runtime-role query reads a CRM table, then zero rows are returned.

## Open questions
Plan catalogue (limits per plan) and billing provider.
