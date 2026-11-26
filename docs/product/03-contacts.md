# Module: Contacts, companies, consent, activity
Status: approved   Owner: Ahmad   Phase: 1   Source: ghl-spec sections 3, 4, 12; ADR 0010

## Purpose
The contact is the hub: identity, ownership, consent, notes, tasks and a timeline of everything that happened.

## In scope for MVP / Out of scope
In: contact CRUD and upsert, identities, dedupe and merge, owner and followers, companies, consent ledger, notes, tasks, activity feed, smart lists, CSV import and export, bulk actions.
Out for MVP: AI summary of a contact, duplicate suggestions by fuzzy matching, birthdays automation.

## Data model
`Contact(first_name, last_name, email, phone_e164, owner, type, source, timezone, attribution, custom, deleted_at)`; `ContactIdentity(channel, external_id, normalised)` unique per workspace; `Company`; `ConsentEvent(contact, channel, purpose, status, source, evidence, actor, at)`; `Note`, `Task`, `Activity` (append-only); `SmartList`; `ImportJob`, `BulkJob`.

## API
`/crm/contacts` (list, create, upsert, get, patch, delete, restore, merge), `/crm/contacts/{id}/consent`, `/notes`, `/tasks`, `/activity`; `/crm/companies`; `/crm/smart-lists`; `/crm/imports`; `/crm/exports`; `/crm/bulk-actions`.

## Events
`crm.contact.created|updated|deleted|merged|assigned`, `crm.consent.changed`, `crm.task.completed`, `crm.note.added`.

## Business rules
1. Identity match order is a workspace setting (email first or phone first); duplicates merge instead of creating.
2. Merge keeps the winner's id, moves identities, tags, notes, tasks, consent and conversations, records an audit entry and can be undone for a configured window.
3. Deleting is soft; hard erase is a privacy action requiring a separate permission.
4. Consent is an append-only ledger; the current state is derived per channel and purpose.
5. Every opt-out writes one ledger row and emits one event.
6. Lists, search, export and detail honour record scope.
7. Import is idempotent per row (match on email, phone or external id) and produces an error file.

## Edge cases
Two imports of the same file create no duplicates; a contact with only a phone and an opt-out on sms is still created; merging contacts with conflicting consent keeps the most restrictive state.

## Security notes
Exports require `crm_import_export`; PII is excluded from logs; erase removes messages, files and activity rows of the contact.

## Acceptance criteria
- Given a contact with sms opted out, when the gateway is asked to send sms, then it is blocked with reason `consent` (phase CRM-3).
- Given an existing email, when a form posts the same email, then the existing contact is updated and no duplicate exists.
- Given scope `assigned`, when a member requests another member's contact by id, then not found is returned.
- Given a merge, when undone inside the window, then both contacts and their links are restored.

## Open questions
Retention period for soft-deleted contacts; which fields count as PII for exports.

## Column picker, bulk actions and the tasks page
The contacts table lets each viewer choose its columns (kept in browser storage). Ticked contacts take one action at once: add or remove a tag, assign, unassign, or delete (needs the delete permission). Contacts the member cannot see are counted as skipped. A request may name at most `CRM_CONTACTS_BULK_LIMIT` contacts (default 200). `/crm/tasks` lists tasks across contacts by scope.

## Appointments
An appointment belongs to one contact and may be about one of their deals. It has a title, a start and end, a place or link, notes, an assignee and a status (scheduled, completed, no-show, cancelled). Booking and status changes publish `crm.appointment.booked` and `crm.appointment.status_changed` and appear on the contact timeline. The deal drawer and the contact page each have an Appointments tab. Not built: a calendar page, availability, booking links and reminders.
