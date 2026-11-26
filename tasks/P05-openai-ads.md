# P5 — OpenAI Ads (ads.openai.com)

Goal: create, launch, monitor and optimise campaigns on the OpenAI Ads platform end to end through the Advertiser API.
Needs: P0, P1 (especially P1.5), P0.20.4 (account, billing and API key are owner actions).

Status of knowledge: the Advertiser API exists (`https://api.ads.openai.com/v1`) and is in beta. Campaigns and ad groups are documented in detail; ads, insights, conversions, uploads, product feeds and errors are only partially documented in the sources read on 2026-09-29. Phase P5 therefore begins with a documentation spike and treats every undocumented behaviour as unknown until verified.
Because the platform is new, the adapter must be built for change: strict payload validation, contract tests, feature detection and a fast path for spec updates.

---

## P5.1 Documentation and capability spike (M) [spike]
Needs: P0.20.4.
- P5.1.1 Read all documentation sections: overview, quickstart, campaigns, ad groups, ads, uploads, conversions, insights / reporting, custom audiences, product feeds, targeting, policies, changelog, errors, pagination, webhooks (if any).
- P5.1.2 Record for each resource: endpoints, required and optional fields, enums, limits, statuses, error shapes, idempotency behaviour, eventual consistency notes.
- P5.1.3 Establish access model: API key per ad account (rotation, revocation, scopes), partner / agency setup path, multi-account handling, sandbox or test mode availability.
- P5.1.4 Establish policy and review: ad review states, common rejection reasons, restricted categories, disclosure requirements, landing page rules, geographic availability (is Canada supported for advertiser and audience?).
- P5.1.5 Establish measurement: pixel and conversions API details, attribution windows, reporting granularity and latency, breakdowns available, export options.
- P5.1.6 Output: updated section in `external-platform-facts.md`, a machine-readable spec snapshot in the repo (OpenAPI if published, otherwise hand-written JSON schemas), and a gap list that blocks or shapes later tasks.
- Done when: every later P5 task can point to documented behaviour; unknowns are listed with a plan to test them in a low-budget live account.

## P5.2 Connection (S)
Needs: P5.1, P0.7.
- P5.2.1 Connection form: API key per ad account, account id, display name; test-connection call; store key in the vault; key rotation flow and expiry reminders.
- P5.2.2 Multi-account support (one connection per ad account) with capability flags.
- P5.2.3 Health check job: account status, review status, serving issues, billing state (as exposed by the API).
- Done when: a valid key connects and an invalid or revoked key degrades the connection with a clear UI message.

## P5.3 API client (M)
Needs: P5.1, P0.7.3.
- P5.3.1 Typed client with base URL and version from config, Bearer auth, JSON handling, pagination helper, standard error mapping (auth, validation, rate limit, not found, conflict, server).
- P5.3.2 Rate limiting to the documented limits: 600 requests per minute per endpoint, 1,200 per minute per account, bulk job creation 10 per 10 seconds; token buckets in the rate-limit registry; honour `retry-after` when present.
- P5.3.3 Idempotency: send `Idempotency-Key` on every create (derived from the action id + step), store keys, handle replay responses.
- P5.3.4 Micros helpers and currency handling (amounts are millionths); unit conversion tests.
- P5.3.5 Time handling: Unix timestamps for start and end; timezone conversion rules per account.
- P5.3.6 Upload client for creatives: minimum 640×640 images, size limit up to 500,000,000 bytes, inline request limit 16 MiB, chunking rules if any; virus / format validation before upload.
- P5.3.7 Contract tests with recorded fixtures and a live smoke test in a low-budget account.
- Done when: the adapter passes the shared `AdsProvider` contract suite for supported capabilities and declares unsupported ones via the capability descriptor.

## P5.4 Structure sync (M)
Needs: P5.3.
- P5.4.1 Sync ad account, campaigns, ad groups, ads, custom audiences, conversion settings, uploads metadata into the neutral model (`mode: product_feed` vs standard noted).
- P5.4.2 Status model mapping (`active`, `paused`, `archived`, review states, serving issues); archived is permanent and must be surfaced as irreversible in UI.
- P5.4.3 Incremental sync strategy (polling with filters or webhooks if available), reconciliation job, drift detection for edits made in Ads Manager.
- Done when: neutral model matches Ads Manager for a real account.

## P5.5 Insights ingestion (M)
Needs: P5.1.5, P5.4.
- P5.5.1 Daily insights pull at campaign, ad group and ad level: impressions, clicks, spend, conversions and value where available, CPM / CPC / CPA computed consistently; breakdowns supported by the API (platform: web, iOS, Android; geography; time).
- P5.5.2 Restatement window based on the documented conversion latency; data completeness flags.
- P5.5.3 Reconciliation vs Ads Manager totals; freshness alerts.
- P5.5.4 Store into `MetricSnapshot` with provider-native fields in raw JSONB.
- Done when: last 30 days reconcile with the UI within tolerance.

## P5.6 Campaign planning for a conversational surface (L)
Needs: P5.1, P5.4, P1.10, P1.6.
- P5.6.1 Understand the medium: ads appear in ChatGPT experiences (`ios_app`, `android_app`, `web` platform targeting); planning differs from search or social. Encode guidance found in the spike into planning prompts (context hints, tone suitable for a conversational context, disclosure of sponsored content).
- P5.6.2 Objective and bidding mapping: `bidding_type` (`impressions`, `clicks`, `conversions`) vs billing event (`impression` or `click`) and `max_bid_micros`; selection rules with rationale and safety caps.
- P5.6.3 Budget: `budget.lifetime_spend_limit_micros` (minimum 1,000,000 micros) is a lifetime cap, so translate daily goals into lifetime limits plus schedule (`start_time`, `end_time`) and pacing checks; guard against a large lifetime limit being consumed quickly.
- P5.6.4 Targeting: location include / exclude (country codes or location ids, up to 2,500 ids each), platform selection, custom audiences (25,000 minimum for inclusion or multipliers; exclusions have no minimum), exclusion precedence rules, `context_hints` authoring (up to 2,000 hints) derived from products, use cases, customer questions and GSC query data.
- P5.6.5 Product-feed mode: when a product feed exists, use `mode: product_feed` with `product_set` (country-level location only); otherwise standard mode; feed generation from WordPress / product data as a later optional task.
- P5.6.6 Conversion setup: `conversion_event_setting_ids`, pixel or conversions API status checks; block conversion bidding when measurement is missing.
- P5.6.7 Bid multipliers for custom audiences within documented range (0.1x to 10x) with guardrails.
- P5.6.8 Naming, tagging and UTM conventions consistent with other platforms (`utm_source=openai` style value decided in P9 taxonomy).
- P5.6.9 Risk assessment for a new, changing platform: lower default autonomy (`L2` only), lower default caps, extra verification steps.
- Done when: plans for a lead-generation brief validate against the documented constraints and are accepted by a human media buyer.

## P5.7 Creative and copy (M)
Needs: P5.1, P5.6, P3.8.
- P5.7.1 Format catalogue from the spike (documented `chat_card`: title, body, image, destination URL, plus any others) with length limits, image ratios and minimum size (640×640).
- P5.7.2 Copy generation tuned to conversational placements: helpful, specific, non-deceptive, clearly sponsored; multiple variants by angle and intent; locale-aware.
- P5.7.3 Image generation or selection via the shared asset pipeline; upload to the platform uploads endpoint; verification of acceptance.
- P5.7.4 Destination URL rules: HTTPS, fast, matches promise, UTM template, tracking of click ids if the platform provides one.
- P5.7.5 Preview: platform preview if available, otherwise a faithful mock renderer built from the format spec (labelled as a mock).
- Done when: variants pass length, format and policy checks and are accepted by the platform's review in a live test.

## P5.8 Policy pre-check (S)
Needs: P5.1.4, shared framework.
- P5.8.1 Rule set from the platform's ad policies (versioned), landing page checks, sensitive category detection, claim checks; LLM reviewer with structured findings.
- P5.8.2 Review outcome ingestion and fix proposals (as in P3.7.5).
- Done when: seeded violations are flagged and rejections generate fix proposals.

## P5.9 Publish flow (L)
Needs: P5.3–P5.8, P1.5.
- P5.9.1 Action kinds: `ads.openai.create_campaign_paused`, `activate`, `pause`, `update_budget`, `update_bid`, `update_targeting`, `replace_creative`, `archive` (R3, irreversible), `create_audience`.
- P5.9.2 Creation order: upload creatives → campaign (paused) → ad groups (paused) → ads (creatives enter review) → verify; each step idempotent via key; compensating cleanup on failure.
- P5.9.3 Activation sequencing: activate campaign, then ad groups, then ads as the API requires; check serving issues and account review status (the API does this) and surface the reasons in the approval item.
- P5.9.4 Verification: re-read all objects, compare with plan, confirm review states, confirm targeting and bidding values in micros.
- P5.9.5 Initial monitoring window with tighter alerts (no delivery, rejection, overspend against lifetime limit).
- P5.9.6 Rollback: pause; archiving only through explicit R3 approval.
- Done when: a low-budget live campaign is created, reviewed, activated and paused with full verification and audit trail.

## P5.10 Optimisation (L)
Needs: P5.5, P5.9, shared engine.
- P5.10.1 Rules and statistics for a young platform: conservative thresholds, larger minimum samples, longer cool-downs; measure relative to other channels.
- P5.10.2 Proposals: bid changes within caps, budget adjustments, pause poor ads, audience exclusions, context-hint refinements, creative rotation.
- P5.10.3 Anomaly detection and pacing against the lifetime cap.
- P5.10.4 Cross-channel comparison inputs for P9 (cost per lead vs Meta and Google) with caveats about attribution differences.
- P5.10.5 Learning capture into memory with an explicit "platform is evolving" freshness policy (shorter validity windows).
- Done when: proposals are explainable and backtests (as data allows) show no harm relative to do-nothing.

## P5.11 Conversions and audiences (M)
Needs: P2.16, P5.1.5.
- P5.11.1 Pixel / conversions API integration as documented: event definitions, deduplication, consent, hashed identifiers.
- P5.11.2 Custom audience creation from first-party lists (hashed email, phone, GAID) only with recorded consent; minimum size checks; refresh and deletion handling.
- P5.11.3 Measurement verification job (events arriving, match rate if available).
- Done when: conversion events from the website appear in the platform with deduplication.

## P5.12 Alerts and reports (S)
Needs: P5.5, P1.9.
- P5.12.1 Alerts: rejection, serving issue, account review state change, lifetime cap nearly consumed, API errors rising, API key issues, changelog updates from the vendor.
- P5.12.2 Daily digest and weekly narrative.
- Done when: alerts reproduce in tests.

## P5.13 OpenAI Ads menu UI (M)
Needs: P0.13, P5.4–P5.12.
- P5.13.1 Overview, campaign explorer, campaign detail (structure, creatives, review states, insights), new-campaign flow entry, audiences, settings (connection, guardrails, autonomy).
- P5.13.2 Clear labelling of beta status and irreversible actions; typed confirmation for archive.
- P5.13.3 Catalog strings, e2e.
- Done when: all P5 capabilities are operable from the menu.

## P5.14 Change resilience and testing (M)
Needs: all of P5.
- P5.14.1 Weekly documentation diff job (fetch docs pages, hash, alert on change) feeding the API drift watch.
- P5.14.2 Strict response validation with tolerant reader for new fields; unknown enum values handled safely.
- P5.14.3 Nightly live smoke test in a low-budget account, feature flags per capability to disable broken parts quickly.
- P5.14.4 Failure drills: key revoked, rate limit, worker crash between ad group and ad creation, creative rejected after activation.
- Done when: a simulated breaking change disables only the affected capability and alerts the owner.
