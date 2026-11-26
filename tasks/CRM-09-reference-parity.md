# CRM-9 Reference parity (Conversations, Contacts, Opportunities, Custom fields)

Goal: match or beat the reference CRM screenshots in look and features. English only (ADR 0002).
Each stage is its own change of at most about 400 lines, with tests (including cross-tenant) and gates green.
Module docs: `docs/product/03-contacts.md`, `04-pipelines.md`, `05-inbox.md`, `02-records-and-fields.md`.

## Audit result (code read; the dev stack was not running, so no visual comparison yet)
Already built: pipelines board/list/insights, deal drawer, filters, saved views, bulk actions, tasks on deals,
inbox with tabs (all, unread, starred, mine, unassigned), contacts, companies, smart lists, custom fields with folders (API),
notes and tasks on contacts, billing models.

Gaps found against the screenshots:
1. Inbox: no contact details side panel (owner, followers, tags, searchable field list); no select-all and bulk actions; no "recent" tab.
2. Deal drawer: no tabbed layout (details, appointment, tasks, notes, payments, associated objects); notes on deals missing;
   no appointments at all (no model, no API); no payments tab; deal has no company or additional contacts link.
3. Contacts list: no column picker, bulk-actions page, tasks page, companies tab parity; check smart list UI.
4. Custom fields: no settings UI with object tabs, folders, field key display, copy key, create folder.

## Stages
- [x] A. Deal drawer tabs (already tabbed: details, tasks, notes, history); notes now belong to a deal (`Note.opportunity`, `opportunity_id` on the notes API, filter on list). Appointment, payments and associated-objects tabs arrive with stages B, C and D.
- [ ] B. Associated objects: company and extra contacts on a deal (privacy review first).
- [x] C. Appointments: `Appointment` model (own contact, optional deal, status scheduled, completed, no_show, cancelled), API (`/crm/appointments`, `/crm/contacts/{id}/appointments`), events and timeline entries, privacy export, and an Appointments tab on the deal drawer and the contact page. A calendar page, availability and reminders are not built.
- [x] D. Payments tab on deals. The billing module only holds the platform invoices of the organization, so this is a manual ledger: `Payment` (payment or refund, paid, pending or failed, method, reference), totals per deal, refunds capped by what was received, API under `/crm/opportunities/{id}/payments` and `/crm/payments/{id}`. Taking money online (checkout, invoices, payment links) needs a payment provider and is not built.
- [x] E. Inbox contact side panel (owner, followers, tags, searchable fields), select-all with bulk actions (`POST /crm/inbox/conversations/bulk`, registry in `crm/inbox/bulk.py`), recent tab (`CRM_INBOX_RECENT_DAYS`, `CRM_INBOX_BULK_LIMIT`).
- [x] F. Contacts list: column picker (per viewer, browser storage), select-all with bulk actions (`POST /crm/contacts/bulk`, registry in `crm/contacts/bulk.py`: add and remove tags, assign, unassign, delete), and a Tasks page (`/crm/tasks`, scopes mine, today, overdue, open, all; task list now carries `contact_name`). Setting `CRM_CONTACTS_BULK_LIMIT`.
- [x] G. Custom fields settings page (`/crm/fields`): tabs per record kind with counts, fields and folders sections, search and filters, create and edit (key and type fixed), copy merge tag, archive, folder rename (`POST /crm/fields/folders/rename`) and `created_at` on fields.
- [ ] H. Visual pass at 1440 and phone widths against the screenshots.
