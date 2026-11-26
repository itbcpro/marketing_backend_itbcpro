# CRM-2 Unified conversations and realtime inbox

Needs: CRM-1.2, CRM-1.5. Spec: ghl-spec sections 6, 15.3, 15.4.

- [x] CRM-2.1 Models: `Conversation` (one per contact), `Message` (channel, direction, unique provider id, status, error code), `MessageStatusEvent`, `ChannelAccount`.
- [x] CRM-2.2 Ingestion pipeline: normalise, resolve contact, store once, announce; public signed webhook ingress (HMAC-SHA256 with timestamp, per-endpoint vault-sealed secret, raw archive, replay-safe) for inbound email and bounce or complaint events.
- [x] CRM-2.3 Instagram bridge: map `social` DMs and comments onto contact identities and conversations without duplicating tokens.
- [x] CRM-2.4 Inbox API: tabs, filters, search, assignment, read state, starred, internal comments with mentions.
- [x] CRM-2.5 Realtime: inbox changes are pushed over the existing resumable SSE stream (ids only). The stream itself still uses session auth.
- [x] CRM-2.6 Templates and snippets with merge fields: backend with validation and preview, template editor with field picker and preview, saved-reply picker in the inbox reply form.
- [x] CRM-2.7 Inbox UI: list, tabs, search, thread, delivery states, star, assign, close, internal notes, live refresh (replies come with the send gateway).
- [x] CRM-2.8 Notifications: in-app bell with unread count, email digest, per-user preferences per event kind.
