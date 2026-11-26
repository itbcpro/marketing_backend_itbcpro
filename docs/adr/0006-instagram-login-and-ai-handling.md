# ADR 0006: Instagram API with Instagram Login, and how AI handles the community

Status: accepted (2026-10-01)

## Context
P6.1.1 asks which authentication path to use for Instagram: Instagram API with Instagram Login, or
Facebook Login for Business with a linked Facebook Page. The platform also needs to answer comments
and direct messages with AI without ever being able to say or do something the owner did not allow.

## Decision
1. **Instagram API with Instagram Login** (`graph.instagram.com`). It needs no Facebook Page, covers
   Business and Creator accounts, and exposes everything this plan needs: media, insights, publishing,
   comments, messages and webhooks. Permissions: `instagram_business_basic`,
   `instagram_business_content_publish`, `instagram_business_manage_comments`,
   `instagram_business_manage_messages`, `instagram_business_manage_insights`.
2. **One long-lived token per connection.** The authorisation code buys a one-hour token that is
   exchanged at once for a 60 day token; there is no refresh token, so the token itself is refreshed
   daily when it expires within 14 days (`social.refresh_tokens`). The standard OAuth code path gained two
   optional descriptor hooks (`exchange`, `refresh`) so a platform with this shape needs no special case
   in the connection code.
3. **The app keys belong to the workspace**, stored in a connection of provider `instagram_app`
   (with the environment as fallback), the same pattern as Google Ads.
4. **Everything that changes Instagram goes through the action layer**: reply, hide, delete, toggle
   comments, send message, publish. Risk classes: hide and toggle are R1 (reversible); reply, message and
   publish are R2 (public or sent to a person); delete is R3 and never automatic.
5. **"Automatic" is an autonomy grant.** In the response settings the owner chooses per category whether a
   reply is off, a draft for a person, or automatic. Choosing automatic creates an `AutonomyGrant` for the
   `community-responder` agent (level 3), which only an owner or admin may do (`change_autonomy`). The kill
   switch, guardrails, audit trail and rollback apply unchanged. Turning automation off removes the grants.
6. **The model classifies and drafts; code decides.** Comments and messages are passed to the model fenced as
   untrusted data and the model has no tools, so nothing a stranger writes can trigger an action. After the
   model answers, hard rules in code (`social/text_rules.py`) strip hashtags and unapproved links, enforce
   length, flag banned words, contact details in public replies and emoji policy. Text that breaks a rule
   that cannot be fixed is never sent automatically. Automatic sending additionally needs: model confidence
   above the owner's threshold, the category allowed, the delay passed, and the hourly cap not reached.
   Safety categories (spam, abuse, sensitive) are never answered; complaints always go to a person.
7. **Direct messages** follow Instagram's rules in code: replies only within 24 hours of the person's last
   message, opt-out words stop the bot at once without asking the model, requests for a human and the owner's
   always-human topics hand off with a summary, and caps per hour and per conversation stop loops.
8. **Publishing is resumable and approval is recorded.** The approver's identity is stored on the draft;
   at the due time the platform proposes the publish action and approves it as that person, so permission
   is re-checked then. Container ids and the published media id are saved as soon as Instagram returns
   them, so retries never create a second post. A slot missed by more than six hours fails instead of
   publishing late.
9. **Statistics are computed in code, the model explains them.** Medians with sample sizes; groups with
   fewer than five posts are labelled "too early" and carry no conclusion.
10. **Pictures:** branded text cards are drawn locally from the brand colours (exact text, AA contrast);
    AI images use an image-generation port (OpenAI adapter) when a workspace connects an image key.

## Consequences
- Media for publishing must be reachable at a public HTTPS address: files are served through short-lived,
  signed, single-asset links (`APP_PUBLIC_BASE_URL`).
- The Instagram app needs Meta app review for accounts without a role on the app; development works with
  role accounts. The webhook callback and verify token are configured in the Meta app.
- Stories cannot get stickers through the API; the composer designs stories to stand alone.
- Facebook Login for Business remains available for a later Facebook Page integration (P6.10); Page
  messaging and comments can reuse the engines in `social/replies.py` and `social/messaging.py`.
