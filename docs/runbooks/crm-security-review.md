# CRM security review

Status: reviewed module by module on the day the phase closed. Re-run when a module changes
its trust boundary (new public endpoint, new provider, new secret).

## What is enforced by machines

| Control | Where it is enforced |
|---|---|
| Every CRM endpoint refuses anonymous callers | `config/tests/test_crm_security_sweep.py` walks the OpenAPI document |
| Every CRM endpoint refuses a signed-in stranger from another workspace | the same sweep |
| Public endpoints never read a workspace header | the same sweep |
| One tenant per row, enforced by the database | RLS on every CRM table, added in each migration (ADR 0009) |
| A cross-tenant test per endpoint | each module's tests (`*_cross_tenant*`, `test_*_tenants`) |
| Secrets sealed, never stored in clear | `core/vault.py`; sealed fields in `SmsCredential`, `MetaAccount`, `WebhookEndpoint` |
| No secret in the repository | gitleaks job in CI |
| Known-vulnerable dependencies | `pip-audit` and `pnpm audit` jobs in CI |
| Public schema names are unique | `config/tests/test_schema_names.py` |
| Layering between modules | import-linter contracts in `pyproject.toml` |

## Module by module

| Module | Trust boundary | Review result |
|---|---|---|
| contacts, fields, lists | Authenticated API, record scope | Record scope applied to every list and detail read. Bulk and import size limits are settings. |
| consent | Ledger written by API, keywords, links | Append-only ledger; STOP words handled before anything else on inbound. |
| inbox | Authenticated API and provider callbacks | Provider ids unique per workspace, so a replayed callback stores once. |
| sending (gateway, email) | Only door for outbound messages | Ordered rule list; adapters are not reachable by other modules (import contract). One-click links are signed and expire. |
| webhooks | Public, signed (HMAC-SHA256 with timestamp) | Secret sealed; replay window is a setting; body archived once by hash; wrong id, wrong workspace and disabled endpoints look the same (404). |
| sms (Twilio) | Public callbacks, signed by Twilio | Signature checked with the workspace's own token before anything is parsed. Messages carry an idempotency token, so a retry never sends twice. |
| voice | Public callbacks, signed by Twilio | Same check. TwiML text is escaped. Recording URLs are stored, not fetched. |
| meta (WhatsApp, Messenger) | Public webhook, `X-Hub-Signature-256` | Verify token compared in constant time. The Cloud API has no idempotency key, so an unknown outcome is never retried blindly. |
| leads | Triggered by the Meta webhook | Only ids arrive by webhook; answers are fetched from Meta. Conversions API gets hashed identifiers only, and only for people who agreed. |
| workflows | Authenticated API, events | Definitions validated before saving (no cycles, no unknown steps). Every send goes through the gateway with a stable idempotency key. Agents can only propose, a person approves. |
| reports | Authenticated API, reports permission | Aggregates only; no message text leaves. |
| usage, notifications | Authenticated API | Per-person data is filtered to the caller. |

## Residual risks and decisions

1. **Runtime database role.** Production should connect as `app_rt`, so the row-level policies apply to
   the application itself. Legacy marketing tasks still need a cross-tenant context, so the switch is
   deferred (tasks CRM-0.4.3 and 0.4.7). Until then tenant isolation relies on the query layer plus the
   tests above; RLS is a second line, already installed.
2. **WhatsApp and Instagram duplicates.** Neither API accepts an idempotency key. A response lost after
   Meta accepted a message is reported as an unknown outcome and not retried. A rare lost message is
   preferred over a duplicated one.
3. **Recording and media links.** Twilio recording URLs and media ids need the provider's credentials
   to open. They are stored as given and shown to authorised people only. A proxy that streams them with
   the workspace's credentials is not built.
4. **Raw webhook archive.** `WebhookDelivery` keeps request bodies for `CRM_WEBHOOK_RETENTION_DAYS` (30 by
   default); the daily `crm.privacy.run_retention` job removes them. Bodies of inbound email contain personal
   data, so an erased person's archived payloads disappear within that window rather than at once.
5. **Public base URL.** Twilio signatures cover the full public address. `PUBLIC_BASE_URL` must be the
   address Twilio calls, or valid callbacks will be refused (the safe failure).
6. **Throttles.** Public CRM endpoints have their own throttle scopes. A provider sending from few
   addresses is limited per address; raise `600/m` in `crm/common/throttles` users if a provider needs it.

## Privacy tooling

- **Access request:** `GET /crm/contacts/{id}/privacy-export` returns one JSON file with the profile, consent history,
  messages, notes, tasks, calls, deals, lead form answers and automations. Needs the export permission and is
  written to the audit log.
- **Erasure:** `POST /crm/contacts/{id}/erase` removes the contact and everything attached by foreign key; the
  `crm.contact.erased` event then clears the person's id from the analytics tables (`FirstResponse`,
  `ConversionLog`), which keep their counts.
- **Retention (daily):** raw webhook bodies, idempotency marks, finished automation runs, read notifications and
  call recordings and transcripts. Periods are the `CRM_*_RETENTION_DAYS` settings. Open automation runs and
  unread notifications are never purged.
- **Hooks:** each module owns a `privacy.py` that only `crm.privacy` imports, so a new module adds its export and
  retention in one place.
