# ADR 0003 — API boundary, session auth and BFF

Status: accepted (2026-09-29)

## Decision
- Django Ninja under `/api/v1`, typed with Pydantic, OpenAPI exported to `packages/api-client/openapi.json`; CI fails when it drifts. TypeScript types are generated with `openapi-typescript`.
- Authentication is a Django session cookie. The browser only talks to the Next.js origin; Next.js rewrites `/bff/*` to the API, so no token ever exists in browser JavaScript.
- Unsafe requests require a CSRF token (`/auth/csrf` then `X-CSRFToken`). Django checks the `Origin` header, so the public web origin must be listed in `APP_CSRF_TRUSTED_ORIGINS`; startup validation requires it in staging and production.
- Errors are RFC 9457 problem responses with a stable machine `code`; human text comes from the message catalog.
- Unsafe requests may carry `Idempotency-Key`; responses are stored and replayed per user, and a crashed request releases its key.

## Consequences
Cross-workspace access returns "workspace required" for both unknown and foreign ids so ids cannot be enumerated.
