# GoHighLevel CRM & Omnichannel Messaging — Reverse-Engineering Specification

Nov 17, 2026 · @Ahmad

## 1. Executive summary

GoHighLevel (GHL, white-labelled as LeadConnector) is a multi-tenant CRM whose real product is one contact record with every channel attached to it. SMS, voice, email, WhatsApp, Facebook Messenger, Instagram DM, social comments, web chat and reviews all land in a single Conversations inbox, all write to the same contact timeline, and all can trigger or be sent from the same workflow engine.

This specification documents that CRM core and its channel integrations in enough detail to rebuild them. Funnels, websites, courses, communities, payments and the social planner are out of scope except where they feed the CRM.

**The five ideas to copy (and improve):**

1. **Contact-centric, channel-agnostic model.** Every message, call, form, appointment and payment is an event on a contact. Channels are adapters, not silos.
2. **One inbox, one thread per contact.** A contact has one conversation; messages from all channels interleave in it, each tagged with its channel type.
3. **Event-driven automation.** Every CRM change and every inbound or outbound message emits an event; workflows subscribe to events (triggers) and call actions.
4. **Hierarchical multi-tenancy.** Agency (reseller) → sub-account (client business) → users. Billing, phone numbers, email domains and WhatsApp numbers are scoped to the sub-account.
5. **Rebilling of usage.** Messaging is resold at cost-plus: the agency sets markup on SMS, email, WhatsApp and AI usage and the platform meters it per sub-account via a wallet.

**How to read this spec.** Sections 2–5 cover the CRM data layer. Sections 6–10 cover the inbox and each channel. Sections 11–14 cover automation, compliance, APIs and reporting. Sections 15–16 translate everything into a build plan for your own product. Each feature is described as *what it does*, *how it behaves*, and *what you need to build it*.

**Companion tabs:** Automations (workflow engine), Gap analysis & monday CRM (what a complete CRM still needs, compared with monday CRM) and Security (security requirements).

**Confidence note.** Behaviour is drawn from HighLevel's help centre, developer docs, changelog and hands-on knowledge of the product. GHL ships weekly, so exact menu names, limits and prices drift; treat any number as indicative and re-check before relying on it.

## 2. Platform architecture: tenancy, users and permissions

Every CRM record in GHL belongs to exactly one **sub-account** (called `location` in the API); the sub-account is the hard tenant boundary for data, channels and billing.

### 2.1 Tenancy hierarchy

| Level | What it is | Owns |
| --- | --- | --- |
| Platform | GHL itself | Infrastructure, carrier/Meta/Mailgun contracts, marketplace |
| Agency (company) | The reseller or the business owner's master account | Branding (white-label domain, logo, app), SaaS plans, rebilling markups, snapshots, agency users, wallet top-ups |
| Sub-account (location) | One client business | Contacts, companies, opportunities, custom objects, conversations, phone numbers, email domains, WhatsApp number, Facebook/Instagram connections, workflows, calendars |
| User | A person who logs in | Role, permissions, assigned sub-accounts, own email/calendar sync |

**Snapshots** are templates of a sub-account (pipelines, custom fields, workflows, templates, funnels, calendars) that an agency can push into new or existing sub-accounts. In your build, a snapshot is a serialised export of configuration objects with ID remapping on import.

### 2.2 Users and roles

- **User types:** Agency user (can see many sub-accounts) and Account user (scoped to one or more sub-accounts).
- **Roles:** Admin or User at each level.
- **Granular permissions** (toggles per module): contacts, conversations, opportunities, campaigns/workflows, settings, phone, marketing, reporting, payments, etc.
- **Data-scope restriction:** “Only assigned data” limits a user to contacts, conversations and opportunities assigned to them. This is enforced on every list query, search, smart list, export and API call.
- **Per-user resources:** a user can own a phone number (user-level number), a connected Gmail/Outlook mailbox, a calendar and an email signature.

### 2.3 What you need to build

- A `tenant_id` (sub-account) column on every business table, with row-level security enforced in the data layer, not just the UI.
- A separate `agency_id` above it for reseller features, billing and white-labelling.
- A permission model with both **module permissions** (can the user open Conversations?) and **record scope** (which records can they see?).
- An audit log of who changed what (GHL has audit logs for contacts, opportunities and custom objects; you should have them everywhere).

## 3. CRM data model

GHL's CRM has three standard objects — **Contacts, Companies, Opportunities** — plus user-defined **Custom Objects**, all connected by labelled **Associations**. The contact is the hub: conversations, appointments, tasks, notes, payments and workflow history all hang off it.

### 3.1 Contact

The contact is a person (or lead) inside one sub-account. Identity is resolved primarily by **email** and **phone** (E.164).

| Field group | Fields |
| --- | --- |
| Identity | first name, last name, full name (derived), email, phone, additional emails\[\], additional phones\[\], date of birth, profile photo |
| Business | company name (free text), linked Company record(s), website |
| Address | address line, city, state, postal code, country, timezone |
| Ownership | assigned user (owner), followers\[\] (users who get notified) |
| Classification | type (lead / customer), tags\[\], source |
| Consent | global DND flag + per-channel DND: SMS, Email, Calls & Voicemail, WhatsApp, Google Business Messages, Facebook/Instagram; inbound DND option; per-channel DND reason and timestamp |
| Attribution | first attribution and latest attribution: session source, medium, UTM source/medium/campaign/term/content, landing URL, referrer, gclid, fbclid, ad/campaign IDs |
| System | id, sub-account id, created at, updated at, last activity at, date added, created by (user, import, API, form, integration) |
| Custom | any number of custom fields (3.2) |

**Duplicate handling.** A sub-account setting controls whether duplicates are allowed. When not allowed, inbound data (forms, API, imports, inbound messages) is matched to an existing contact by a configurable priority — email first or phone first — and merged instead of created. Manual merge of two contacts is also supported, with field-by-field selection of which value wins.

### 3.2 Custom fields and custom values

**Custom fields** are per-record attributes defined per sub-account and per object (contact, opportunity, company, custom object). Each has a name, a unique **key** used in merge tags (`{{contact.lead_score}}`), a folder/group, a placeholder and a type:

| Type | Notes |
| --- | --- |
| Single line / multi-line text | Free text |
| Text box list | Several labelled text inputs in one field |
| Number, Monetary | Numeric; monetary carries currency |
| Phone, Email | Validated formats |
| Dropdown (single), Dropdown (multiple), Radio, Checkbox | Fixed option lists |
| Date picker | Date value, usable in date-based workflow triggers |
| File upload | Stored in media storage, linked by URL |
| Signature | Captured from forms |

**Custom values** are different: they are sub-account-level constants (business name, booking link, support phone) used as merge tags (`{{custom_values.booking_link}}`) inside templates and workflows so snapshots stay reusable.

### 3.3 Tags

Tags are free-form strings on a contact. They are the primary segmentation and automation primitive: “tag added” and “tag removed” are workflow triggers, and smart lists, bulk actions and campaigns filter by tags. Build them as a tenant-scoped `tags` table plus a `contact_tags` join, so renames and usage counts are cheap.

### 3.4 Company

A business record with name, phone, email, website, address, description, owner and custom fields. Contacts link to companies, and since 2026 [companies can also be associated with opportunities and custom objects](https://leveluppipeline.freshdesk.com/support/solutions/articles/153000254801-associate-companies-with-custom-objects-opportunities), shown in record views, Kanban cards and list views.

### 3.5 Opportunity

A deal tied to one primary contact and placed in one pipeline stage. Fields: name, pipeline, stage, status (Open, Won, Lost, Abandoned), monetary value, owner, source, lost reason, followers, custom fields, created/updated/last-stage-change timestamps. A contact can have many opportunities; a setting controls whether more than one open opportunity per contact per pipeline is allowed. Details in section 5.

### 3.6 Custom objects

[Custom objects let you define new record types such as Properties, Vehicles, Policies or Patients](https://gohighlevelassist.freshdesk.com/support/solutions/articles/155000006631-custom-objects-in-all-plans-higher-limit). Each has a singular/plural name, a primary display field, its own custom fields, list and record views, import, smart-list filtering and workflow support (record created/updated triggers, create/update/find records actions). Changes are audit-logged.

### 3.7 Associations

Associations are typed, labelled relationships between any two records:

- **Contact ↔ Contact** with custom labels (spouse, employee, referrer).
- **Contact / Company / Opportunity ↔ Custom object.**
- **Company ↔ Opportunity.**
- Cardinality: one-to-one, one-to-many and many-to-many, with a label for each direction.
- Workflows can **associate records** automatically and **find related records** to act on them.

### 3.8 Records attached to a contact

| Record | Purpose |
| --- | --- |
| Conversation + messages | All channel messages and calls (section 6) |
| Notes | Free text, author, timestamp, pinned |
| Tasks | Title, description, due date, assignee, status, linked contact/opportunity |
| Appointments | Calendar bookings with status (confirmed, showed, no-show, cancelled) |
| Documents & contracts | Proposals and e-signatures |
| Payments | Transactions, invoices, subscriptions, orders |
| Workflow & campaign history | Which automations the contact is in, at which step |
| Activity timeline | Chronological feed of everything above |
| Form / survey submissions, page visits | Captured with attribution |

### 3.9 Data model you should implement

Use a hybrid relational model: fixed columns for standard fields, and custom fields stored as typed values (`custom_field_values(object_type, record_id, field_id, value_text, value_number, value_date, value_json)`) or as a JSONB column with GIN indexes. Associations go in one generic `associations(from_type, from_id, to_type, to_id, label_id)` table so custom objects get relationships for free.

## 4. Contact management UX

The Contacts module is a filterable, bulk-actionable table backed by saved segments (Smart Lists) and a rich single-contact page that doubles as a messaging console.

### 4.1 Contact list and Smart Lists

- **Table view** with configurable, reorderable columns (any standard or custom field), sort, pagination and page-size options.
- **Quick search** across name, email, phone, company and tags.
- **Advanced filters** combining conditions with AND/OR groups. Filter on any field, tags (has/doesn't have), DND status, created date, last activity, assigned user, opportunity status/pipeline/stage, appointment status, workflow membership, form/survey submitted, email events (opened, clicked, bounced), attribution, custom object relationships.
- **Smart Lists** are saved filters with saved columns. They are dynamic (membership recomputed on query), can be private or shared with users, and serve as the audience for bulk actions.
- **Restricted views** honour the “only assigned data” permission.

### 4.2 Bulk actions (on selection or entire smart list)

| Action | Notes |
| --- | --- |
| Add / remove tags | Fires tag triggers |
| Add to / remove from workflow | Enrol with optional drip (batch size + interval) |
| Send SMS / email / WhatsApp template | Bulk send with scheduling and batch drip |
| Create / update opportunity | Choose pipeline and stage |
| Assign owner | Round-robin or fixed user |
| Change DND | Per channel |
| Merge, delete, export (CSV) | Delete is soft with restore window in newer versions |
| Send review request | Reputation module |
| Import into a campaign / list | Legacy campaigns |

Bulk jobs run asynchronously and appear in a **Bulk Actions** log with progress, per-contact success/failure, and the ability to cancel or roll back where possible. Build every bulk operation as a background job with idempotent per-record steps.

### 4.3 Import and export

- CSV import wizard: upload, map columns to standard/custom fields, choose to create new or update existing (match on email/phone/ID), add tags to all imported rows, optionally enrol in a workflow, set DND for the batch, and review an error file afterwards.
- Import works for contacts, opportunities, companies and custom objects.
- Export of any smart list to CSV with the selected columns.

### 4.4 The contact record page

Three-pane layout:

1. **Left: details panel.** All fields grouped into collapsible sections (contact, general info, additional info, custom field folders), DND toggles per channel, tags editor, owner and followers, attribution, active workflows (with add/remove), opportunities, associated records, and the contact's companies.
2. **Centre: conversation.** The same thread as the inbox (section 6) with a composer that switches channel (SMS, WhatsApp, Email, FB, IG, GMB, Live Chat, Internal Comment) and a click-to-call button.
3. **Right: activity tabs.** Activity timeline, notes, tasks, appointments, documents, payments, and an AI summary of the contact.

Other essentials: inline editing with field validation, edit history, quick actions (call, book appointment, send invoice, add to workflow), keyboard navigation between records in a smart list, and real-time updates when another user or automation changes the record.

## 5. Pipelines, opportunities, tasks and the activity timeline

Pipelines turn contacts into revenue tracking: each opportunity sits in exactly one stage, and every stage move is an automation event.

### 5.1 Pipelines

- Unlimited pipelines per sub-account, each with an ordered list of named stages.
- Per-stage settings: show in funnel chart, show in pie chart, and stage probability (for weighted forecasting).
- Pipeline-level visibility can be restricted to certain users.

### 5.2 Opportunity views

- **Kanban board:** columns per stage with count and total value; drag-and-drop to change stage; cards show contact, value, owner, tags, next task/appointment and associated records; quick actions on the card (call, SMS, email, add task, add note).
- **List view:** sortable/filterable table with bulk actions (move stage, change status, assign owner, delete, export).
- **Filters:** status, owner, value range, date created, last stage change, source, tags, custom fields, “stale” (no activity in X days).
- **Opportunity detail:** its own fields, the primary contact's details and conversation, notes, tasks, appointments, associated companies and custom objects.

### 5.3 Status and stage rules

- Status (Open/Won/Lost/Abandoned) is independent of stage, so a deal can be Lost while sitting in “Proposal sent”.
- Lost requires an optional lost reason from a managed list.
- Every change records `last_stage_change_at` and `last_status_change_at`; these drive stale-deal filters and reports.
- Workflow triggers: *Opportunity created*, *Pipeline stage changed*, *Opportunity status changed*, *Opportunity changed* (any field), *Stale opportunities*.
- Workflow actions: *Create or update opportunity*, *Remove opportunity*, assign owner.

### 5.4 Tasks

Tasks have title, description, due date/time, assignee, status (incomplete/complete) and a link to a contact (and optionally an opportunity). Users see a task list with filters (mine, overdue, due today), get reminders, and workflows can create tasks (“call this lead within 15 minutes”). Trigger: *Task reminder*, *Task completed*.

### 5.5 Notes

Rich-text notes with author and timestamp, editable, pinnable, @mentions of users (notifies them), visible on the contact and opportunity. Workflows can add notes. Trigger: *Note added / changed*.

### 5.6 Activity timeline and audit log

The contact timeline merges: field changes, tag changes, opportunity events, appointments, form/survey submissions, page views, emails opened/clicked, calls, payments, workflow enrolments and step executions, and notes. The audit log records actor (user, workflow, API key, integration), before/after values and timestamp. Build this as an append-only `activity_events` table written by the same event bus that feeds automations (section 15).

## 6. The unified Conversations inbox

One contact has one conversation, and every channel's messages interleave in it chronologically, each stamped with its channel type, direction and delivery status. This is the single most important design decision to copy.

### 6.1 Channels that land in the inbox

| Channel | Message type in API | Inbound | Outbound |
| --- | --- | --- | --- |
| SMS / MMS | `TYPE_SMS` | Yes | Yes |
| Phone call, voicemail | `TYPE_CALL` | Yes (recording, transcript) | Yes (click-to-call) |
| Email | `TYPE_EMAIL` | Yes (reply tracking / 2-way sync) | Yes |
| WhatsApp | `TYPE_WHATSAPP` | Yes | Yes (templates outside 24h) |
| Facebook Messenger | `TYPE_FACEBOOK` | Yes | Yes (24h rule + tags) |
| Instagram DM | `TYPE_INSTAGRAM` | Yes | Yes (24h rule) |
| FB / IG comments | comment types | Yes | Reply publicly or privately |
| Google Business messages / reviews | `TYPE_GMB`, `TYPE_REVIEW` | Yes | Yes |
| Website live chat widget | `TYPE_LIVE_CHAT` | Yes | Yes |
| Custom provider (your own SMS/email/chat) | `TYPE_CUSTOM_*` | Via API | Via webhook |
| Internal comment | internal | — | Team-only, with @mentions |
| Activity entries | `TYPE_ACTIVITY_*` | System | System (opportunity, appointment events) |

### 6.2 Inbox list (left pane)

- **Tabs:** Unread, Recents, Starred, All; plus a **Manual Actions** queue where workflow-created “manual SMS” and “manual call” tasks wait for a human.
- **Filters:** channel, assigned to (me / unassigned / specific user / all), status, tags, date range; sort newest or oldest first.
- **Search** by contact name, phone, email or message text.
- **Row content:** avatar, name, channel icon of the last message, preview, relative time, unread badge, assignee.
- **Row actions:** mark read/unread, star, assign, delete, block/spam.

### 6.3 Thread view (centre pane)

- Bubbles grouped by day, with channel icon, sender (user name or “workflow”/“bot”), timestamp and **delivery status**: pending → sent → delivered → read, or failed/undelivered with the carrier/Meta error code and a retry button.
- Emails render as cards with subject, from/to/cc, collapsed quoted text, attachments and open/click indicators.
- Calls show direction, duration, recording player, transcript and AI call summary; voicemails show the audio.
- Attachments preview inline (images, video, PDF, audio).
- Internal comments appear in a distinct colour and are never sent to the contact.

### 6.4 Composer

- **Channel switcher** defaulting to the channel of the last inbound message.
- **From selector:** which phone number, email address, WhatsApp number or Facebook page to send from.
- **Templates and snippets** with merge fields (`{{contact.first_name}}`, custom values), saved per sub-account; WhatsApp switches to the approved-template picker when the 24-hour window is closed.
- Email extras: subject, CC/BCC, rich-text editor, signature, attachments, reply-in-thread.
- Emoji, attachments from device or media library, **schedule send**, character/segment counter for SMS.
- **AI assist:** suggested replies, rewrite/translate/shorten, and Conversation AI bot in suggestive (drafts) or auto-pilot mode.
- Quick actions: call, book appointment, send payment link or invoice, send review request, add to workflow.

### 6.5 Assignment and collaboration

- Conversations follow the contact's assigned user; reassigning either updates both.
- Workflows assign via round-robin (with optional “only available users”) or fixed user.
- @mentions in internal comments notify teammates.
- Notifications: in-app bell, mobile push, desktop push, email digest; configurable per user (all new messages vs. only assigned).

### 6.6 Real-time behaviour you must engineer

- **Push updates** over WebSockets so new messages, status changes and assignment changes appear instantly for every viewer.
- **Unread counters** per user per conversation.
- **Idempotent ingestion:** carriers and Meta retry webhooks; dedupe on provider message ID.
- **Ordering:** sort by provider timestamp, not arrival time, and tolerate out-of-order status callbacks (a “delivered” can arrive before “sent”).
- **Contact resolution on inbound:** match by phone/email/PSID/IGSID/WhatsApp ID; create a contact if none exists; respect the duplicate-handling setting.

## 7. SMS, MMS and voice

GHL's telephony is a white-labelled Twilio layer called **LC Phone**; users can alternatively plug in their own Twilio account. Everything below is what you would build on top of a CPaaS (Twilio, Telnyx, Plivo, Vonage, or a local aggregator).

### 7.1 Phone numbers

- Buy numbers inside the app: filter by country, area code, capability (SMS, MMS, voice) and type (local, toll-free, mobile).
- Numbers belong to the sub-account; one is the **default**; others can be assigned to specific users (user-level numbers that become their caller ID and sending number).
- **Number pools** for call tracking: dynamic number insertion on websites to attribute calls to a source.
- International numbers require regulatory bundles (proof of address/identity) per country.
- Porting existing numbers in.

### 7.2 A2P 10DLC and sender registration (US)

In the US, [A2P means texts sent by software rather than a person typing on a phone, and 10DLC means a standard 10-digit local number](https://getautomized.com/?p=14700). Registration lives in the **Trust Center** under Settings → Phone System.

1. **Brand registration:** legal business name, EIN (or Sole Proprietor brand), address, website, authorised contact with a business-domain email, OTP verification for sole proprietors.
2. **Campaign registration:** use case (marketing, customer care, mixed, 2FA…), description, 2–5 sample messages, opt-in method with proof (form URL with consent language), opt-in/opt-out/help keywords and messages, whether links/phone numbers are included.
3. Submission flows to The Campaign Registry and carriers; [carriers make the final decision and HighLevel cannot override it](https://getautomized.com/?p=14700).
4. [Once the brand is approved, HighLevel automatically submits the campaign, and registration fees are passed through with no HighLevel markup](https://gohighlevelassist.freshdesk.com/support/solutions/articles/155000004539-a2p-campaign-registration-step-by-step-guide-and-faqs).
5. Toll-free numbers need a separate **toll-free verification**.

Common rejection reasons to validate *before* submission in your own product: personal Gmail contact address, website domain not matching the brand, missing privacy policy or SMS consent language, sample messages without brand name or opt-out text.

### 7.3 Sending SMS/MMS

- **Segments:** GSM-7 messages split at 160 characters (153 per part when concatenated); any Unicode character (emoji, Persian, Arabic) switches to UCS-2 at 70 (67 per part). Show a live segment counter and price estimate.
- **MMS:** images, GIF, short video, vCard, PDF (carrier-dependent size limits).
- **Sending limits** per number per second and per day set by the 10DLC trust score; queue and throttle outbound traffic per number.
- **Delivery status callbacks:** queued, sent, delivered, undelivered, failed with error codes (e.g. 30003 unreachable, 30005 unknown number, 30006 landline, 30007 carrier filtering, 21610 recipient unsubscribed).
- **Missed-call text-back:** auto-SMS when an inbound call is missed.

### 7.4 Opt-out handling

- Inbound **STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT** sets the contact's SMS DND and sends the opt-out confirmation.
- **START / UNSTOP** re-subscribes; **HELP** returns the help message.
- Outbound SMS to a DND contact is blocked at send time with a visible error, including from workflows.

### 7.5 Voice

| Capability | Behaviour |
| --- | --- |
| Inbound routing | Ring users simultaneously or round-robin, forward to a mobile number, business hours, timeout to voicemail |
| IVR | Built in the workflow builder: menus, gather digits/speech, route to user or voicemail |
| Call connect / whisper | User hears “press 1 to accept a call from Facebook Ads” before connecting |
| Recording | Per number; disclosure message option |
| Transcription & AI summary | Post-call, attached to the conversation |
| Outbound dialer | Browser (WebRTC) and mobile app; caller ID = user or default number; call notes and dispositions |
| Voicemail drop | Pre-recorded voicemail via workflow |
| Voice AI agent | AI answers inbound calls, books appointments, updates fields |
| Call tracking | Number pools, source attribution, call reports |

### 7.6 Billing model

SMS segments, MMS, call minutes, recordings, transcription and number rentals are metered per sub-account and drawn from a prepaid **wallet** with auto-recharge; agencies can rebill at a markup. Build a `usage_records` ledger written at send/callback time with cost and price per unit.

## 8. Email

GHL treats email as two things on one pipe: **1:1 conversational email** in the inbox and **bulk marketing email** (campaigns and workflow sends). Both use the same sending service, domain setup, tracking and unsubscribe logic.

### 8.1 Sending infrastructure options

| Option | How it works | When used |
| --- | --- | --- |
| LC Email, shared domain | Platform-owned sending domain and IP pool | Default for new accounts; weakest deliverability |
| LC Email, dedicated sending domain | Customer adds a subdomain (e.g. `mail.brand.com`) and DNS records | Recommended; brand-aligned SPF/DKIM |
| Dedicated IP | Isolated IP with warm-up | High volume senders |
| Third-party SMTP | Mailgun, SendGrid, Amazon SES or any SMTP | Customers who already own a provider |
| Gmail / Outlook sync | OAuth mailbox per user | 1:1 email from the rep's real mailbox with two-way sync |

Setup path: [Settings → Email Services → Dedicated Domain and IP → Add Domain, enter a subdomain, then add the DNS records shown](https://gohighlevelassist.freshdesk.com/support/solutions/articles/155000005058-getting-started-setup-email-phone-and-sms). [New domains should be warmed up before emailing large lists](https://gohighlevelassist.freshdesk.com/support/solutions/articles/48001226115-dedicated-email-sending-domains-overview-setup).

### 8.2 DNS records your product must generate and verify

- **SPF** (TXT) including your ESP's sending servers.
- **DKIM** (TXT or CNAME) with a per-domain key.
- **Return-path / bounce** CNAME for SPF alignment.
- **Tracking** CNAME for branded open/click links.
- **MX** on the subdomain for inbound replies/bounces.
- **DMARC** check and recommendation (at least `p=none` with reporting).
- A **verify** button that resolves DNS live and shows each record's status.

### 8.3 Two-way conversational email

- Outbound from the inbox or workflows is threaded with `Message-ID`, `In-Reply-To` and `References` headers.
- **Reply tracking:** replies come back either through the synced Gmail/Outlook mailbox or through an inbound-parse route on the sending domain, then attach to the right contact and thread.
- Inbound mail parsing: strip quoted history for previews, keep full HTML, store attachments, detect auto-replies and bounces so they don't trigger “customer replied”.
- From name/address per user or per sub-account; signatures per user.

### 8.4 Bulk email and templates

- **Email builder:** drag-and-drop blocks (text, image, button, columns, video thumbnail, social, divider, product, countdown), HTML mode, saved templates and reusable sections, merge fields, mobile preview, dark-mode preview, test send.
- **Campaigns:** audience = smart list, tags or whole database; schedule or send now; **batch sending** (X emails every Y minutes); timezone-aware send; **A/B testing** on subject/content with winner selection; resend to non-openers.
- **Statistics** per campaign and per template: delivered, opened, clicked (per link), replied, bounced (hard/soft), complained, unsubscribed.

### 8.5 Compliance and deliverability features

- Mandatory unsubscribe link with **one-click List-Unsubscribe** headers (required by Gmail/Yahoo bulk-sender rules since 2024). Unsubscribe sets Email DND.
- Automatic suppression of hard bounces and spam complainers.
- **Email verification** (optional paid service) to validate addresses before sending.
- Sending-reputation dashboard: bounce and complaint rates with warnings and automatic pausing when thresholds are crossed.

### 8.6 Automation hooks

Triggers: *Email events* (delivered, opened, clicked, bounced, unsubscribed, complained), *Customer replied* (channel = email). Actions: *Send email* (template or inline, from user/owner), *Send internal notification email*, wait for email event (“wait until opened, max 2 days”).

## 9. WhatsApp

GHL's native WhatsApp runs on the **official WhatsApp Business Platform (Meta Cloud API)**, with GHL acting as a Meta Tech Provider/BSP. [It is a paid add-on of $10 per month per WhatsApp-enabled sub-account, with one official WhatsApp Business Account (WABA) per location and message charges on top](https://timelines.ai/gohighlevel-whatsapp-pricing).

### 9.1 Onboarding (Embedded Signup)

1. User clicks *Connect WhatsApp* in the sub-account and goes through Meta's Embedded Signup popup.
2. Selects or creates a Meta Business portfolio, creates a WABA, enters business display name, category and the phone number.
3. Verifies the number by SMS/voice OTP; Meta reviews the display name.
4. The platform receives the WABA ID, phone number ID and a system-user token, subscribes the app to the WABA's webhooks, and registers the number.
5. Status shown in-app: number status, display-name approval, **quality rating** (green/yellow/red) and **messaging limit tier** (how many unique customers can be messaged with business-initiated templates per 24h; it rises with good quality and volume).

Build notes: a number active on the consumer/Business app must be migrated, unless you support Meta's **coexistence** mode (Business app and Cloud API on one number). Plan for Meta business verification, which unlocks higher limits.

### 9.2 The 24-hour customer service window

- [The window starts every time the customer sends an inbound message; inside it, businesses can send unlimited free-form messages](https://highlevel.canny.io/changelog/whatsapp-message-rate-card-updates).
- Outside the window, only **approved templates** can be sent. [In GHL, unless the customer has messaged in the last 24 hours, the composer prompts for a template and stays template-only until they reply](https://gohighlevelassist.freshdesk.com/support/solutions/articles/155000001980-whatsapp-overview-sub-account-set-up-and-best-practices).
- **Free entry point:** when a customer messages from a Click-to-WhatsApp ad or a Facebook Page CTA and you respond within 24h, a 72-hour window opens in which messages are free.
- Your composer and workflow engine must compute the window per contact per number (`last_inbound_at + 24h`) and block or auto-switch to template mode.

### 9.3 Templates

| Part | Options |
| --- | --- |
| Category | Marketing, Utility, Authentication (Meta can re-categorise on review) |
| Language | One template per language code |
| Header | None, text (1 variable), image, video, document, location |
| Body | Text up to 1,024 chars with positional `{{1}}` or named variables |
| Footer | Short static text (e.g. “Reply STOP to opt out”) |
| Buttons | Quick replies, URL (static or dynamic suffix), call phone, copy code, flows |
| Status | Draft → Pending → Approved / Rejected / Paused / Disabled |

In the product: create templates via the Graph API, map each variable to a contact field or custom value, show sample values, sync status changes from Meta webhooks, and show rejection reasons.

### 9.4 Pricing model to meter

- [Since July 1, 2025, HighLevel's WhatsApp billing uses a delivered-message model](https://timelines.ai/gohighlevel-whatsapp-pricing), matching Meta's per-message pricing for templates.
- Service (free-form) replies inside the window are free; utility templates inside an open window are free; marketing and authentication templates are charged per delivered message at a country-specific rate.
- Meta revises rate cards periodically, so store rates in a versioned table keyed by country and category, and price each message at the delivered callback.

### 9.5 Messaging features

- Outbound: text, templates, images, video, audio, documents, location, contacts; [WhatsApp in GHL supports all file formats allowed by the Business API, e.g. images up to 5 MB and videos up to 16 MB](https://highlevel.canny.io/changelog/new-rich-whatsapp-media-messaging-in-conversations-labs), and attachments from Media Storage.
- Inbound: text, media, voice notes, location, contacts, button/list replies, reactions, replies-to-message context.
- Status: sent, delivered, **read** (blue ticks), failed with Meta error codes (e.g. re-engagement required, template paused, number not on WhatsApp).
- Entry points: wa.me links, QR code and website chat widget button.

### 9.6 Automation hooks

- Triggers: *Customer replied* (channel = WhatsApp), keyword match on inbound text, button reply payload.
- Actions: *Send WhatsApp* (template with variable mapping, or free-form if the window is open), *Update WhatsApp DND*, wait for reply with timeout.
- Bulk: send a template to a smart list with drip batching, respecting the tier limit.

### 9.7 Official vs. unofficial connectors

Marketplace apps also connect WhatsApp through a QR-scanned personal/Business-app session (WhatsApp Web emulation). They avoid template rules and per-message fees but violate WhatsApp's terms and risk number bans. For your product, make the official Cloud API the default and treat unofficial connectors as a risky optional plugin, if at all.

## 10. Meta: Facebook Messenger, Instagram, comments, Lead Ads and Conversions API

One Facebook OAuth connection per sub-account unlocks five capabilities: Messenger DMs, Instagram DMs, post comments, Lead Ads ingestion and ad-side reporting/conversion feedback. [HighLevel's ideal loop is ad → lead capture → instant response → qualification → nurture → appointment → sale → CRM feedback to Meta](https://oneppcagency.co.uk/crm-ads/gohighlevel-the-ultimate-facebook-ads-tool-for-lead-gen/).

### 10.1 Connection and permissions

- User logs in with Facebook, selects the Business, Pages and linked Instagram professional accounts, and grants scopes. Typical scopes: `pages_show_list`, `pages_messaging`, `pages_manage_metadata`, `pages_read_engagement`, `pages_manage_engagement`, `leads_retrieval`, `instagram_basic`, `instagram_manage_messages`, `instagram_manage_comments`, `ads_read` / `ads_management`, `business_management`.
- Your Meta app needs **App Review** and **Business Verification** for each advanced permission; plan weeks for this.
- [Multiple Facebook Pages can be connected to one sub-account](https://ideas.gohighlevel.com/changelog/facebook-multi-page-support-for-sub-accounts) for conversations, lead ads and reviews.
- Store long-lived Page tokens; subscribe each Page to webhook fields (`messages`, `messaging_postbacks`, `message_reads`, `message_deliveries`, `feed`, `leadgen`) and each IG account to `messages` and `comments`.
- Handle token expiry, revoked permissions and password changes with a visible “reconnect” state.

### 10.2 Facebook Messenger

- Inbound DMs create/match a contact by **Page-scoped ID (PSID)**; name and profile picture fetched from the Graph API.
- Standard **24-hour messaging window** after the user's last message; outside it, only allowed **message tags** (e.g. `HUMAN_AGENT`, which extends the window to 7 days for human replies) can be used.
- Supports text, images, video, files, quick replies, postbacks, read and delivery receipts.
- Website chat widget can hand off to Messenger.

### 10.3 Instagram Direct

- Requires an Instagram professional (Business/Creator) account and [access to messages enabled in Instagram settings; DMs then appear in Conversations, can trigger automations, and replies follow the 24-hour window](https://netpartners.marketing/social-commerce-messaging-automation-connect-shop-leads-to-gohighlevel-then-automate-sms-chat-ai-cart-recovery-and-upsells.md).
- Contact matched by **Instagram-scoped ID (IGSID)**; handles story replies, story mentions, shared posts, media and reactions.

### 10.4 Comments and comment-to-DM

- New comments on Facebook Page and Instagram posts flow into the inbox (or a social tab) and can be replied to publicly or by a **private reply** DM.
- Workflow trigger *Facebook/Instagram comment on post* with filters (specific post, keyword contains) → action *Reply in comments* and *Send DM*: the [comment-to-lead feature sends a personalised DM and captures the lead into the CRM](https://www.gohighlevel.com/post/social-comments-leads).
- Meta rules: one private reply per comment, within 7 days.

### 10.5 Lead Ads (Instant Forms)

1. Subscribe the Page to the `leadgen` webhook.
2. On each event, fetch the lead by `leadgen_id` with `leads_retrieval`.
3. Map form questions to contact standard/custom fields (mapping UI per form), create or update the contact, record source, ad, ad set, campaign and form IDs as attribution.
4. Fire trigger *Facebook Lead Form Submitted* (filter by page/form) so speed-to-lead SMS/WhatsApp/call workflows run within seconds.
5. Provide a test button that uses Meta's Lead Ads testing tool and a log of received leads with payloads and errors.

### 10.6 Conversions API (CAPI) and audiences

- Workflow action *Facebook Conversion API* sends server-side events (Lead, Schedule, Purchase, CompleteRegistration, custom) with hashed email/phone, fbc/fbp click IDs, value and currency, so Meta optimises on real CRM outcomes such as “appointment showed” or “deal won”.
- [HighLevel supports an Instagram DM event type in the CAPI workflow for Click-to-Direct ads](https://help.gohighlevel.com/support/solutions/articles/155000004717-how-to-create-a-meta-engagement-ad-with-messaging-apps).
- Workflow actions add/remove contacts to Meta **Custom Audiences** for retargeting or exclusion.

### 10.7 Click-to-message ads and ad management

- Click-to-Messenger, Click-to-Instagram and Click-to-WhatsApp ads deliver a `referral` object (ad ID, headline, `ctwa_clid`) with the first message; store it as attribution on the contact and the conversation.
- GHL's **Ad Manager** creates and reports on Facebook/Instagram campaigns from inside the app, tying ad spend to leads, opportunities and revenue in attribution reports.

## 11. Automation: workflow triggers and actions

Workflows are the glue between the CRM and the channels: a visual builder where one or more **triggers** (with filters) enrol a contact (or opportunity/custom object record), and a tree of **actions** runs with waits and branches. Below is the CRM- and messaging-relevant subset you need.

The full automation specification — every trigger, action, setting, recipe and the engine design — is in its own tab: Automations

### 11.1 Triggers

| Group | Triggers (each with filters) |
| --- | --- |
| Contact | Contact created, Contact changed (field filter), Tag added/removed, DND changed, Birthday / custom date reminder, Note added, Task added/reminder/completed, Contact engagement score |
| Messaging | Customer replied (filter by channel, contains phrase), Inbound webhook, Trigger link clicked, Email events, Call status / Call details, Missed call, Conversation AI trigger, Keyword in message |
| Meta | Facebook lead form submitted, FB/IG comment on post, Instagram DM / Messenger message received |
| Forms & pages | Form submitted, Survey submitted, Quiz submitted, Chat widget started, Funnel/page visit |
| Opportunities | Opportunity created, Stage changed, Status changed, Opportunity changed, Stale opportunity |
| Calendar | Appointment booked, status changed (confirmed/cancelled/showed/no-show), reminders |
| Payments | Order submitted, Invoice paid/overdue, Subscription events, Payment received |
| Custom objects | Record created, Record changed |
| Scheduler | Recurring schedule (run at a time for a segment) |

### 11.2 Actions

| Group | Actions |
| --- | --- |
| Communication | Send SMS, Send email, Send WhatsApp, Send Messenger / IG DM, Reply in comments, Call (connect user then contact), Voicemail drop, Manual SMS / Manual call task, Send internal notification (email, SMS, in-app, push), Send review request, Messenger/IG interactive message |
| CRM | Create/update contact, Update contact field, Add/remove tag, Assign to user (round-robin), Remove assigned user, Edit conversation (mark read/unread, archive), Add note, Add task, Set DND, Copy contact to another sub-account |
| Opportunities | Create/update opportunity, Remove opportunity |
| Custom objects | Create/update record, Find related records, Associate records |
| Flow control | If/else (any field, tag, channel, reply status), Wait (time, until date, until event like reply/email open/appointment, with timeout), Goal event (jump when goal met), Go to, Split (A/B), Drip mode, End |
| Data & code | Webhook (outbound HTTP), Custom webhook with response mapping, Custom JavaScript code, Math operations, Date/text formatters, Array functions, AI prompt (generate text into a field) |
| Meta & ads | Facebook Conversions API, Add/remove from custom audience, Google Ads conversion |
| Workflow | Add to / remove from workflow |

### 11.3 Workflow settings and runtime rules

- **Allow re-entry** (can a contact enter twice?), **allow multiple opportunities**.
- **Stop on response:** exit the workflow when the contact replies on any channel.
- **Time window / quiet hours:** only send messages on chosen days and hours in the contact's timezone; queue otherwise.
- **Default sender:** from name, email and number per workflow.
- **Conversation event handling:** mark outgoing messages as read so they don't clutter the inbox.
- **Execution log** per contact per step: time, action, result, error. **Enrolment history** and the ability to remove contacts in bulk.
- **Versioning:** draft vs. published; edits apply to future steps for contacts already in flight.

### 11.4 Engineering implications

- Treat every action as an idempotent job on a durable queue with retries and dead-letter handling.
- Waits are scheduled jobs (`run_at`) persisted in the database, not in-memory timers.
- “Wait until event” requires an index of waiting enrolments keyed by contact + event type, checked whenever that event fires.
- Enforce DND, the WhatsApp 24h window, Messenger tags and quiet hours inside the *send* service, not the workflow, so every path (inbox, bulk, API, workflow) obeys them.

## 12. Compliance and consent

Consent is stored per contact per channel and enforced at the single send gateway; GHL's per-channel DND flags are the model to copy, extended with proof of consent, which GHL records only loosely.

| Area | Rule | What your product must do |
| --- | --- | --- |
| SMS (US) | TCPA, CTIA guidelines, A2P 10DLC | Store opt-in source, timestamp, IP, form URL and consent text; honour STOP keywords instantly; block sends to DND; quiet hours |
| SMS (Canada) | CASL | Express or implied consent with expiry; identification and unsubscribe in every message |
| Email | CAN-SPAM, CASL, GDPR, Gmail/Yahoo bulk-sender rules | Physical address and unsubscribe in bulk mail; one-click List-Unsubscribe; suppress bounces/complaints; keep complaint rate under 0.3% |
| WhatsApp | Meta Business and Commerce policies | Explicit opt-in to receive WhatsApp from the business; templates outside 24h; honour opt-out; quality rating monitoring |
| Messenger / Instagram | Meta Platform policy | 24h standard window; only valid message tags outside it; no promotional content via tags |
| Calls | TCPA, recording-consent laws (one/two-party) | Recording disclosure option; DNC list checks for cold outreach; caller ID accuracy |
| Data protection | GDPR, CCPA/CPRA, PIPEDA | Data export per contact, right to erasure (hard delete across messages, files, logs), DPA, data residency options, retention policies |

Design rules:

- A `consents` table per contact per channel: status (opted-in, opted-out, unknown), source, evidence (form submission ID, message ID), timestamp, actor.
- Every opt-out — keyword, unsubscribe link, manual toggle, API — writes one consent event and emits an automation trigger.
- The send gateway checks consent, channel window and quiet hours, and records the reason whenever it blocks a message.

## 13. API, webhooks and custom conversation providers

GHL exposes the same objects through a REST API (v2) and an outbound webhook stream, and lets marketplace apps plug in as message providers; your product should be API-first in the same way so the UI, workflows and third parties share one contract.

### 13.1 API surface (v2)

- Base URL `https://services.leadconnectorhq.com`, with a required `Version` header (e.g. `2021-07-28`).
- Auth: **OAuth 2.0** marketplace apps (agency-level or sub-account-level tokens, refresh tokens, granular scopes such as `contacts.write`, `conversations/message.write`) and **Private Integration Tokens** for single-tenant scripts.
- [The v2 surface spans 41 OpenAPI 3.0 modules](https://context7.com/gohighlevel/highlevel-api-docs), including contacts, conversations, calendars, opportunities, payments, invoices, users, workflows and sub-accounts.

| Module | Key operations |
| --- | --- |
| Contacts | Create, get, update, delete, **upsert** (dedupe-aware), search with filters, tags add/remove, notes, tasks, followers, add to/remove from workflow |
| Conversations | Search conversations, get/create/update conversation, list messages, **send message** (`POST /conversations/messages`, `type` = SMS, Email, WhatsApp, IG, FB, Custom, Live\_Chat), add inbound message, add outbound call log, upload attachments, update message status, get call recording/transcript, cancel scheduled message |
| Opportunities | CRUD, search, update stage/status, list pipelines |
| Objects & associations | Custom object schemas, records CRUD, association definitions and relations |
| Custom fields / values | CRUD per object; folders |
| Users, sub-accounts | CRUD, permissions; agency creates sub-accounts and applies snapshots |
| LC Email, phone system | Email verification, number listing, A2P status |

### 13.2 Webhooks

[HighLevel publishes 50+ real-time webhook events covering contacts, conversations, appointments, opportunities, invoices, products, tasks, users and app lifecycle, with a logs dashboard that keeps payloads, response codes and retries for 30 days](https://jc-aiwiki.pages.dev/gohighlevel/webhooks). Relevant events:

- Contacts: `ContactCreate`, `ContactUpdate`, `ContactDelete`, `ContactTagUpdate`, `ContactDndUpdate`, `NoteCreate`, `TaskCreate`, `TaskComplete`.
- Conversations: [`InboundMessage`](https://marketplace.gohighlevel.com/docs/webhook/InboundMessage.md), [`OutboundMessage`](https://marketplace.gohighlevel.com/docs/webhook/OutboundMessage.md), `ProviderOutboundMessage`, `ConversationUnreadUpdate`.
- Opportunities: `OpportunityCreate`, `OpportunityStageUpdate`, `OpportunityStatusUpdate`, `OpportunityMonetaryValueUpdate`, `OpportunityAssignedToUpdate`, `OpportunityDelete`.
- Calendar: `AppointmentCreate`, `AppointmentUpdate`, `AppointmentDelete`.

An `InboundMessage` payload carries `locationId`, `contactId`, `conversationId`, `messageType` (e.g. `SMS`, `Email`, `WhatsApp`, `Call`), `direction`, `body`, `attachments[]`, `status`, `from`/`to` and channel-specific fields (email subject/threading, call duration and recording URL).

### 13.3 Custom conversation providers

A marketplace app can register as the sub-account's **SMS, email or chat provider**:

1. The provider is selected as the default for that channel in the sub-account.
2. When a user or workflow sends, GHL emits `ProviderOutboundMessage` to the app instead of using LC Phone/LC Email.
3. The app delivers through its own carrier/ESP/messaging service and calls *update message status* (delivered, failed, read).
4. Inbound traffic is pushed into the thread via *add inbound message* with the provider ID.

This is how third parties bring in WhatsApp (unofficial), Telegram, local SMS aggregators and other channels. **Copy this pattern**: a channel-adapter SDK lets you (and partners) add channels without touching the core, which matters if your market depends on providers GHL doesn't support.

### 13.4 Rate limits and reliability

- Per-app, per-sub-account burst and daily limits; return `429` with `Retry-After`.
- Webhooks: at-least-once delivery, retries with backoff, signature header for verification, event ID for dedupe.

## 14. Reporting

GHL reporting answers three questions — where leads come from, how fast the team responds, and what the pipeline is worth — and its custom dashboard builder lets users assemble widgets from these datasets.

| Report | Metrics | Data it needs |
| --- | --- | --- |
| Conversations | Messages sent/received per channel and user, **average first response time**, unread/unreplied conversations, response rate | Message timestamps, direction, user ID |
| Call reporting | Inbound/outbound calls, answered/missed, duration, first-time callers, by number/source/user | Call logs with source attribution |
| SMS / WhatsApp delivery | Sent, delivered, failed by error code, opt-outs, cost | Status callbacks, usage ledger |
| Email | Delivered, open, click, bounce, complaint, unsubscribe per campaign/template/workflow | Email events |
| Pipeline | Value and count per stage, win rate, conversion between stages, average time in stage, lost reasons, forecast (value × stage probability) | Stage history table |
| Attribution | Leads, opportunities, appointments and revenue by source, UTM, ad campaign, first vs. last touch | Contact attribution + Meta/Google ad spend |
| Appointments | Booked, confirmed, showed, no-show, by calendar/user/source | Appointment status history |
| Agent performance | Per-user messages, calls, tasks completed, deals won | All of the above keyed by user |
| Workflow | Enrolled, completed, goal met, step errors | Execution log |

Build notes: keep an analytics store (ClickHouse, BigQuery or Postgres materialised views) fed from the event stream, store stage and status histories rather than only current values, and compute first-response time at message write time (first outbound after an inbound, during business hours).

## 15. Blueprint for your own build

Build a modular monolith around one Postgres database and one event bus, with every channel behind a common adapter interface and every send passing through one gateway that enforces consent, windows and billing.

&#91;embedded content: target architecture · inbound and outbound message paths\]

Inbound traffic flows down the left side into the CRM; every outbound message, whatever its source, passes the send gateway on the right before any provider sees it.

### 15.1 Services

| Service | Responsibility |
| --- | --- |
| API gateway + auth | OAuth/JWT, API keys, tenant resolution, rate limiting, permission + record-scope checks |
| CRM core | Contacts, companies, opportunities, custom objects, fields, tags, associations, notes, tasks, import/export, dedupe/merge |
| Conversation service | Conversations, messages, unread state, assignment, templates/snippets, internal comments |
| Send gateway | Single entry for every outbound message; consent, channel window, quiet hours, throttling, cost estimate, then hands off to an adapter |
| Channel adapters | One per provider: Twilio/Telnyx SMS+voice, ESP (SES/Mailgun/SendGrid) + inbound parse, Gmail/Outlook sync, WhatsApp Cloud API, Messenger, Instagram, web chat, custom-provider SDK |
| Webhook ingress | Public endpoints for providers; signature verification, dedupe, raw payload archive, publish to bus |
| Automation engine | Trigger matching, enrolments, step execution, scheduled waits, event waits, execution logs |
| Event bus | Kafka, NATS JetStream, Redis Streams or Postgres outbox + worker; durable, ordered per contact |
| Realtime | WebSocket gateway pushing message, status and record changes to browsers and mobile |
| Billing & usage | Wallet, usage ledger, rate cards (per country/category), rebilling markup, auto-recharge |
| Search & analytics | OpenSearch/Meilisearch for contacts and messages; ClickHouse/warehouse for reports |
| Media | Object storage (S3/R2) with signed URLs, virus scan, transcoding for WhatsApp/MMS limits |

### 15.2 Core tables (minimum)

| Table | Key columns |
| --- | --- |
| `agencies`, `tenants`, `users`, `memberships` | tenant = sub-account; membership = user × tenant × role × permissions × scope |
| `contacts` | id, tenant\_id, names, email, phone\_e164, owner\_id, type, source, attribution jsonb, custom jsonb, timezone, created\_at, updated\_at, last\_activity\_at, deleted\_at |
| `contact_identities` | contact\_id, channel, external\_id (phone, email, PSID, IGSID, WA ID), channel\_account\_id — unique per tenant |
| `consents` | contact\_id, channel, status, source, evidence, created\_at |
| `tags`, `contact_tags` | tag catalogue + join |
| `field_definitions` | object\_type, key, label, type, options, folder |
| `companies`, `opportunities`, `pipelines`, `stages`, `opportunity_stage_history` | deal tracking with history |
| `object_schemas`, `object_records`, `associations`, `association_labels` | custom objects and relationships |
| `channel_accounts` | tenant\_id, channel, provider, external IDs (number, WABA, page, IG account, domain), credentials (encrypted), status |
| `conversations` | id, tenant\_id, contact\_id, assigned\_user\_id, last\_message\_at, last\_message\_preview, last\_channel, status, starred |
| `messages` | id, conversation\_id, channel, direction, channel\_account\_id, provider\_message\_id (unique), body, html, subject, attachments jsonb, status, error\_code, sent\_by (user/workflow/api/bot), template\_id, cost, meta jsonb, created\_at, provider\_ts |
| `message_status_events` | message\_id, status, provider\_ts, raw |
| `templates` | channel, name, body, variables, provider\_template\_id, category, language, approval\_status |
| `workflows`, `workflow_versions`, `enrolments`, `scheduled_jobs`, `execution_logs` | automation runtime |
| `activity_events` | append-only timeline + audit |
| `usage_records`, `wallets`, `rate_cards` | metering and billing |

### 15.3 Inbound message flow

1. Provider calls the webhook ingress; verify signature, store raw payload, dedupe on provider event/message ID, acknowledge fast (under 2 s).
2. Normalise into a common `InboundMessage` envelope (channel, channel account, sender external ID, body, media, provider timestamp, referral/ad data).
3. Resolve tenant from the channel account; resolve or create the contact via `contact_identities` and the dedupe rules.
4. Find or create the conversation, insert the message, update unread counters, the WhatsApp/Messenger window, `last_activity_at`, and handle opt-out keywords.
5. Download media to your storage (provider URLs expire).
6. Publish `message.inbound` on the bus → realtime push, notifications, automation trigger matching (*Customer replied*, keywords), “stop on response” exits, waiting-event resumes, AI agent.

### 15.4 Outbound message flow

1. Any source (inbox, bulk job, workflow, API, AI agent) calls the send gateway with contact, channel, content or template, and optional sender.
2. Gateway checks permissions, consent/DND, channel window (WhatsApp 24h, Messenger 24h/tags), quiet hours, and wallet balance; on failure it records a blocked message with the reason.
3. Renders merge fields, picks the sender (owner's number, default number, sticky sender), computes segments/cost.
4. Inserts the message as `pending` and enqueues a per-channel-account job, throttled to provider limits.
5. Adapter sends, stores the provider message ID, and marks `sent`; status callbacks later move it to delivered/read/failed and write usage records.
6. Publish `message.outbound` and `message.status` events → realtime, reports, workflow “wait for email open”.

### 15.5 Suggested stack

- Backend: TypeScript (NestJS) or Go; Postgres with row-level security; Redis for cache, locks and rate limits; a durable queue (BullMQ, Temporal, or SQS).
- **Temporal** (or a similar durable workflow engine) is worth considering for the automation engine: waits, retries and long-running enrolments come built in.
- Frontend: React/Next.js with a virtualised inbox list, optimistic sends and a WebSocket client; React Native or Flutter for the mobile app (the inbox and dialer are what users open daily).
- Providers: Twilio or Telnyx (SMS/voice, A2P), Amazon SES or Mailgun (email), Meta Cloud API directly (WhatsApp, Messenger, Instagram) — becoming a Meta Tech Provider avoids a BSP middleman.

## 16. Where to differ, and the build order

GHL wins on breadth, not depth; your opening is to match its CRM-plus-inbox core and beat it on clarity, inbox workflow, localisation and the channels your market actually uses.

### 16.1 Differentiation opportunities

| GHL weakness | Your alternative |
| --- | --- |
| Dense, cluttered UI with a steep learning curve | Opinionated defaults, guided setup per industry, progressive disclosure of advanced settings |
| Inbox lacks real help-desk states | Open / pending / snoozed / closed statuses, SLAs, team inboxes, collision detection (“Ali is replying”), canned responses with approval |
| US-centric telephony and compliance | Local SMS aggregators via the adapter SDK, country-specific sender registration flows, multi-currency rate cards |
| Weak RTL and localisation | Full RTL UI, Persian/Arabic text handling (UCS-2 segment counting, shaping), Jalali/Hijri calendar display, localised templates |
| WhatsApp is a paid add-on with basic features | WhatsApp-first: catalogues, WhatsApp Flows, interactive lists, payments, click-to-WhatsApp attribution, coexistence support |
| Missing channels | Telegram, Viber, LINE, Apple Messages for Business, SMS from Android gateway for markets without A2P |
| Hard-to-debug automations | Visual run replay per contact, test mode with a sandbox contact, version diff, error alerts |
| Consent tracked as a flag | Consent ledger with evidence, per-purpose consent (marketing vs. transactional), exportable audit for regulators |
| Usage pricing is opaque | Live cost estimate before every bulk send, per-channel spend caps, alerts |
| AI bolted on per module | One AI agent with CRM memory working across every channel, with human hand-off and guardrails |

### 16.2 Phased build order

The roadmap diagram below lays out six phases. Two external dependencies gate the plan and take the longest: **provider accounts** (CPaaS with sender registration, an ESP with dedicated domains) before the messaging phase, and **Meta App Review + Business Verification / Tech Provider onboarding** before the WhatsApp and Meta phase. Start both on day one, in parallel with engineering.

&#91;embedded content: build roadmap · 6 phases, 2 external gates (not to scale)\]

Each phase ships something usable: after Phase 2 you already have a sellable CRM with a two-channel inbox, and WhatsApp/Meta slot in as soon as Meta's approvals land.

## 17. Glossary and sources

| Term | Meaning |
| --- | --- |
| Sub-account / location | One client business; the tenant boundary |
| Snapshot | Exportable template of a sub-account's configuration |
| Smart List | Saved dynamic contact filter used for views and bulk actions |
| Custom value | Sub-account-level constant used as a merge tag |
| DND | Do Not Disturb; per-channel opt-out flag |
| LC Phone / LC Email | GHL's white-labelled Twilio telephony and its email sending service |
| A2P 10DLC | US registration for software-sent SMS on local numbers (brand + campaign) |
| TCR | The Campaign Registry, which receives 10DLC brand and campaign registrations |
| WABA | WhatsApp Business Account |
| Customer service window | 24h after a customer's last WhatsApp/Messenger message when free-form replies are allowed |
| Free entry point | 72h free window after a click-to-WhatsApp ad or Page CTA conversation |
| PSID / IGSID | Page-scoped (Messenger) and Instagram-scoped user IDs |
| CAPI | Meta Conversions API (server-side conversion events) |
| Conversation provider | Marketplace app that replaces the built-in SMS/email/chat transport |

### Sources

- [HighLevel — Getting Started: Setup Email, Phone and SMS](https://gohighlevelassist.freshdesk.com/support/solutions/articles/155000005058-getting-started-setup-email-phone-and-sms)
- [HighLevel — Dedicated Email Sending Domains](https://gohighlevelassist.freshdesk.com/support/solutions/articles/48001226115-dedicated-email-sending-domains-overview-setup)
- [HighLevel — A2P Campaign Registration guide](https://gohighlevelassist.freshdesk.com/support/solutions/articles/155000004539-a2p-campaign-registration-step-by-step-guide-and-faqs)
- [Getautomized — GoHighLevel A2P 10DLC registration flow](https://getautomized.com/?p=14700)
- [HighLevel — WhatsApp overview, setup and best practices](https://gohighlevelassist.freshdesk.com/support/solutions/articles/155000001980-whatsapp-overview-sub-account-set-up-and-best-practices)
- [HighLevel changelog — WhatsApp rate card updates](https://highlevel.canny.io/changelog/whatsapp-message-rate-card-updates)
- [HighLevel changelog — Rich WhatsApp media messaging](https://highlevel.canny.io/changelog/new-rich-whatsapp-media-messaging-in-conversations-labs)
- [TimelinesAI — GoHighLevel WhatsApp pricing](https://timelines.ai/gohighlevel-whatsapp-pricing)
- [HighLevel changelog — Facebook multi-page support](https://ideas.gohighlevel.com/changelog/facebook-multi-page-support-for-sub-accounts)
- [HighLevel — Meta engagement ads with messaging apps](https://help.gohighlevel.com/support/solutions/articles/155000004717-how-to-create-a-meta-engagement-ad-with-messaging-apps)
- [HighLevel — Facebook and Instagram comment lead feature](https://www.gohighlevel.com/post/social-comments-leads)
- [NetPartners — Social commerce messaging with GoHighLevel](https://netpartners.marketing/social-commerce-messaging-automation-connect-shop-leads-to-gohighlevel-then-automate-sms-chat-ai-cart-recovery-and-upsells.md)
- [One PPC Agency — GoHighLevel for Facebook lead gen](https://oneppcagency.co.uk/crm-ads/gohighlevel-the-ultimate-facebook-ads-tool-for-lead-gen/)
- [HighLevel — Custom Objects in all plans](https://gohighlevelassist.freshdesk.com/support/solutions/articles/155000006631-custom-objects-in-all-plans-higher-limit)
- [Associate Companies with Custom Objects & Opportunities](https://leveluppipeline.freshdesk.com/support/solutions/articles/153000254801-associate-companies-with-custom-objects-opportunities)
- [HighLevel API docs — InboundMessage webhook](https://marketplace.gohighlevel.com/docs/webhook/InboundMessage.md)
- [HighLevel API docs — OutboundMessage webhook](https://marketplace.gohighlevel.com/docs/webhook/OutboundMessage.md)
- [HighLevel API v2 documentation (Context7 index)](https://context7.com/gohighlevel/highlevel-api-docs)
- [HighLevel webhooks overview (community wiki)](https://jc-aiwiki.pages.dev/gohighlevel/webhooks)
