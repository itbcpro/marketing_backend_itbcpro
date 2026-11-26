# External Platform Facts (verified 2026-09-29)

Purpose: the single place where third-party facts live, with sources and a list of what is **not yet verified**.
Rule: no task may rely on a fact that is not here or in the vendor documentation read during the task's own
`re-verify` subtask. Where sources disagree, the conflict is recorded and resolved by a `[spike]`.

## Version pinning and drift watch

- API versions live in configuration (`PROVIDER_API_VERSIONS`), one entry per adapter, with the sunset date if known.
- A scheduled job (`P0.10.9`) checks vendor changelogs / sunset pages weekly and opens an alert when a pinned version is within 90 days of sunset or a newer major exists.
- Each adapter has a nightly contract smoke test against a sandbox / test account.

## Meta (Marketing API, Instagram, Facebook)

Verified:
- Graph API and Marketing API **v26.0 released 2026-07-29**. v25.0 released 2026-02-18. v23.0 reached end of life 2026-06-09; v24.0 is the oldest supported. Versions live roughly two years. Source: Meta changelog v26.0, Kitchn Q2 2026 summary. Note: some third-party pages still list v25 as latest; treat the official changelog as authoritative and pin v26 unless the spike finds a blocker.
- v26.0 Marketing API breaking changes: Delivery Estimate fields `daily_outcomes_curve`, `budget_guardrail`, `estimate_dau` removed; Instagram Explore Feed placement removed (delivery redirected); Messenger Stories position silently removed; poll components no longer supported in ads and creatives; creatives with `applink_treatment=web_only` cannot attach to Website-and-App conversion campaigns. New: WhatsApp Status ads expansion, Shop Ads default `destination_type = WEBSITE_AND_SHOP` when a shop exists.
- **Advantage+ unification**: legacy Advantage+ Shopping (ASC) and App (AAC) campaign creation and updates through the Marketing API are blocked on all versions since 2026-05-19. A campaign becomes "Advantage+" when three automation levers are enabled: budget automation (`advantage_budget_state`, campaign-level budget with supported bid strategies), audience automation (`advantage_audience_state`, Advantage+ audience or minimal geo-only targeting) and placement automation (`advantage_placement_state`, no placement targeting or exclusions). Read-only `advantage_state` values: `ADVANTAGE_PLUS_SALES`, `ADVANTAGE_PLUS_APP`, `ADVANTAGE_PLUS_LEADS`, `DISABLED`. `smart_promotion_type` flags replaced by `GUIDED_CREATION`; `existing_customer_budget_percentage` deprecated. Migration endpoints: `migrate_to_advantage_plus` on Copies (ASC only) or on the campaign (ASC and AAC).
- Metric retirements from June 2026: post and page reach, video impressions, story impressions replaced by Media Views, Media Viewers, Page Viewer. Ads Insights: 7-day and 28-day view attribution windows removed since 2025-01-12; retention limits of 13 months for unique and hourly breakdowns, 6 months for frequency.
- Webhooks: since 2026-03-31 Meta signs webhook mTLS with its own CA (`meta-outbound-api-ca-2025-12.pem`); receivers must trust it.
- Access tiers (2026-05-04): "Ads Management Standard Access" renamed **Marketing API Access Tier**; "Standard Access" → **Limited access**, "Advanced Access" → **Full access**. Auto-approval threshold **500 Marketing API calls in 15 days** with error rate **under 15% over a rolling 500-request window**; screen recording no longer required for upgrade.
- Country and location inside `lookalike_spec` deprecated, cut over 2026-09-01. Nielsen DMA targeting replaced by Comscore Markets for automotive (2026-06-22).
- Instagram Graph API: publishing cap per account per rolling 24 hours (sources disagree: 50 vs 100 in Meta's own pages; treat as a configurable value, default 50, and read the live quota endpoint); Reels and Stories count toward it. Automated DMs about 200 per hour per account. Reply window 24 hours after the user's last message. Business Use Case rate limit about 200 calls per user per hour. App Review mandatory, typically 2–4 weeks.

Not yet verified (spikes): exact current permission names for Instagram business login vs Facebook login flows; human-agent message tag window and eligibility; Reels container requirements (codec, duration, size) in v26; quota endpoint fields; Meta Ad Library API scope (competitor ads outside political and EU); `ads_management` review requirements for agency vs own-business use; WhatsApp scope (out of plan for now).

Instagram, as implemented (P6; written from Meta documentation, not yet confirmed against a live account):
- Path: Instagram API with Instagram Login on `graph.instagram.com` (ADR 0006); version from `PROVIDER_API_VERSIONS["instagram"]` (default v26.0).
- Tokens: the authorisation code buys a 1 hour token, exchanged for a 60 day token; the long-lived token is refreshed through `refresh_access_token` (needs to be at least 24 hours old and unexpired).
- Media metrics requested: `reach, likes, comments, saved, shares, total_interactions, views`; reels add `ig_reels_avg_watch_time` and `ig_reels_video_view_total_time` (milliseconds); stories use `replies`. Older posts reject some metrics, so smaller sets are tried in turn.
- Account insights: `reach, views, accounts_engaged, total_interactions, likes, comments, saves, shares, replies, reposts, follows_and_unfollows, profile_links_taps` as 28 day `total_value`; `reach` and `follower_count` as daily time series (`follower_count` is new followers per day); demographics need 100 or more followers.
- Publishing: container flow (`media`, `status_code` of `IN_PROGRESS`, `FINISHED`, `ERROR`, `EXPIRED`, then `media_publish`); images as JPEG; `content_publishing_limit` read before every publish with a safety margin.
- Messaging: send API within 24 hours of the person's last message; opt-out words are honoured before any model call.
- Webhooks: signature `X-Hub-Signature-256` with the app secret; verification handshake with `hub.verify_token`; fields `comments, messages, mentions, message_reactions`.

Sources: developers.facebook.com Marketing API changelog and v26.0 changelog; ppc.land on Advantage+ unification; kitchn.io Q2 2026 update; Instagram API guides (Phyllo, Storrito, bundle.social).

## Google Ads

Verified:
- Latest **v25.2 (2026-09-23)**, no breaking changes; v25 released 2026-07; v24 May–June 2026. Sunsets: v20 on 2026-06-10, v21 on 2026-08-05, **v22 on 2026-10-07**. Policy: about three majors per year, up to four majors live, majors last about 12 months.
- v25 breaking changes: Customer and Campaign Lifecycle Goal services removed, migrate to unified `Goal` and `CampaignGoalConfig`; `CustomerLifecycleOptimizationValueSettings` restructured; several reach-forecast and incentive-service field changes; creator-insights `search_brand` removed.
- New capabilities: `AssetGenerationService` (generative text and image assets), Performance Max automated video from URL and asset automation, Demand Gen TargetCPC and animated images, new goal types (Loyalty Retention, New Customer Acquisition), synthetic-content attestation fields mutable.
- Python client library exists (`google-ads`); the current release number must be read from PyPI in `P4.1` (search results were inconsistent).
- The API supports `validate_only` on mutate requests and partial failure handling (confirm exact per-service behaviour in `P4.6`).

Not yet verified (spikes): developer token access levels and current approval process; OAuth vs service account for manager accounts; PMax and Search API limitations in v25; Ads Transparency data access; recommended REST vs gRPC transport for Python at this version.

Sources: developers.google.com/google-ads/api release notes, versioning and sunset dates pages; Google Ads developer blog 2026.

## OpenAI Ads

Verified:
- Advertiser API base `https://api.ads.openai.com/v1`; auth `Authorization: Bearer <key>`; **one API key per ad account** issued in Ads Manager settings; Ads Manager is in beta; partner setup is separate.
- Hierarchy: ad account → campaign → ad group → ad. Campaigns hold budget and targeting; ad groups hold bidding; ads hold creative.
- Rate limits: 600 requests per minute per endpoint, 1,200 per minute per account; bulk job creation 10 requests per 10 seconds per account. `Idempotency-Key` header supported on creates.
- Campaign endpoints: list, create, retrieve, update (POST), `activate`, `pause`, `archive`. Statuses `active` and `paused`; archive is permanent. Create requires `name` (3–1000 chars), `status`, `budget.lifetime_spend_limit_micros` (min 1,000,000). Optional: `description`, `start_time`, `end_time` (Unix), `mode` (`product_feed`), `bidding_type` (`impressions`, `clicks`, `conversions`), `conversion_event_setting_ids`, targeting (`locations.include`, `excluded_locations`, `platforms.included` in `ios_app`, `android_app`, `web`, custom audiences include and exclude).
- Ad group endpoints likewise with activate, pause, archive; create requires `campaign_id`, `name`, `status`, `bidding_config.billing_event_type` (`impression` or `click`), `bidding_config.max_bid_micros`; optional `context_hints` (up to 2,000), `product_set`, custom-audience bid multipliers (0.1x to 10x, expressed 100000 to 10000000). Amounts in micros.
- Targeting limits: up to 2,500 location ids per include and exclude list; custom audiences need at least 25,000 matched users for inclusion or multipliers; exclusions win over inclusions; product-feed campaigns support country-level location only.
- Creatives: minimum 640×640 images; documented format `chat_card` (title, body, image, destination URL); creatives enter review on ad creation; activation checks serving issues and account review state. Upload limit 500,000,000 bytes; inline request limit 16 MiB.

Resolved on 2026-09-30 (see `docs/vendor/openai-ads-api.md`): ads endpoints and creative types (`chat_card`, `product_ad_template`), insights and conversion-insights endpoints, Conversions API, Bulk API with `validate_only`, bidding strategies (`fixed_bid`, `maximize_clicks`, `maximize_conversions`), daily vs lifetime budgets, files upload, spend limits, review statuses.

Still unknown (probe in a live low-budget account): error body shape, serving-issue and rejection reason codes, sandbox availability, partner/agency access, and whether Canadian advertisers and locations are supported.

Sources: developers.openai.com/ads (API overview, campaigns, ad groups, campaign targeting); OpenAI Help Center Ads Manager Beta overview.

## LinkedIn

Verified:
- Posts API at `/rest/posts`, versioned header `Linkedin-Version: YYYYMM`; organisation posting needs scope `w_organization_social`, an admin role on the page, and **Community Management API** access, which is a restricted product requiring an application (use case, screen recording, test credentials). Member posting via `w_member_social` is self-serve. Documentation revision `2026-04` exists.

Not yet verified (spikes in `P7.1`): current approval turnaround, daily API quotas, scheduling support in API, video and document upload flows, analytics endpoints and scopes, whether Marketing (ads) API is in scope later.

## WordPress (site under our control)

Verified live from the connected site (ability discovery, 2026-09-29): 78 abilities exposed through the WordPress MCP adapter, including core site info, posts / pages / CPT CRUD, categories and tags, comments (moderate, reply, update), media upload from URL, post meta read and write, active plugins, site stats, Rank Math, Yoast and SEOPress metadata read and write, Rank Math schema write, Yoast sitemap index, Polylang / WPML language and translation linking, Meta Box and JetEngine fields, WP Mail SMTP email logs and stats, and Elementor (page structure, create page, preview link, publish, widgets, global classes and variables).

Implications: the platform can start with the existing MCP abilities (fast) but should own a stable `CmsProvider` port so it can fall back to the WordPress REST API with application passwords. The set of installed SEO plugins is discovered at runtime and mapped through an SEO-plugin adapter (Rank Math, Yoast, SEOPress).

## SEO, GEO and AI-search evidence (read before designing P2)

- Generative-engine optimisation is about being cited in AI answers (AI Overviews, ChatGPT, Perplexity, Gemini, Claude). Guidance is contested; the plan therefore **measures instead of assuming**.
- Google's documentation lists `llms.txt` and content "chunking" as unnecessary for AI search. Plan: `llms.txt` is an optional low-priority experiment, never a headline task.
- A May 2026 study reported by Search Engine Journal found adding JSON-LD did not measurably raise AI citations for pages already visible in AI Overviews, while other guides still recommend structured data. Plan: keep schema for classic rich results and machine readability, but evaluate its GEO effect with our own A/B sampling.
- Reported pattern: a large share of LLM citations come from the first part of a document, so answer-first intros matter. Same query returns different AI answers almost always, so measurement needs repeated sampling and confidence intervals.
- Site must be crawlable by AI user agents the owner wants to allow (search-oriented agents vs training agents are separate decisions), and pages must be indexable and fast (field data from CrUX).

## Tooling versions verified

| Component | Verified state |
|---|---|
| Django | 6.1 released 2026-08-05, 6.1.1 bugfix 2026-09-02; 6.0 (2025-12) added `django.tasks` (API only, no worker), template partials, built-in CSP |
| Celery | 5.6.3 (2026-03-26) stable |
| Next.js | 16.3.6 current; security release scheduled 2026-09-30 (16.3.7): upgrade immediately after release |
| Tailwind CSS | v4.3 current (CSS-first config, logical utilities, scrollbar styling) |
| PostgreSQL | 18 (released 2025-09-25; 18.3 seen in vendor news), native `uuidv7()`, async I/O; verify pgvector and hosting support for 18 in `P0.5` |
| Claude Agent SDK (Python) | 0.2.159 (2026-09-23), bundles the Claude Code CLI |
| Anthropic models | Fable 5.1 `claude-fable-5-1`, Opus 5.5 `claude-opus-5-5`, Sonnet 5.5 `claude-sonnet-5-5`, Haiku 4.5 `claude-haiku-4-5-20251001` (routing by tier lives in configuration) |

Everything above is re-verified by the first subtask of the phase that uses it.
