# ADR 0004 — Credential vault, audit chain and outbox

Status: accepted (2026-09-29)

## Decisions
- **Vault**: AES-256-GCM envelope encryption. Each secret has its own data key, wrapped by a versioned master key from `APP_VAULT_MASTER_KEYS`. The connection id and credential kind are bound as associated data, so a credential moved to another connection fails verification. Rotating the master key only re-wraps data keys.
- **Audit**: append-only `AuditEvent` per workspace chain. Each event stores `prev_hash` and `hash` (SHA-256 over canonical fields); a per-chain advisory lock serialises writers. Database triggers reject UPDATE, DELETE and TRUNCATE. `audit.verify_chain` runs hourly and reports the first broken sequence number.
- **Outbox**: messages are written in the caller's transaction and delivered by a Celery task using `SELECT ... FOR UPDATE SKIP LOCKED` with exponential backoff. Handlers must be idempotent.

## Consequences
Tampering with audit rows requires disabling triggers and is still detected by chain verification. Master keys live outside the database; losing every master key version makes stored credentials unrecoverable, so key backup is part of the P0.16 runbook.
