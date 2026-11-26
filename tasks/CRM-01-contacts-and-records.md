# CRM-1 Records, contacts, fields, tags, consent

Needs: CRM-0. Spec: ghl-spec sections 3, 4, 12.

## CRM-1.1 Field definitions and custom values (M)
- [x] CRM-1.1.1 Models: `FieldDefinition` (object type, key, label, type, options, folder, position) and `WorkspaceValue` (workspace constants).
- [x] CRM-1.1.2 Typed value storage and validation per field type; unique key rules; reserved keys.
- [x] CRM-1.1.3 Service API: create, update, reorder, archive (used fields are never hard-deleted).
- [x] CRM-1.1.4 Merge-field resolver (`{{contact.key}}`, `{{workspace_values.key}}`) with safe escaping and a missing-value policy.
- [x] CRM-1.1.5 Tests including cross-tenant and template injection.

## CRM-1.2 Contact core (L)
- [x] CRM-1.2.1 `Contact`, `ContactIdentity` (channel, external id, normalised value, unique per workspace), additional emails and phones.
- [x] CRM-1.2.2 Phone normalisation to E.164 and email normalisation in one validator module.
- [x] CRM-1.2.3 Duplicate policy (setting): match order, upsert, manual merge with per-field winner, merge audit and undo window.
- [x] CRM-1.2.4 Soft delete, restore window, hard erase for privacy requests.
- [x] CRM-1.2.5 Ownership: assigned user, followers, round-robin assigner service.
- [x] CRM-1.2.6 Events: created, updated, deleted, merged, assigned (outbox).
- [x] CRM-1.2.7 API: CRUD, upsert, search, cursor pagination (idempotency comes from the shared middleware; ETag is not added).

## CRM-1.3 Tags (S)
- [x] CRM-1.3.1 `Tag` and join table, rename, merge, usage count, add and remove events.
- [x] CRM-1.3.2 Bulk add and remove as a background job.

## CRM-1.4 Companies and associations (M)
- [x] CRM-1.4.1 `Company` model and API.
- CRM-1.4.2 Generic `Association` with labelled types and cardinality.
- CRM-1.4.3 Custom objects (schema and records) on the same engine.

## CRM-1.5 Consent ledger (M) - ADR 0010
- [x] CRM-1.5.1 Append-only `ConsentEvent`; derived current state per contact, channel and purpose.
- [x] CRM-1.5.2 Law profiles as data (CASL default); quiet-hour rules as data.
- [x] CRM-1.5.3 Opt-out keyword catalog per channel and language (data, not code).
- [x] CRM-1.5.4 Events, read API, and evidence export for one contact.

## CRM-1.6 Notes, tasks, activity timeline (M)
- [x] CRM-1.6.1 `Note` (mentions, pin) and `Task` (assignee, due, status, reminders).
- [x] CRM-1.6.2 Append-only `Activity` fed by outbox consumers; audit linkage.

## CRM-1.7 Smart lists, import, export (L)
- [x] CRM-1.7.1 Filter language (AND/OR groups) compiled to safe ORM queries with a field whitelist; no raw SQL.
- [x] CRM-1.7.2 `SmartList` saved filters, columns, sharing.
- [x] CRM-1.7.3 CSV import backend: upload, mapping, dry run, error file, idempotent job.
- [x] CRM-1.7.4 Export job with permission and scope checks.
- [x] CRM-1.7.5 Bulk actions framework: job, progress, per-record result, cancel.

## CRM-1.8 Contacts UI (L)
- [x] CRM-1.8.1 Route group `(app)/crm`, nav entry, i18n namespace, generated client.
- [x] CRM-1.8.2 Contacts table with columns, filters, smart lists, bulk bar.
- [x] CRM-1.8.3 Contact record page (details, timeline, notes, tasks) with accessibility tests.
- [x] CRM-1.8.4 Import wizard UI.
- Done when: a user can import, filter, edit and merge contacts end to end; e2e covers it.
