# ADR 0008 - JWT access tokens with refresh rotation

Status: accepted (2026-11-18). Extends ADR 0003.

## Context
ADR 0003 chose session cookies only, plus opaque machine tokens. A sellable multi-tenant product needs
standard, verifiable, short-lived credentials for the web app, the realtime gateway, mobile apps, partners and
future services, without a database lookup per request.

## Decision
- The API issues **signed JWT access tokens** (asymmetric, EdDSA/Ed25519 by default, algorithm configurable and
  pinned on verification: never read from the token header). Lifetime 10 minutes (setting).
- Claims: `iss`, `aud`, `sub` (user id), `ws` (workspace id), `role`, `scp` (scopes), `sid` (session family id),
  `jti`, `iat`, `nbf`, `exp`. One token is valid for exactly one workspace; switching workspace mints a new token.
- Public keys are served at `/.well-known/jwks.json` with `kid`. Private keys come from the secret manager or
  environment, never the repository. Several keys can be active, so rotation needs no downtime.
- **Refresh tokens** are opaque random values, stored hashed, single use with rotation. Re-using a rotated token
  revokes the whole family (`sid`). Refresh expiry is absolute and idle-based, both configurable.
- Login (password + MFA, passkey) stays the only way to obtain a first token pair. MFA enrolment gate applies.
- Verification is stateless except for a short-lived deny-list of revoked `sid` values (Redis) checked on each
  request, so logout and "sign out everywhere" take effect within seconds.
- Browser clients keep ADR 0003's BFF: Next.js stores the token pair in `HttpOnly`, `Secure`, `SameSite=Lax` cookies
  and attaches the bearer header server-side. Tokens never exist in browser JavaScript. The existing session
  authentication stays enabled during migration and is removed once the web app has moved.
- Machine `ApiToken`s remain for long-lived integrations; they are not JWTs.
- All three authenticators resolve to one `Principal(user, workspace, role, scopes)` used by the permission layer.

## Alternatives considered
- Symmetric HS256: rejected, every verifier would hold the signing secret.
- Keep sessions only: rejected, no stateless verification for realtime and external clients.

## Consequences
- New dependency: `PyJWT[crypto]` (cryptography is already present).
- Tests: algorithm confusion, expired, wrong audience, unknown `kid`, revoked family, refresh reuse.
