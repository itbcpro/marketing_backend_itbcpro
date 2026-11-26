# P3 — Meta Ads (Marketing API)

Goal: from one sentence in the command box to a live, monitored, optimised Meta campaign, with every step planned, validated, approved and audited.
Needs: P0, P1 (P1.5 actions is mandatory), P0.20.1 (approvals are the critical path).

Facts to re-verify first (P3.1.1): API v26.0 (released 2026-07-29), Advantage+ unified structure (three automation levers), removed placements and poll ads, metric retirements, access tiers, webhook mTLS CA. See `docs/external-platform-facts.md`.

---

## P3.1 Connection and access (L)
Needs: P0.7, P0.20.1.
- P3.1.1 [spike] Re-verify: current version and changelog, permission names and review requirements, system-user token behaviour and expiry, app modes (development vs live), Access Tier thresholds, Business Manager requirements, sandbox and test ad account options. Update the facts file.
- P3.1.2 Auth flows.
  - [ ] System-user long-lived token path for server-to-server ads management (preferred), stored in the vault with scope and asset listing.
  - [ ] OAuth (Facebook Login for Business) path for user-authorised assets where needed (pages, Instagram), with token exchange and refresh handling; `appsecret_proof` on every call.
  - [ ] Token debug endpoint check job (validity, expiry, scopes, granting user) with alerts before expiry.
- P3.1.3 Asset discovery: Business Manager, ad accounts (status, currency, timezone, spend cap, funding source status), pages, Instagram accounts, pixels / datasets, catalogs, custom audiences; map into `Connection` sub-resources with capability flags.
- P3.1.4 Permission gap detector: compare granted vs required scopes per feature; UI explains what is missing and how to fix it.
- P3.1.5 Access Tier tracker: count of calls and error rate over the rolling window used by Meta; surface progress toward Full access and warn when error rate threatens eligibility.
- Done when: the platform lists all ad accounts and assets, detects a revoked token within one health cycle and explains missing permissions.

## P3.2 API client (L)
Needs: P3.1, P0.7.3.
- P3.2.1 Thin typed client around Graph API: version from config, base URL, field selection, edge pagination (cursor), batch requests (up to the documented limit), async report jobs, upload endpoints (images, videos with chunked upload), error parsing into the platform taxonomy.
- P3.2.2 Rate-limit handling: parse `X-Business-Use-Case-Usage`, `X-Ad-Account-Usage`, `X-App-Usage` headers, per-account throttling state in the registry, backoff on error codes for throttling, queue prioritisation (writes over analytics).
- P3.2.3 Error code map: auth expired, permission missing, rate limited, invalid parameter (with `error_subcode` and user message), policy rejection, transient; each mapped to retry / repair / fail behaviour.
- P3.2.4 Request logging with redaction; sample payload capture for fixtures.
- P3.2.5 Version compatibility layer: field renames and removals isolated in one module keyed by version; automated check that runs a read-only smoke suite on a new version before switching config.
- Done when: client passes contract tests against recorded fixtures for v26.0 and survives simulated throttling.

## P3.3 Structure sync and normalisation (L)
Needs: P3.2, P1 domain models.
- P3.3.1 Mapping Meta objects to neutral model: campaign, ad set (`AdGroup`), ad, creative, audience, pixel / dataset, conversion events; `ExternalEntityMap` for ids.
- P3.3.2 Full initial sync and incremental sync (updated_time filter), deletion / archival detection, effective status vs configured status handling.
- P3.3.3 Store Advantage+ state (`advantage_state` and the three lever states) and objectives (outcome-driven objectives), budget type (campaign budget vs ad set budget), bid strategy, optimisation goal, attribution setting, learning-phase status.
- P3.3.4 Policy and delivery issues ingestion (`issues_info`, `ad_review_feedback`, `effective_status`) into `AdPolicyIssue`.
- Done when: the neutral model of an existing real account matches Ads Manager for structure and statuses.

## P3.4 Insights ingestion (L)
Needs: P3.2, P3.3, P0.5.4.
- P3.4.1 Daily job per ad account pulling insights at campaign / ad set / ad level with a defined field list (spend, impressions, reach where still valid, clicks, link clicks, CTR, CPC, CPM, results / actions, cost per result, ROAS, video metrics, frequency).
- P3.4.2 Breakdowns: date, placement, platform, device, age, gender, country, creative asset; async report jobs for large pulls; hourly only where needed (retention limits).
- P3.4.3 Restatement window: re-pull the last N days (default 28) daily because conversions arrive late; upsert semantics; record data completeness flags.
- P3.4.4 Attribution handling: store attribution setting used in each pull; only request windows still supported; document differences from Ads Manager UI.
- P3.4.5 Metric-retirement compatibility: map retired metrics to Media Views / Media Viewers equivalents in organic (P6); keep provider-native raw payload.
- P3.4.6 Backfill tool (bounded by retention limits), integrity checks (sum of children equals parent within tolerance), freshness alerts.
- Done when: last 90 days match Ads Manager within an agreed tolerance for spend and results; late conversions are reflected after restatement.

## P3.5 Campaign builder — planning (XL → split)
Needs: P3.3, P1.10, P1.6.
- P3.5.1 Brief-to-plan agent: from `CampaignBrief` produce a `CampaignPlan` (neutral structure) using memory (brand rules, past winners) and account state.
- P3.5.2 Objective mapping: business goal → outcome-driven objective (leads, sales, traffic, awareness, engagement, app promotion) with conversion location (website, instant form, calls, messaging) and rationale.
- P3.5.3 Advantage+ strategy: decide which of the three levers to enable, when to keep manual control (for example strict audience or placement constraints, compliance), and record why.
- P3.5.4 Budget and bidding: campaign vs ad set budget, daily vs lifetime, bid strategy selection (lowest cost, cost cap, bid cap, minimum ROAS) with numeric parameters derived from history or safe defaults; learning-phase aware minimums (sufficient budget for conversion volume); pacing and schedule; guardrail compliance.
- P3.5.5 Targeting: geo (with location types), languages, age and gender constraints only where allowed, interests and behaviours as suggestions (Advantage+ audience signals), custom audiences and lookalikes (respecting v26 lookalike spec changes), exclusions (existing customers, converters), special ad categories (housing, employment, credit, social issues) detection with required restrictions.
- P3.5.6 Placements: default automatic; manual placements only with reasons; remove unavailable placements (Instagram Explore, Messenger Stories); format compatibility check per placement.
- P3.5.7 Conversion setup: pixel / dataset, standard events, custom conversions, Conversions API status check, event match quality thresholds, domain verification and aggregated event configuration status; block launch when conversion tracking is broken for conversion objectives.
- P3.5.8 Creative plan: number of variants, formats (image, video, carousel, collection, instant experience), aspect ratios (1:1, 4:5, 9:16), copy angles, CTA button, destination URL with UTM template (`utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term` plus platform macros), lead form questions and privacy policy link.
- P3.5.9 Naming convention engine (campaign / ad set / ad names encode objective, market, audience, date, version) and label / tag strategy for later analysis.
- P3.5.10 Plan risk assessment: budget exposure, policy risk (claims, sensitive categories), learning-phase risk, tracking risk; risk class assigned.
- Done when: a brief produces a plan that a human media buyer would accept without structural edits in an evaluation set.

## P3.6 Campaign builder — creative and copy (L)
Needs: P3.5, P3.8.
- P3.6.1 Copy generation per variant: primary text (multiple lengths), headline, description, CTA, in the market language; brand voice and banned-claim compliance; character limits per placement; emoji and formatting rules.
- P3.6.2 Localisation: adapt (not translate) per market; glossary enforcement; text handling for right-to-left scripts when a market brief requires them.
- P3.6.3 Variant strategy: angle diversity (pain, benefit, proof, offer, urgency where permitted), avoid near-duplicates, tag each variant with hypothesis for later learning.
- P3.6.4 Asset selection: existing library assets vs newly generated (P3.8); safe-zone checks (text overlay areas for Stories / Reels), aspect ratio coverage, file constraints.
- P3.6.5 Dynamic creative / Advantage+ creative options: which enhancements to allow (text improvements, image enhancements, music, etc.), default off for sensitive brand elements, recorded per plan.
- P3.6.6 Preview generation: Meta ad preview endpoints (`generatepreviews`) for each placement; screenshots stored with the action for review.
- Done when: every variant passes length, policy pre-check (P3.7) and preview generation for its placements.

## P3.7 Policy and compliance pre-check (M)
Needs: P3.6, P1.5.3.
- P3.7.1 Rule sets: Meta advertising standards summary as configurable rules (personal attributes, before/after claims, misleading claims, prohibited and restricted content, financial and health claims, sensitive categories, landing page requirements) with versioning.
- P3.7.2 LLM policy reviewer with structured findings and citations to the rule; false-positive tracking.
- P3.7.3 Landing page check: reachable, fast, matches the ad promise, policy and contact pages present, no broken redirects, tracking present.
- P3.7.4 Brand and legal rules from memory (banned claims, disclaimers, regulated industries).
- P3.7.5 Post-submission monitoring: read review feedback, categorise rejections, propose fixed variants (as new actions), record learnings.
- Done when: seeded violating ads are flagged before submission; real rejections generate a fix proposal.

## P3.8 Creative asset pipeline (L) [decision: generation vendors]
Needs: P0.20.6, P1.
- P3.8.1 Asset library: storage, metadata (rights, source, generated or human, model / prompt used, licence), tagging, search, dedupe by perceptual hash, usage tracking, expiry of licensed assets.
- P3.8.2 `MediaGenerator` port and adapters for image and video vendors chosen by spike (capabilities, cost, latency, quality, licensing, content policy, watermarking / disclosure requirements).
- P3.8.3 Image workflow: prompt from creative brief and brand visual notes, generate variants, upscale, background clean-up, resize to required ratios, add text overlays programmatically (brand fonts, contrast checks, right-to-left script support when required), compress, quality checks (artifacts, hands / faces if any, logos correctness, text legibility).
- P3.8.4 Video workflow: script → storyboard → generation or template rendering → subtitles (auto-generated, reviewed, burned-in and sidecar) → audio (licensed music only, or none) → export per placement; duration and file size limits.
- P3.8.5 Human-made asset ingestion (uploads) with validation.
- P3.8.6 AI-content disclosure handling: labels or metadata where platform policies require (Meta AI-generated content labelling, Google synthetic content attestation).
- P3.8.7 Brand safety and rights checks: no real people likeness without consent, no competitor trademarks, no copyrighted characters; automated scans plus human approval for the first N assets of each type.
- P3.8.8 Cost tracking per generated asset and per campaign.
- Done when: from a creative brief the pipeline yields approved, correctly sized, correctly disclosed image and short video assets with recorded provenance.

## P3.9 Preview, review and approval UX (M)
Needs: P3.5–P3.8, P1.5.5, P1.12.
- P3.9.1 Plan review screen: structure tree, budget summary and exposure, targeting summary, placement list, conversion setup status, risk flags, cost of AI work.
- P3.9.2 Creative review: per-placement previews, copy variants side by side, edit inline, regenerate one element, approve subsets.
- P3.9.3 Approval decision capture with reasons feeding memory; edits produce diffs.
- P3.9.4 Two-step approvals: "create paused" (R1) and "activate" (R2/R3 depending on budget) as separate actions.
- Done when: an approver can review, edit, approve creation, then separately approve activation.

## P3.10 Publish flow (L)
Needs: P3.2–P3.9, P1.5.6.
- P3.10.1 Action kinds: `ads.meta.create_campaign_paused`, `ads.meta.activate`, `ads.meta.pause`, `ads.meta.update_budget`, `ads.meta.update_bid`, `ads.meta.update_targeting`, `ads.meta.replace_creative`, `ads.meta.archive`, `ads.meta.duplicate`, `ads.meta.create_audience`.
- P3.10.2 Creation order and atomicity: upload assets → create creatives → campaign → ad sets → ads, all paused; track created ids after each step so a failure halts and cleans up (compensating actions) rather than leaving partial structures; idempotency via external reference / naming and stored ids.
- P3.10.3 Validation before creation using Meta's validation option (`execution_options` validate-only where supported) or equivalent dry run.
- P3.10.4 Post-creation verification: re-read every object, compare with plan (budget, targeting, placements, status), verify pixel and events, generate previews, check for immediate policy issues.
- P3.10.5 Activation checklist gate: payment method active, spend cap sufficient, account status healthy, conversion tracking OK, review status not rejected, schedule sanity; then activation by approved action.
- P3.10.6 Initial monitoring window: increased check frequency for first 72 hours, early-warning rules (no delivery, disapproval, extreme CPC).
- P3.10.7 Rollback and cleanup: pause, archive, delete drafts created by failed runs.
- Done when: creating and activating a real low-budget campaign in a test / low-risk account succeeds with verification and a clean audit trail; injected failures leave no orphan objects.

## P3.11 Optimisation engine (XL → split)
Needs: P3.4, P3.10, P1.6, P1.7.
- P3.11.1 Rules layer (deterministic, explainable): pause ads with spend above threshold and no results, pause ads with high frequency and falling CTR (creative fatigue), shift budget between ad sets by efficiency within caps, respect learning phase and minimum data thresholds, protect recently changed entities from further changes (cool-down), never edit more than X% budget per day.
- P3.11.2 Statistical layer: significance and uncertainty (Bayesian or sequential tests) for comparing ads and audiences, avoid decisions on noise, adjust for delivery bias.
- P3.11.3 Agent layer: interprets the rules output plus context (seasonality, promotions, creative history, memory), proposes actions with rationale and expected effect; can propose new creative tests and audience tests.
- P3.11.4 Anomaly detection: spend spikes, delivery drops, CPA jumps, tracking breaks (conversions vanish), pacing errors; severity levels and immediate alerts.
- P3.11.5 Budget pacing and forecast: end-of-month projection vs cap, under / over-delivery corrections.
- P3.11.6 Creative rotation and refresh proposals: identify fatigue, propose new variants using winners' attributes from outcome memory.
- P3.11.7 Learning from outcomes: link every optimisation action to outcome windows, evaluate whether it helped, store in memory; auto-demote rules with poor track record.
- P3.11.8 Explainability: each proposal shows data used, rule or reasoning, alternatives considered, expected impact and risk.
- Done when: on a backtest of historical data the engine's proposals beat a do-nothing baseline on cost per result without breaching guardrails, and every proposal has an explanation.

## P3.12 Experimentation (M)
Needs: P3.10, P3.11.2.
- P3.12.1 Test design: hypothesis, variable (creative, audience, placement, bid strategy), split method (Meta A/B test object vs manual ad sets with equal budgets), duration, minimum sample, success metric.
- P3.12.2 Test execution actions (create test, stop, declare winner) through the action layer.
- P3.12.3 Result analysis and learning capture to outcome memory; winners flow into creative planning.
- Done when: an A/B test runs to completion and its learning appears in memory with evidence.

## P3.13 Conversions API and tracking health (L)
Needs: P2.16, P3.1.
- P3.13.1 Server-side events from website (leads, contacts, purchases if any) with `event_id` deduplication against browser pixel, hashed user data per requirements, consent gating, event source url, action source.
- P3.13.2 Event quality monitoring: event match quality, deduplication rate, delayed events, failed events; alerts.
- P3.13.3 Test events workflow during setup; domain verification and aggregated events configuration checks.
- P3.13.4 Offline / CRM-free lead quality feedback: mark qualified leads manually in the platform and send back as conversion events (optional, consent-aware).
- Done when: website lead events appear in Events Manager with deduplication and acceptable match quality.

## P3.14 Webhooks and near-real-time updates (M)
Needs: P0.16.7, P3.1.
- P3.14.1 Webhook endpoint: verification handshake, signature check, replay protection, trust store update for Meta's outbound CA certificate, idempotent processing.
- P3.14.2 Subscriptions relevant to ads: ad account changes, leadgen (if instant forms used), campaign / ad status and review changes where available.
- P3.14.3 Fallback polling when webhooks are delayed; reconciliation job.
- P3.14.4 Lead retrieval from instant forms into `InboundEvent` with consent flags and notification.
- Done when: an ad disapproval or a new lead is reflected in the platform within one minute.

## P3.15 Alerts and reports (M)
Needs: P3.4, P1.9, P9.
- P3.15.1 Daily Meta digest: spend vs pacing, results, top and bottom performers, changes made by agents, issues.
- P3.15.2 Alert rules: disapproved ad, no delivery, spend anomaly, pixel problems, payment failure, account restricted, token expiring, quota nearing limits.
- P3.15.3 Weekly performance narrative in the user's language with evidence and recommendations.
- Done when: a simulated disapproval and a simulated payment failure each trigger the correct alert with next steps.

## P3.16 Meta Ads menu UI (L)
Needs: P0.13, P3.3–P3.15.
- P3.16.1 Overview: KPIs, pacing, alerts, agent activity, pending approvals.
- P3.16.2 Campaign explorer: tree table with metrics, filters, breakdowns, compare periods, saved views.
- P3.16.3 Campaign detail: structure, creatives with previews, history of changes (by human or agent), issues, experiments.
- P3.16.4 New campaign flow (link to the command box and the plan review screen).
- P3.16.5 Settings: connection, assets, conversion setup status, guardrails specific to Meta, naming templates, UTM templates, autonomy settings for the Meta ads agent.
- P3.16.6 Strings in the catalog; e2e for create → approve → activate → monitor.
- Done when: every P3 capability is operable from the Meta Ads menu.

## P3.17 Testing and safety (M)
Needs: all of P3.
- P3.17.1 Sandbox strategy: dedicated test ad account with tiny caps; nightly contract smoke; never run destructive tests on production accounts.
- P3.17.2 Fixture library covering all statuses, errors, throttling responses, policy rejections, partial creation failures.
- P3.17.3 Simulation-mode dry run of full daily cycle on real read-only data.
- P3.17.4 Failure drills: token revoked mid-run, throttled during creation, worker crash between creative and ad creation.
- Done when: all drills pass and leave the account in a consistent state.
