# Module: Unified conversations and inbox
Status: draft   Owner: Ahmad   Phase: 2   Source: ghl-spec section 6, 15.3, 15.4; ADR 0010

## Purpose
One conversation per contact; every channel interleaves in it with delivery status, assignment and realtime updates.

## In scope for MVP / Out of scope
In: conversations, messages, internal comments, assignment, read state, templates and snippets, realtime (SSE), Instagram bridge. Out: SLA timers, snooze (help-desk states are a later differentiator).

## Data model
`Conversation(contact, assigned_to, last_message_at, status, starred)`, `Message(conversation, channel, direction, channel_account, provider_message_id unique, body, status, error_code, sent_by)`, `MessageStatusEvent`, `ChannelAccount`.

## Business rules
1. Inbound and outbound ingestion is idempotent on provider message id.
2. Messages order by provider time; out-of-order status callbacks never move a status backwards.
3. Inbound resolves or creates the contact through identities and the duplicate policy.
4. Every outbound message goes through the send gateway.
5. Internal comments never leave the system.
6. Realtime events carry no message body; clients fetch through the API.

## Events
`crm.message.inbound|outbound|status_changed`, `crm.conversation.assigned`.

## Acceptance criteria
- Given the same webhook delivered twice, when processed, then one message exists.
- Given a delivered callback before sent, when processed, then status ends as delivered.

## Open questions
Retention of message bodies and attachments per plan.

## Side panel, bulk actions and the recent tab
On wide screens the open thread shows the contact beside it: owner, followers, tags and a searchable field list. Ticked threads take one action at once (read, unread, star, unstar, close, reopen, assign, unassign); threads the member cannot see are counted as skipped. The recent tab lists threads with a message in the last `CRM_INBOX_RECENT_DAYS` days (default 7); a request may name at most `CRM_INBOX_BULK_LIMIT` threads (default 100).
