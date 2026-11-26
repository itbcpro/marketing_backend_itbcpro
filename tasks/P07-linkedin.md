# P7 — LinkedIn

Goal: publish and manage content on the company page, handle engagement, and measure page performance, within what LinkedIn's API allows.
Needs: P0, P1, P6.2 (shared social domain), P0.20.3 (Community Management API approval is the critical path).

Facts to re-verify first (P7.1.1): Posts API at `/rest/posts` with `Linkedin-Version: YYYYMM`, scope `w_organization_social` and page admin role, restricted-product approval process, versioned API sunset cadence. See `docs/external-platform-facts.md`.

---

## P7.1 Connection and access (M)
Needs: P0.7, P0.20.3.
- P7.1.1 [spike] Re-verify approval status, approved scopes, daily and per-member quotas, current API version and its support window, content types available (text, image, multi-image, video, document, article link, poll), scheduling support, analytics endpoints, comment and reaction APIs, mentions, and restrictions on automation.
- P7.1.2 3-legged OAuth for a page administrator; token and refresh token storage in the vault (LinkedIn refresh token expiry rules), reconnect flow before expiry, health check.
- P7.1.3 Organisation discovery and role verification (only pages where the authorising user is admin), with clear errors when roles change.
- P7.1.4 Version header management from configuration; monthly check of supported versions; upgrade runbook.
- Done when: the company page connects, roles are validated and token expiry is monitored.

## P7.2 API client (M)
Needs: P7.1.
- P7.2.1 Typed client for Posts, Images, Videos, Documents, Social Actions, Organization pages, analytics; URN handling; REST.li quirks (headers such as `X-Restli-Protocol-Version`, `X-RestLi-Method`) kept inside the adapter.
- P7.2.2 Rate-limit handling from response headers and daily quotas; per-endpoint backoff.
- P7.2.3 Error mapping (auth, scope, validation, throttled, duplicate content, policy) and retries.
- P7.2.4 Fixtures, contract tests, nightly smoke on a test page if available.
- Done when: the adapter passes the shared `SocialProvider` contract suite for supported capabilities.

## P7.3 Content strategy for LinkedIn (M)
Needs: P6.3, P1.6.
- P7.3.1 LinkedIn-specific strategy: B2B audience, thought leadership, case studies, service explainers, culture, hiring; cadence and best times from own analytics.
- P7.3.2 Repurposing pipeline from website articles and reels into LinkedIn-native formats (text posts with strong hooks, document carousels as PDFs, image posts, short video).
- P7.3.3 Tone rules: professional, concise, specific; avoid engagement-bait patterns; brand voice memory applies; locale-aware.
- P7.3.4 Personal-profile posting policy: out of scope by default (company page only); if the owner wants executive posting, treat as a separate approval-heavy capability using the self-serve member scope.
- Done when: the calendar for LinkedIn is generated alongside the other networks with format-appropriate drafts.

## P7.4 Post creation and publishing (L)
Needs: P7.2, P7.3, P1.5, P3.8.
- P7.4.1 Formats: text, single image, multi-image, video, document (PDF carousel generated from brand templates), article link with preview, poll if supported.
- P7.4.2 Asset upload flows: register upload → upload binary → finalise, with status polling for video and documents; size, duration and type validation; auto-conversion to required specs.
- P7.4.3 Mentions and hashtags: resolve organisation and person URNs where permitted; hashtag rules; link handling (preview generation, UTM tagging).
- P7.4.4 Scheduling: platform scheduler publishes at the due time (unless the API offers native scheduling per P7.1.1); retries and duplicate prevention (LinkedIn rejects duplicate content; keep idempotency by content hash and stored post URN).
- P7.4.5 Action kinds: `social.linkedin.publish`, `schedule`, `delete`, `edit` (if supported), `comment_reply`.
- P7.4.6 Post-publish verification via post URN fetch and permalink.
- Done when: text, image, document and video posts publish to the page with verification.

## P7.5 Engagement management (M)
Needs: P7.2, P1.13.
- P7.5.1 Ingest comments and reactions on page posts (webhooks if offered, otherwise polling within quotas); mentions of the page.
- P7.5.2 Classification and reply drafts in brand voice; hide or delete spam through the action layer; escalation of leads and partnership requests to inbound events.
- P7.5.3 Messaging: check whether API access to page messages is permitted at all; if not, document as unsupported and provide notification-only guidance rather than workarounds.
- Done when: comments are triaged with drafts awaiting approval.

## P7.6 Analytics (M)
Needs: P7.2, P0.5.4.
- P7.6.1 Page statistics: followers (total, gained, demographics), page views, unique visitors, button clicks; share statistics per post (impressions, clicks, reactions, comments, shares, engagement rate); video analytics if exposed.
- P7.6.2 Time-series ingestion, backfill limits, restatement rules; metric definitions stored with validity.
- P7.6.3 Outcome windows for posts feeding memory; content analysis by format, topic, hook style and time.
- Done when: dashboards show page and post performance with definitions.

## P7.7 LinkedIn Ads (optional, later) (L) [decision]
Needs: owner decision, P0.20.3 extended access.
- P7.7.1 Evaluate whether B2B lead generation warrants LinkedIn Campaign Manager automation (Marketing API access separate from Community Management).
- P7.7.2 If yes: a new phase mirroring P3 (connection, sync, insights, planning, creative, publish, optimise, UI) reusing the shared ads engine; otherwise document as out of scope.
- Done when: a written decision exists.

## P7.8 LinkedIn menu UI (M)
Needs: P0.13, P7.3–P7.6.
- P7.8.1 Overview, calendar, composer with LinkedIn preview (text truncation "see more" simulation), media and document builder, engagement inbox, analytics, settings (connection, roles, version, guardrails).
- P7.8.2 Strings in the catalog; e2e for compose → approve → schedule → publish → analytics.
- Done when: every P7 capability is operable from the LinkedIn menu.

## P7.9 Testing and safety (S)
Needs: all of P7.
- P7.9.1 Fixtures and drills: token expiry, missing scope, duplicate post rejection, upload failure, API version change.
- P7.9.2 Compliance review of automation against LinkedIn's terms (no scraping, no automation outside the official API).
- Done when: drills pass and the compliance note is filed.
