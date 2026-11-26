# P9 — Unified Reporting and Attribution

Goal: one trustworthy view across ads, organic, website and inbound events, with honest attribution, an executive briefing, and cross-channel budget guidance.
Needs: P2, P3, P4, P5, P6, P7 data (can start once two channels are ingesting).

---

## P9.1 Measurement framework (M) [decision: KPIs]
- P9.1.1 Define business KPIs with the owner: qualified contacts, cost per contact, pipeline signals available without a CRM (manual qualification flag), organic visibility, AI citation share, social reach and engagement, revenue proxy if available.
- P9.1.2 Metric dictionary: one definition per metric, source of truth, formula, time zone, attribution basis, known caveats; versioned and shown in the UI beside every number.
- P9.1.3 Data freshness policy per source and visible "as of" timestamps.
- Done when: the dictionary is approved and referenced by all dashboards.

## P9.2 Taxonomy and tagging standard (S)
Needs: P9.1.
- P9.2.1 UTM taxonomy (`utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`, plus `platform`, `market`, `objective`) with allowed values and naming conventions per platform including OpenAI Ads.
- P9.2.2 Enforcement: builders from P3–P5 generate URLs from the taxonomy; a validator rejects non-conforming URLs; WordPress and social posts follow the same rules.
- P9.2.3 Click-id capture (`gclid`, `fbclid`, `msclkid`, LinkedIn `li_fat_id` if present, OpenAI ads identifier if provided) stored on inbound events (P2.16).
- Done when: 100% of platform-created URLs pass validation.

## P9.3 Warehouse-style modelling in Postgres (L)
Needs: P0.5.4, ingestion from P2–P7.
- P9.3.1 Dimensional model: `dim_date`, `dim_channel`, `dim_campaign`, `dim_content`, `dim_market`; fact tables for spend, results, sessions, contacts, social metrics, AI visibility.
- P9.3.2 Materialised views and incremental refresh jobs; indexes; retention and roll-ups; performance budgets for dashboard queries.
- P9.3.3 Currency normalisation (single reporting currency, daily rates source recorded), timezone normalisation, restatement handling with lineage.
- P9.3.4 Data quality suite (completeness, freshness, reconciliation to source UIs, duplicate detection) run daily; failures visible on a data health page.
- Done when: the model reconciles with each platform within tolerance and refreshes incrementally.

## P9.4 Attribution (L)
Needs: P9.2, P9.3, P2.16.
- P9.4.1 Attribution views: platform-reported, first-touch, last-touch, and position-based on first-party events; clear labelling of differences and double-counting risk.
- P9.4.2 Contact-level journeys built from UTM, click ids and landing pages (no identity resolution beyond what consent allows).
- P9.4.3 Incrementality helpers: geo or time-based holdout tests, before/after with controls, marketing mix modelling only when data volume justifies (documented threshold).
- P9.4.4 Confidence and uncertainty shown in reports; no false precision.
- Done when: the same contact set produces explainable numbers per model and reports show reconciliation notes against platform figures.

## P9.5 Dashboards (L)
Needs: P9.3, P0.14.
- P9.5.1 Executive overview: spend, contacts, cost per contact, trend, top movers, pacing, alerts, agent activity, approvals waiting.
- P9.5.2 Channel comparison: efficiency, volume, quality, saturation, with caveats.
- P9.5.3 Content and SEO view: articles impact on traffic and contacts, AI visibility.
- P9.5.4 Social view: reach, engagement, growth, best content.
- P9.5.5 Filters (date range, market, channel, campaign), saved views, comparison periods, drill-down to source, CSV export, share links with expiry and permissions.
- P9.5.6 Accessibility of charts (text summaries, keyboard, colour-blind palettes), consistent formatting of numbers and dates.
- Done when: dashboards load within performance budgets and every number links to its definition and source.

## P9.6 Reports and briefings (M)
Needs: P9.5, P1.9.
- P9.6.1 Daily brief (short): what changed, what needs approval, what broke.
- P9.6.2 Weekly report: performance narrative, wins and losses, experiments, learnings added to memory, recommended actions as proposals.
- P9.6.3 Monthly business review: goals vs actuals, budget efficiency, market context (P8), strategic recommendations, risks.
- P9.6.4 Narrative generation grounded in data (every claim links to a metric and period), numeric consistency checks between narrative and tables, locale-aware output, PDF and shareable web versions.
- P9.6.5 Report evaluation: factual accuracy checks against source data, owner ratings feeding memory.
- Done when: reports are generated on schedule, numerically consistent and rated useful by the owner.

## P9.7 Budget allocation guidance (M)
Needs: P9.4, P3.11, P4.9, P5.10.
- P9.7.1 Cross-channel efficiency analysis and marginal return estimates with uncertainty.
- P9.7.2 Budget reallocation proposals within monthly caps, respecting minimum spend per channel for learning, always as R3 proposals requiring approval.
- P9.7.3 Scenario planner ("what if we shift 20% from Meta to Google").
- Done when: proposals include evidence, uncertainty and expected impact ranges.

## P9.8 Alerting and anomaly layer (S)
Needs: P9.3.
- P9.8.1 Cross-channel anomaly detection (contacts drop while spend steady, tracking break across platforms, sudden traffic source shifts).
- P9.8.2 Alert routing and deduplication with per-channel alerts from P3–P7.
- Done when: a simulated tracking break is detected once and routed correctly.
