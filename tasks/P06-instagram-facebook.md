# P6 — Instagram and Facebook (organic, community, messaging)

Goal: plan, create, publish and analyse posts, carousels, reels and stories; moderate comments; answer direct messages with safe automation and human handoff; manage the Facebook page. Each channel has its own menu.
Needs: P0, P1, P3.1 (shared Meta app, tokens, webhooks), P0.20.1 review approvals for the required permissions.

Facts to re-verify first (P6.1.1): publishing quota per rolling 24 hours (sources disagree: 50 vs 100), automated DM limit (about 200 per hour), 24-hour reply window and message tags, permission names, Reels container requirements, retired metrics (reach, impressions replaced by views metrics). See `docs/external-platform-facts.md`.

---

## P6.1 Connection and permissions (L)
Needs: P3.1, P0.7.
- P6.1.1 [spike] Choose the auth path: Instagram API with Instagram Login vs Facebook Login for Business with a linked Page. Verify permission names, review requirements, token lifetimes, business vs creator account support, and webhooks per path. Output: ADR and permission matrix.
- P6.1.2 Connect Instagram professional account and Facebook Page; show linkage status, account type, followers, page roles, verification status.
- P6.1.3 Long-lived token management (exchange, refresh before expiry), revoked-token detection, re-auth UI.
- P6.1.4 Permission gap detector per feature (publish, comments, messaging, insights) with plain-language fixes.
- P6.1.5 Webhook subscriptions: comments, mentions, messages, story insights where available, page feed; verification, signature checks, mTLS CA trust (shared P3.14).
- Done when: both accounts connect, missing permissions are explained, and webhook events arrive and are stored.

## P6.2 Shared social domain (M)
Needs: P0.3, P6.1.
- P6.2.1 Neutral models: `SocialAccount`, `Post`, `PostVariant`, `MediaAsset` usage, `ContentCalendarItem`, `Conversation`, `Message`, `Comment`, `EngagementMetric`; provider-neutral `SocialProvider` port with capability descriptors per network.
- P6.2.2 Post state machine: `idea → drafted → in_review → approved → scheduled → publishing → published | failed`, with edit rules per state.
- P6.2.3 Content pillars and campaign links: posts belong to pillars (education, proof, offer, culture) and optionally to an ad or SEO campaign.
- Done when: the same domain model serves Instagram, Facebook and LinkedIn with per-network capability limits.

## P6.3 Content strategy and calendar (L)
Needs: P6.2, P1.6, P2.8, P8.
- P6.3.1 Strategy agent: per-network goals, audience, pillars, cadence, best times (from own insights), formats mix; stored as reviewable strategy memory.
- P6.3.2 Idea generation sources: website articles (repurpose), keyword and question data, competitor and trend signals (P8), customer questions from DMs and comments, seasonality and events.
- P6.3.3 Calendar generation with capacity limits, no clashes with ad campaigns, holiday awareness, per-network cadence rules, cross-post adaptation (not copy-paste).
- P6.3.4 Calendar UI contract: week and month views, drag to reschedule (creates edit actions), status filters, workload indicator.
- Done when: a 4-week calendar with varied formats and rationale is produced and approved in one review.

## P6.4 Post creation (L)
Needs: P6.3, P3.8.
- P6.4.1 Caption generation: hook, body, CTA, hashtags (relevance and number rules, banned list), mentions, emoji policy from brand voice, locale-specific adaptation, length limits, first-comment strategy.
- P6.4.2 Formats: single image, carousel (up to the network's limit, slide-by-slide narrative), reels, stories (sequence, stickers are not API-supported, so design accordingly), text-only for Facebook and LinkedIn.
- P6.4.3 Visual production through the shared asset pipeline: templates for carousels and quote cards rendered from brand tokens (fonts, colours, safe zones), typography for the languages a brief requires, alt text generation, accessibility checks (contrast, text size).
- P6.4.4 Alt text and accessibility descriptions for every image.
- P6.4.5 Brand and compliance checks (banned claims, disclosure of sponsored content, copyright and music rights, competitor mentions).
- P6.4.6 Variant testing plan (hook A/B by time slot) using the shared experiment framework.
- Done when: drafts pass automated checks and preview correctly in a mock of the native layout.

## P6.5 Publishing (L)
Needs: P6.2, P6.4, P1.5.
- P6.5.1 Action kinds: `social.instagram.publish_image`, `publish_carousel`, `publish_reel`, `publish_story`, `schedule`, `delete`, `edit_caption` (where supported), `first_comment`; similar for Facebook.
- P6.5.2 Media hosting: public HTTPS URLs required by the API (signed, expiring URLs from object storage) or resumable upload where supported; validation of dimensions, aspect ratios, duration, codecs, size; auto-transcode to supported specs.
- P6.5.3 Container flow: create media container → poll status (`IN_PROGRESS`, `FINISHED`, `ERROR`) with backoff and timeout → publish; carousel child containers; reels processing time; error explanation mapping.
- P6.5.4 Quota manager: read the content publishing limit endpoint, block scheduling that would exceed the rolling 24-hour limit, spread posts; configurable safety margin.
- P6.5.5 Scheduling: platform-side scheduler (Celery) publishing at the due time with retry; time zone handling; missed-slot policy; native scheduling on Facebook Pages where the API supports it.
- P6.5.6 Post-publish verification: fetch permalink, confirm media type and caption, record ids; failures produce alerts and one automatic retry.
- P6.5.7 Delete and correction flows with audit; caption edits limited by API capability.
- P6.5.8 Collaboration and tagging features (collab posts, product tags, location tags, user tags) only where supported, behind capability flags.
- Done when: image, carousel, reel and story publish to a test account with verification, and quota limits are respected under load.

## P6.6 Insights and analytics (M)
Needs: P6.1, P0.5.4.
- P6.6.1 Account and media insights ingestion with the current metric names (views, reach where valid, viewers, interactions, saves, shares, profile actions, follows, watch time for reels, retention), demographics (with thresholds), online followers for best-time analysis.
- P6.6.2 Metric compatibility layer: handle retired metrics and version differences; store definitions per metric with validity dates.
- P6.6.3 Post-level outcome windows (24 hours, 7 days, 28 days) feeding memory (P1.7.3).
- P6.6.4 Content analysis: which hooks, formats, topics, lengths, posting times perform for this account; statistically careful comparisons.
- P6.6.5 Follower growth, audience quality, hashtag performance where APIs allow.
- Done when: dashboards show consistent per-post and per-account metrics with definitions and known limitations.

## P6.7 Reel and video production (XL → split) [decision: vendors]
Needs: P3.8, P0.20.6.
- P6.7.1 [spike] Vendor evaluation for AI video and template video: capability, cost per second, quality, control (image-to-video, text-to-video, lip sync, subtitles), turnaround, licensing, content policy, API stability, watermark. Output: chosen adapters and a fallback.
- P6.7.2 Reel formats catalogue: talking-head from script (avatar or voice), b-roll montage with voiceover, kinetic typography, screen demo, product showcase, testimonial cards; each with template and constraints (length 15–45 s default, 9:16, 1080×1920).
- P6.7.3 Pipeline: idea → script (hook in first 2 seconds, structure, CTA) → storyboard → asset generation or selection → voiceover (licensed or synthetic voice with consent and disclosure rules) → subtitles (accurate, brand-styled) → music (licensed library only) → render → quality gate → export.
- P6.7.4 Automated quality gate: black frames, audio loudness normalisation, subtitle sync, safe zones, aspect ratio, artifacts detection where feasible, brand elements present.
- P6.7.5 Human review UI: watch, edit script and captions, regenerate segments, approve.
- P6.7.6 Cost controls: per-video budget, caching of reusable segments, batching.
- P6.7.7 AI disclosure labels as required by platform policy.
- Done when: a reel from brief to approved export is produced within cost and quality thresholds and publishes successfully (P6.5).

## P6.8 Comment management (M)
Needs: P6.1, P1.13, P1.5.
- P6.8.1 Ingest comments via webhooks and polling; threads; mentions; story replies.
- P6.8.2 Classifier (untrusted content): question, praise, complaint, lead intent, spam, abuse, competitor, off-topic, safety-sensitive.
- P6.8.3 Actions: reply (draft in brand voice), like, hide, delete, report, escalate to human, convert to inbound lead event; autonomy per class (auto-hide obvious spam at L3, replies at L2 by default).
- P6.8.4 Reply quality rules: brief, helpful, on-brand, no promises outside policy, no sensitive data, language matching, avoid arguing, escalate complaints.
- P6.8.5 SLAs and inbox views with unresolved counters.
- Done when: spam is handled automatically, complaints escalate with context, and reply drafts match brand voice in evaluation.

## P6.9 Direct messages (XL → split)
Needs: P6.1, P1.13, P1.5, P1.9, P2.16.
- P6.9.1 [decision] Automation policy: which conversation types the AI may answer alone, which need approval, which are always human. Compliance review (CASL, platform messaging policies, opt-out handling).
- P6.9.2 Conversation ingestion: webhook events, thread reconstruction, participant identity (minimal), attachments, reactions, message status; store minimal PII with retention policy.
- P6.9.3 Windows and rules: 24-hour standard reply window, human-agent handoff tag rules (verify current eligibility and duration), rate limit (automated messages per hour) with queueing and prioritisation; opt-out keywords ("stop") honoured immediately.
- P6.9.4 Intent classification: pricing, availability, support, quote request, partnership, complaint, spam, other; urgency; language detection; sentiment.
- P6.9.5 Knowledge base for answers: FAQ, services, pricing rules, policies, tone, sourced from the website content and owner-approved snippets; retrieval with citations; "I don't know" behaviour that escalates instead of guessing.
- P6.9.6 Response generation with guardrails: never invent prices or promises, never collect sensitive data (payment, passwords), links only from an allowlist, short messages, quick replies where supported; prompt-injection defences (P1.13); tainted-run rules apply.
- P6.9.7 Lead capture: detect and structure lead details (name, need, contact, budget, timeline) into `InboundEvent` with consent flags; notify humans immediately for hot leads.
- P6.9.8 Handoff: assign to a human, summary of context, suggested reply draft, SLA timers, ability to take over and hand back to the bot.
- P6.9.9 Analytics: response time, resolution, handoff rate, leads per channel, common questions (feeds content ideas P2.8 and FAQ additions).
- P6.9.10 Safety net: daily review sample of bot conversations, red-flag phrase detection, kill switch for DM automation.
- Done when: FAQ-type DMs are answered correctly within limits, leads are captured and notified, and complex or risky chats reach a human with a summary.

## P6.10 Facebook Page management (M)
Needs: P6.1, P6.2, P6.5.
- P6.10.1 Post types: text, link, photo, multi-photo, video, reel, story, event, offer (where API supports); native scheduling.
- P6.10.2 Page insights ingestion with retired-metric handling and the new Page Viewer metric when available.
- P6.10.3 Comments and Messenger integration reuse P6.8 and P6.9 with page-specific rules (Messenger policies).
- P6.10.4 Page settings hygiene checks (about info, hours, CTA button, roles, spam filter settings) as read-only audits with suggestions.
- P6.10.5 Cross-posting rules from Instagram with format adaptation.
- Done when: page posts, scheduled posts, comments and Messenger flows work with the same safety layer.

## P6.11 Social listening and community (S)
Needs: P6.6, P6.8.
- P6.11.1 Track brand mentions and tags where APIs allow; UGC candidates (with permission workflow before reposting).
- P6.11.2 Sentiment and topic trends from comments and DMs (own data only), feeding P8 and content planning.
- Done when: weekly summary of audience questions and sentiment exists.

## P6.12 Instagram menu and Facebook menu UI (L)
Needs: P0.13, P6.2–P6.11.
- P6.12.1 Instagram: overview, calendar, composer (image, carousel, reel, story), media library, insights, comments inbox, DM inbox, hashtag and content analysis, settings.
- P6.12.2 Facebook: overview, calendar, composer, insights, comments, Messenger inbox, settings.
- P6.12.3 Shared components: mobile-style preview, caption editor with character counters and locale switch, asset picker, approval bar, conversation view with handoff controls.
- P6.12.4 Strings in the catalog; e2e for compose → approve → schedule → publish → see insights, and DM handoff.
- Done when: every P6 capability is operable from its own menu.

## P6.13 Testing and safety (M)
Needs: all of P6.
- P6.13.1 Test accounts (Instagram professional test account, test page), fixtures for webhooks, container states, errors, quota responses.
- P6.13.2 Drills: expired token during publish, container stuck in progress, quota exceeded, webhook burst, malicious DM injection, duplicate webhook delivery.
- P6.13.3 Privacy tests: retention, deletion request, message redaction in logs.
- Done when: drills pass without duplicate posts, lost messages or policy violations.
