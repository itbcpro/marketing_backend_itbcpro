# P4 — Google Ads

Goal: create, launch, monitor and optimise Google Ads campaigns end to end from a brief, using the current Google Ads API generation.
Needs: P0, P1 (especially P1.5), P0.20.2 (developer token is the critical path), shared modules from P3 (creative pipeline, policy pre-check framework, experiment framework).

Facts to re-verify first (P4.1.1): latest API version (v25.2 on 2026-09-23), sunset of v22 on 2026-10-07 (never target it), unified `Goal` / `CampaignGoalConfig` resources replacing lifecycle goal services, `AssetGenerationService`, Performance Max and Demand Gen changes, `validate_only` behaviour. See `docs/external-platform-facts.md`.


## Build order (working backlog, updated 2026-09-30)

Verified on 2026-09-30 from Google's documentation: Google Ads API v25 (released 2026-07-22), current minor v25.2 (2026-09-23). Performance Max asset group limits: 3-15 headlines (30 chars), 1-5 long headlines (90), 2-5 descriptions (90), business name (25), 1-20 landscape images 1.91:1 (min 600x314, recommended 1200x628), 1-20 square images 1:1 (min 300x300, recommended 1200x1200), optional portrait 4:5 (min 480x600), 1-5 square logos (min 128x128), optional landscape logos 4:1 (min 512x128), up to 15 videos, every image at most 5,120 KB. **Video assets must be YouTube videos** (`YOUTUBE_VIDEO` asset with a YouTube video id); the Google Ads API cannot take a video file. Uploading to YouTube through the Data API (`videos.insert`, scope `youtube.upload`, 100 uploads per day) works, but uploads from unverified API projects created after 2020-07-28 are locked to private until the project passes YouTube's audit, so the first release links existing YouTube videos by URL or id and treats API upload as a later, audit-dependent step.

Each step ends with tests, the OpenAPI client regenerated, and the screen usable.

- [x] **G1 Asset library (provider-neutral, needed by every channel).** Upload images, logos and video files; import by URL; link YouTube videos; dedupe by hash; metadata (dimensions, duration, size, format); per-destination validation (Google Performance Max, Google logo, OpenAI chat card, Meta); one-click derived crops to the required ratios; tags, alt text, rights and licence notes, AI-generated flag; Assets screen with grid, detail and checks. See P4.15.
- [x] **G2 Connection.** Google OAuth (offline refresh token, `adwords` scope), workspace developer token in the vault, manager account (`login-customer-id`) and client account selection, health check, token refresh, `invalid_grant` handling. Needs the owner's developer token and OAuth client (P0.20.2).
- [x] **G3 REST client.** `googleads.googleapis.com/v25`, headers (`developer-token`, `login-customer-id`, bearer), GAQL `searchStream`, mutate calls, `validate_only`, error mapping (`GoogleAdsFailure`), quota tracking, rate limits, redacted logging, fixtures.
- [x] **G4 Structure and numbers sync.** Campaigns, ad groups, ads (responsive search), asset groups, assets, keywords, negatives, budgets, bidding strategies, conversion actions; daily metrics per ad/asset group/keyword; policy status and serving issues; change detection (`change_event`); local copy in the same tables as OpenAI Ads.
- [x] **G5 Dashboards.** The campaign, ad group, ad and asset group dashboards from OpenAI Ads, with Google-specific panels: ad strength, impression share lost (budget and rank), search terms, keywords with quality score, asset performance labels, policy findings.
- [x] **G6 AI analysis and suggestions.** Reviews of a campaign, ad group or asset group compared with the previous review; responsive search ad copy (15 headlines, 4 descriptions) in three versions; keyword and negative cleanup; search term mining; asset swaps for Performance Max; apply suggestions through actions.
- [x] **G7 Publish flow.** Actions with `validate_only` before approval: create a paused Search campaign (budget, campaign, ad group, keywords, responsive search ad in one atomic mutate), create a Performance Max campaign with asset group, activate, pause, update budget and bidding, add keywords and negatives, update ads, archive; verification by re-query; rollback.
- [x] **G8 Assets on Google.** Upload image assets from the library to Google (`AssetService`, image bytes), link to asset groups and campaigns, YouTube video assets by id, logos, business name, sitelinks, callouts, structured snippets, call and promotion assets; asset performance read back into the library.
- [ ] **G9 Conversions (audit done; enhanced conversions and offline import with `gclid` still open).** Conversion action audit, enhanced conversions for leads, offline conversion import with `gclid` (needs the inbound-event capture from P2.16).
- [ ] **G10 Alerts and reports (alerts, sync checks and alerts tab done; billing and quota alerts, daily digest and weekly narrative still open).** Disapprovals, limited serving, budget exhausted early, conversion drop, billing problem, quota and version sunset; daily digest and weekly narrative.

Everything above is tested only against an in-memory fake of the Google Ads REST API; nothing has run against a live account yet. Confirm field names with `validateOnly` on the first real account.

Owner inputs needed before G2 can be tested live: Google Ads developer token (with access level), an OAuth client (Google Cloud project with the Google Ads API enabled), the manager account id and the client account id, and the company's YouTube channel (for video assets).

---

## P4.1 Connection and access (L)
Needs: P0.7, P0.20.2.
- P4.1.1 [spike] Re-verify: current stable major and its sunset date, client library release and supported Python version, transport (gRPC vs REST) recommendation, developer token access levels and quotas, OAuth vs service account for manager accounts, test account behaviour, required scopes.
- P4.1.2 Auth.
  - [ ] OAuth 2.0 with offline refresh token for the owner's Google identity; store refresh token in the vault; handle `invalid_grant` by marking the connection expired.
  - [ ] Developer token stored separately (workspace-level secret), never exposed to browser.
  - [ ] Manager account (`login-customer-id`) selection and client account listing (`customer_client`).
- P4.1.3 Account discovery: accessible customers, hierarchy, currency, timezone, status, billing setup, conversion tracking status, linked GA4 and Merchant Center accounts.
- P4.1.4 Quota tracker: operations per day and per developer token access level; alerts near limits; backoff.
- Done when: all managed accounts are discovered with billing and conversion tracking status.

## P4.2 API client and version management (M)
Needs: P4.1.
- P4.2.1 Wrap the official client behind the `AdsProvider` port; keep vendor types out of the domain (map at the boundary).
- P4.2.2 Version pinning in `PROVIDER_API_VERSIONS`; automatic warning when within 90 days of sunset; upgrade runbook with the read-only smoke suite that must pass before switching.
- P4.2.3 Error mapping: `GoogleAdsFailure` details into the platform taxonomy (auth, quota exceeded, resource exhausted, policy violation, invalid argument with field path, mutate partial failure).
- P4.2.4 Retry policy for transient gRPC / HTTP errors, idempotency for mutates via temporary resource ids and pre-checks by name / reference (Google has no generic idempotency header; design accordingly).
- P4.2.5 Request logging (redacted), request id capture for support cases.
- Done when: adapter passes the shared `AdsProvider` contract suite against fixtures and the test account.

## P4.3 Structure sync (L)
Needs: P4.2.
- P4.3.1 GAQL-based sync of campaigns, ad groups, ads, assets and asset groups, keywords and negatives, bidding strategies (portfolio and standard), budgets (shared vs campaign), conversion actions, audiences and user lists, extensions / assets at all levels, labels, geo and language targets, schedules, devices.
- P4.3.2 Campaign type coverage plan: Search first, then Performance Max, Demand Gen, Shopping (if a Merchant Center exists), Display and Video only if requested.
- P4.3.3 Incremental sync using `change_status` / `change_event` resources; deletion and removal handling; status vs serving status vs primary status reasons.
- P4.3.4 Policy data: `policy_summary`, approval status, topic entries, and limited-serving reasons into `AdPolicyIssue`.
- P4.3.5 Recommendations ingestion (`recommendation` resource) as suggestions input to the agent (never auto-applied).
- Done when: structure and statuses match the Google Ads UI for a real account.

## P4.4 Reporting ingestion (L)
Needs: P4.3, P0.5.4.
- P4.4.1 Daily jobs using GAQL via search stream: campaign, ad group, ad, keyword, asset, asset group, search term, geographic, device, hour-of-day, audience, landing page, conversion action segmented reports.
- P4.4.2 Metrics list: impressions, clicks, cost, conversions, conversion value, all conversions, view-through conversions where relevant, impression share and lost share (budget / rank), CTR, average CPC, quality score components (Search), asset performance labels, PMax insights (`campaign_search_term_insight`, asset group signals).
- P4.4.3 Restatement window (default 30 days) because conversions and conversion delays; store conversion action breakdown so primary vs secondary goals stay distinguishable.
- P4.4.4 Currency and timezone normalisation; cost micros handling.
- P4.4.5 Data integrity checks vs UI totals; freshness alerts.
- Done when: last 90 days reconcile with the Google Ads UI for cost and conversions within tolerance.

## P4.5 Campaign planning (XL → split)
Needs: P4.3, P1.10, P1.6.
- P4.5.1 Brief-to-plan agent producing a neutral `CampaignPlan` with Google specifics.
- P4.5.2 Campaign type selection with rationale (intent capture → Search; broad reach and automation → Performance Max; visual discovery → Demand Gen; product feeds → Shopping / PMax retail).
- P4.5.3 Goals and conversions: unified goal configuration (v25 `Goal` / `CampaignGoalConfig`), choose primary conversion actions, value rules, new-customer acquisition options, verify conversion tracking exists (tag, GA4 import, enhanced conversions, offline import); block launch on broken measurement.
- P4.5.4 Budgeting and bidding: daily budget, shared budget rules, bidding strategy (maximise conversions, target CPA, maximise conversion value, target ROAS, manual CPC only for cold-start), learning period expectations, minimum conversion volume checks, seasonality adjustments.
- P4.5.5 Search structure: themed ad groups (or AI Max / broad match settings decision with rationale), keyword research from Keyword Planner / third-party data, match type strategy, negatives (account, campaign, shared lists), brand vs non-brand separation, search themes.
- P4.5.6 Performance Max structure: asset groups by theme, audience signals from first-party data, URL expansion and final URL rules, brand exclusions, negative keyword lists, asset requirements (text, images, logos, videos) and automation options (video from URL, asset enhancements) with recorded choices.
- P4.5.7 Demand Gen structure: ad formats, audiences, lookalike-like signals, creative requirements.
- P4.5.8 Targeting: geo (presence vs interest option), languages, schedules, devices, audience segments, demographics exclusions where appropriate.
- P4.5.9 Assets: sitelinks, callouts, structured snippets, call, location, price, promotion, lead form; business name and logo; image assets; each with policy notes.
- P4.5.10 Tracking: final URL suffix / tracking template with ValueTrack parameters and UTM alignment with GA4.
- P4.5.11 Risk assessment and naming conventions (shared engine from P3.5.9).
- Done when: plans for Search and PMax briefs are accepted by a human specialist without structural edits in an evaluation set.

## P4.6 Copy and assets (L)
Needs: P4.5, P3.8.
- P4.6.1 Responsive Search Ad generation: up to the maximum headlines and descriptions with pinning strategy only when needed; keyword insertion rules; diversity constraints; character limits; policy checks (P4.7).
- P4.6.2 PMax / Demand Gen asset generation: headlines, long headlines, descriptions, images in required ratios, logos, videos; optional `AssetGenerationService` evaluation with human review and synthetic-content attestation handled through the fields exposed in v25.
- P4.6.3 Localisation per language and market; dynamic parameters; countdown / promo assets only with factual constraints.
- P4.6.4 Landing page alignment check: message match between keywords, ads and landing page; page speed and mobile checks from P2.
- P4.6.5 Ad strength awareness: read ad strength and asset performance labels, iterate, without gaming labels at the cost of quality.
- Done when: every generated asset set passes character limits, policy pre-check and landing-page message-match checks.

## P4.7 Policy pre-check (M)
Needs: P4.6, shared framework from P3.7.
- P4.7.1 Google Ads policy rule set (restricted content, editorial and technical requirements, trademarks, healthcare / financial restrictions, misrepresentation, destination requirements) versioned.
- P4.7.2 API-side validation via `validate_only` mutate to catch policy findings and exemptions requirements; record `PolicyFindings` with topics and evidence.
- P4.7.3 Exemption workflow: never auto-request exemptions; surfaced to humans with rationale.
- P4.7.4 Post-launch monitoring of disapprovals and limited serving with fix proposals.
- Done when: seeded violations fail validation with explainable messages; real disapprovals generate fix proposals.

## P4.8 Publish flow (L)
Needs: P4.2–P4.7, P1.5.
- P4.8.1 Action kinds: `ads.google.create_campaign_paused`, `activate`, `pause`, `update_budget`, `update_bidding`, `add_keywords`, `add_negatives`, `update_ads`, `add_assets`, `update_targeting`, `archive`, `create_conversion_action`, `apply_recommendation`.
- P4.8.2 Creation strategy: use atomic multi-resource mutates where the API supports them (temporary ids, one request for budget + campaign + criteria) with `partial_failure` disabled for creation so it is all-or-nothing; validate first with `validate_only`.
- P4.8.3 Verification by re-query and comparison with plan; check serving status, policy status, budget, geo, bidding, conversion goals.
- P4.8.4 Activation gate: billing OK, budget within caps, conversion tracking verified, no blocking policy findings, brand safety exclusions applied.
- P4.8.5 Rollback: pause or remove created resources; change history capture via `change_event` for audit reconciliation (detects edits made outside the platform).
- Done when: a Search campaign and a PMax campaign are created paused, verified, activated with approval, and rolled back cleanly in the test account.

## P4.9 Optimisation engine (XL → split)
Needs: P4.4, P4.8, P3.11 shared framework.
- P4.9.1 Rules layer: budget-limited campaigns (impression share lost to budget) vs rank-limited, bid strategy target adjustments within a percentage cap and cool-down, pause wasteful keywords / ads / placements, add negatives from search-term mining, respect learning periods.
- P4.9.2 Search term mining: classify search terms (relevant, irrelevant, brand, competitor, question) with LLM plus rules, propose negatives and new exact / phrase keywords, group by theme; caution on low-volume noise.
- P4.9.3 Performance Max analysis: asset group and asset performance, search term categories, channel breakdown when available, audience signal effect, brand cannibalisation checks; propose asset swaps and exclusions.
- P4.9.4 Quality Score and ad relevance improvement proposals (Search).
- P4.9.5 Geo, device, schedule, audience bid adjustments proposals from data with significance checks.
- P4.9.6 Budget reallocation across campaigns by marginal efficiency within monthly caps.
- P4.9.7 Recommendations triage: evaluate Google's own recommendations with the agent (accept only with evidence; reject by default those that raise spend without proven benefit).
- P4.9.8 Anomaly detection and pacing (shared engine from P3.11.4–5) plus conversion tracking break detection.
- P4.9.9 Outcome linking and learning to memory (shared P1.7).
- Done when: backtests beat do-nothing on cost per conversion within guardrails and every proposal is explainable.

## P4.10 Conversion tracking and offline feedback (L)
Needs: P2.16, P4.1.
- P4.10.1 Conversion action audit: duplicates, primary vs secondary, attribution model, windows, value settings; propose corrections.
- P4.10.2 GA4 import vs native tag decision support; verify tag firing via website checks.
- P4.10.3 Enhanced conversions for leads: hashed first-party data with consent, upload via API; capture `gclid` at the website (P2.16.2) and store with the inbound event.
- P4.10.4 Offline conversion import when the owner marks lead outcomes (qualified, won, value) — optional, consent-aware, deduplicated.
- P4.10.5 Conversion delay analysis for optimisation timing.
- Done when: a website lead with a `gclid` is uploaded as a conversion and appears in reporting.

## P4.11 Experiments (M)
Needs: P4.8, shared P3.12.
- P4.11.1 Google experiment resources (campaign experiments, ad variations) via API where supported; otherwise controlled manual splits.
- P4.11.2 Design, execution, analysis, learning capture as in P3.12.
- Done when: an experiment runs and produces a documented decision.

## P4.12 Alerts and reports (M)
Needs: P4.4, P1.9.
- P4.12.1 Alert rules: disapprovals, limited serving, budget exhausted early, conversion drop, tracking failure, billing issue, suspended account, unusual search terms, quota nearing limit, API version nearing sunset.
- P4.12.2 Daily digest and weekly narrative in the user's language with evidence and recommendations.
- Done when: each alert type is reproducible in test and delivers actionable text.

## P4.13 Google Ads menu UI (L)
Needs: P0.13, P4.3–P4.12.
- P4.13.1 Overview, campaign explorer (tree with metrics), campaign detail (structure, assets, keywords, search terms, policy, change history), experiments, recommendations triage, settings (connection, accounts, guardrails, naming, tracking templates, autonomy).
- P4.13.2 Search terms workbench with bulk negative / keyword proposals.
- P4.13.3 Catalog strings, e2e for create → approve → activate → monitor.
- Done when: every P4 capability is operable from the Google Ads menu.

## P4.14 Testing and safety (M)
Needs: all of P4.
- P4.14.1 Test account strategy (Google test manager accounts have restrictions; document behaviour) plus low-cap real sandbox account for smoke.
- P4.14.2 Fixture library for errors, quota, policy findings, partial failures.
- P4.14.3 Drills: `invalid_grant`, quota exhausted mid-run, worker crash between mutates, API version switch dry run.
- Done when: drills pass with consistent state.

## P4.15 Asset library and media (L)
Needs: P0, P0.7 (vault), shared by P3, P4, P5.
- P4.15.1 Storage port with a local-disk adapter now and an S3-compatible adapter later; files are private and served only through authenticated endpoints; short-lived signed public links for platforms that fetch a URL (OpenAI image upload).
- P4.15.2 Upload: multipart with size and type limits, magic-byte verification (images verified by decoding, no SVG), SHA-256 dedupe per workspace, SSRF-safe import from a URL, YouTube link by URL or id with title and thumbnail read from the public oEmbed endpoint.
- P4.15.3 Metadata: dimensions and aspect ratio, video duration and dimensions read from the MP4/MOV header without external tools, size, format, tags, title, alt text, rights (owned, licensed, stock, unknown), licence note and expiry, AI-generated flag, source.
- P4.15.4 Destination specs as data: Google Performance Max marketing image (1.91:1), square (1:1), portrait (4:5), square logo, landscape logo (4:1), 5,120 KB cap; OpenAI chat card image (at least 640x640, JPEG/PNG/WebP); Meta feed and story ratios; each check returns pass or fail with the reason.
- P4.15.5 Derived crops: from one source image create the required ratios at the recommended size by centre crop or a chosen focal point, re-encoded under the size cap, linked to the source, never replacing it.
- P4.15.6 API: list with filters (kind, search, tag, destination fit), detail, update metadata, delete (blocked while an asset is used by a live campaign), specs endpoint.
- P4.15.7 Screen: upload by drag and drop with progress, YouTube link form, grid with thumbnails, filters, detail panel with checks per destination, derive buttons, delete.
- P4.15.8 Later: YouTube upload through the Data API once the API project is audited; image generation with human review; brand-safe stock search; usage tracking per campaign and expiry alerts for licensed assets.
- Done when: one uploaded photo yields every Performance Max image ratio that passes the checks, a YouTube video can be linked, and both can be selected from the library when building a campaign.
