# P2 — WordPress, Content Intelligence, SEO and GEO

Goal: the platform understands the whole WordPress site, audits it continuously for classic SEO and AI-search (GEO) readiness,
produces new articles that are consistent with existing content, refreshes old ones, publishes safely, and tracks every inbound contact event.
Needs: P0, P1.1–P1.5 (P1.6 memory for brand and style).

Verified starting point: the site already exposes 78 WordPress MCP abilities (posts, pages, CPT, media, comments, Rank Math / Yoast / SEOPress,
Polylang / WPML, Elementor, WP Mail SMTP logs, Meta Box, JetEngine). See `docs/external-platform-facts.md`.

---

## P2.1 WordPress connector (`CmsProvider`) (L)
Needs: P0.7, P0.9.
- P2.1.1 [spike] Transport decision: WordPress MCP adapter endpoint vs REST API with application passwords vs a small companion plugin. Criteria: coverage, auth model, rate limits, webhooks, stability. Output: ADR, default = both behind one port with REST as fallback.
- P2.1.2 Connection setup UI and health check: site URL, credentials, capabilities probe, WordPress and PHP versions (`core/get-environment-info`), timezone, permalink structure, HTTPS check.
- P2.1.3 Capability discovery: list abilities / endpoints, active plugins (`get-active-plugins`), detect SEO plugin (Rank Math / Yoast / SEOPress), multilingual plugin (Polylang / WPML), page builder (Elementor version and mode), form plugins, cache plugins, SMTP logging plugin, analytics plugins; store as `SiteProfile`.
- P2.1.4 Port implementation.
  - [ ] Read: posts, pages, CPT items, taxonomies, media, comments, users (roles only), site stats.
  - [ ] Write: create/update draft, publish, schedule, trash, meta, taxonomies, featured image, media upload from URL, comments moderation and reply.
  - [ ] SEO plugin adapters behind `SeoMetaPort`: get/set title, description, focus keyword, canonical, robots, OG/Twitter, schema (Rank Math schema write), redirect support if available.
  - [ ] Multilingual adapters behind `TranslationPort`: language of post, translation map, link translations.
  - [ ] Elementor adapter: structure read (`get-page-structure`), text extraction, safe element edits (`manage-elements`), preview link, publish; never overwrite unknown widgets.
- P2.1.5 Safety features: every write first stores a `ContentVersion` snapshot; use WordPress revisions where available; dry-run diff; conflict detection using `modified_gmt` and content hash (do not overwrite edits made by humans since our last read).
- P2.1.6 Error normalisation, retries, rate limiting (be gentle to the shared host), request signing where applicable.
- P2.1.7 Contract tests plus recorded fixtures from the real site (sanitised); a staging WordPress clone for write tests.
- Done when: the adapter passes the `CmsProvider` contract suite; a full round trip create-draft → update → publish → restore-revision works on staging.

## P2.2 Content inventory sync (L)
Needs: P2.1.
- P2.2.1 Initial full crawl through the API: all posts, pages, CPT, categories, tags, authors, media metadata, comments counts, translations map; paginated, resumable.
- P2.2.2 Incremental sync by `modified_gmt` and deletions detection; scheduled job plus manual trigger; webhook or companion-plugin push if P2.1.1 chose it.
- P2.2.3 Content normalisation.
  - [ ] Convert HTML and Elementor structures to clean text plus heading outline, links, images with alt, tables, FAQ blocks, embeds.
  - [ ] Content hash and word counts; language detection; reading time; publish and modified dates.
  - [ ] Store raw and normalised, versioned by hash.
- P2.2.4 Rendered-page fetch (what Google sees): fetch public URLs politely (robots.txt, throttling, user agent), capture status, redirects, canonical, meta robots, headers, HTML, render check for JS-dependent content (headless only when needed), core web vitals field data link.
- P2.2.5 URL registry: canonical vs alias, redirects chain, orphan detection, sitemap membership (`yoast-get-sitemap-index` or sitemap fetch).
- P2.2.6 Inventory UI: filterable table (type, language, status, age, word count, traffic, rankings, audit score, last audit).
- Done when: inventory matches WordPress counts exactly (`site-stats`), incremental sync picks up an edit within one cycle.

## P2.3 Site knowledge and consistency memory (L)
Needs: P2.2, P1.6, P1.1.5.
- P2.3.1 Embeddings for every content item and section (chunking by heading), stored with pgvector, refreshed on change.
- P2.3.2 Style and voice extraction: analyse best-performing and owner-approved articles to derive tone, structure patterns, terminology, CTA style, formatting habits; store as reviewable brand-voice memory (proposed → approved).
- P2.3.3 Topic map and entity map: what the site already covers, entities and terms, depth per topic, claims made (prices, statistics, guarantees) to keep new content consistent.
- P2.3.4 Internal link graph: inbound / outbound counts, anchor text inventory, link depth, orphan pages, link equity approximation.
- P2.3.5 Duplicate and cannibalisation detection: semantic near-duplicates, multiple pages targeting the same query (uses GSC data from P2.4).
- P2.3.6 "What we already said" service: given a proposed article, returns overlapping content, conflicting statements and link opportunities (used by the writer agent, see P2.9).
- Done when: for a sample topic the service correctly lists existing overlapping articles and prior claims with links.

## P2.4 SEO and analytics data sources (L)
Needs: P0.7, P0.20.2, P0.20.7.
- P2.4.1 Google Search Console connector: queries, pages, countries, devices, search appearance, date ranges, 16-month history backfill, sitemap and indexing status, URL Inspection (quota aware).
- P2.4.2 GA4 Data API connector: sessions, users, engagement, conversions / key events, landing pages, source / medium / campaign, events by name; custom dimensions for UTM.
- P2.4.3 PageSpeed Insights and CrUX connector: lab and field metrics per URL, scheduled rotation with quota respect.
- P2.4.4 Third-party SEO data (`SearchDataProvider`): Ahrefs and SE Ranking adapters for keywords, volume, difficulty, SERP features, backlinks, organic competitors, AI-answer visibility; caching and credit budgeting so agents cannot burn credits (per-run cap).
- P2.4.5 Normalisation into `MetricSnapshot` and page-level tables; restatement handling; timezone consistency between sources.
- P2.4.6 Data quality checks: sampling / threshold notes (GSC anonymised queries), freshness alerts per source.
- Done when: a page's clicks, impressions, position, sessions and CWV can be shown together for any URL over time.

## P2.5 Technical and on-page auditor (L)
Needs: P2.2, P2.4.
- P2.5.1 Rule engine design: each rule is a class with key, severity, scope (site / page / template), evidence collector, fix suggestion, auto-fixability flag; registry; versioned rule set; per-rule enable/disable.
- P2.5.2 Crawlability and indexability rules: status codes, redirect chains and loops, canonical correctness, `noindex` mistakes, robots.txt rules, sitemap coverage and accuracy, hreflang correctness (return tags, x-default, language codes), pagination, faceted URLs, soft 404, duplicate titles/descriptions, URL hygiene.
- P2.5.3 On-page rules: title and meta length and uniqueness, H1 presence and uniqueness, heading hierarchy, keyword placement (without stuffing), image alt text and dimensions, lazy-loading and LCP image handling, internal link count and anchors, external link quality, thin content, outdated dates, broken links (internal and external).
- P2.5.4 Performance rules using CrUX and PSI: LCP, INP, CLS, TTFB, page weight, render-blocking assets, Elementor-specific bloat indicators, caching headers.
- P2.5.5 Structured data rules: presence and validity for Article, FAQ, Organization, LocalBusiness, BreadcrumbList, Product / Service where relevant; mismatch between visible content and markup; Rank Math schema conflicts.
- P2.5.6 Security and trust signals: HTTPS, mixed content, cookie / consent banner impact on rendering, author and about pages, contact details, policy pages.
- P2.5.7 Mobile and accessibility quick checks relevant to SEO.
- P2.5.8 Findings model: dedupe across runs, status lifecycle (`open`, `acknowledged`, `fixed`, `ignored`, `regressed`), evidence snapshot, first / last seen, effort and impact estimates.
- P2.5.9 Scoring: per page and per site, weighted by traffic and severity; trend over time.
- Done when: audit on the real site produces a triaged, deduplicated finding list with evidence and no false-positive class above an agreed rate (measured on a labelled sample).

## P2.6 Content quality auditor (L)
Needs: P2.3, P2.4, P1.1.
- P2.6.1 Quality rubric (E-E-A-T style): experience signals, expertise and author credentials, accuracy of claims, sourcing and citations, originality vs competitors, completeness against SERP intent, freshness, readability, structure, clarity of offer and CTA, brand fit.
- P2.6.2 Programmatic checks first (word count vs SERP median, freshness dates, citation count, author box, unanswered questions from People Also Ask / question keywords), then LLM judgement with rubric and evidence quotes; store scores with rationale.
- P2.6.3 Intent match: classify search intent for the page's main queries (GSC) and compare page type against what ranks (informational / commercial / transactional / navigational, format).
- P2.6.4 Decay detection: pages losing clicks and positions over rolling windows; statistical thresholds to avoid noise; seasonality awareness.
- P2.6.5 Verdicts: keep, refresh, consolidate (merge with redirect plan), expand, rewrite, delete (with redirect), leave alone; each with expected effect and effort.
- P2.6.6 Fact and claim checker: extracts factual claims (prices, laws, statistics, product specs), flags those that are stale or unverifiable, links to sources; marks legal/regulated claims for human review.
- Done when: verdicts on a sample of 30 real pages agree with a human SEO reviewer at an agreed rate.

## P2.7 GEO / AI-search readiness (L)
Needs: P2.2, P2.4, P1.1.
- P2.7.1 [spike] Evidence review refresh before building: current vendor guidance (Google AI features documentation, OpenAI search crawler documentation, Anthropic and Perplexity crawler documentation) and recent studies; output: updated fact sheet and prioritised hypothesis list.
- P2.7.2 AI crawler access audit: robots.txt rules for search-oriented and training-oriented user agents (for example the OpenAI, Anthropic, Perplexity, Google extended agents), CDN / WAF blocks, rendering, `noindex` / `nosnippet` conflicts; the owner decides policy per agent (decision item), the platform verifies it is implemented.
- P2.7.3 Answer-readiness analysis per page.
  - [ ] Answer-first intro presence (direct answer near the top), question-style headings, concise definitions, scannable lists and tables, FAQ coverage from real question keywords.
  - [ ] Entity clarity: brand, product, location, people consistently named and linked; `Organization` / `sameAs` consistency; Knowledge Graph presence check.
  - [ ] Evidence density: statistics with sources, quotes, original data, dates, author identification.
  - [ ] Extractability: heading structure, no key content hidden behind tabs / accordions that do not render, semantic HTML.
- P2.7.4 AI visibility tracking.
  - [ ] Prompt set management: seed from question keywords, GSC queries and customer language; segments by intent and product.
  - [ ] Sampling engine across engines that have official APIs or licensed data (through Ahrefs Brand Radar / SE Ranking AI tracking connectors where available; direct API sampling only where terms allow).
  - [ ] Repeated sampling with confidence intervals (answers vary), store `AiVisibilitySample` with cited URLs, brand mentions, sentiment and position.
  - [ ] Metrics: citation share, mention share, share of voice vs competitors, per-prompt and per-page trends.
- P2.7.5 Experiment framework: apply a GEO change to a page group, keep a comparable control group, measure citation and traffic changes over a fixed window, record the result as outcome memory. Hypotheses to test include structured data additions, answer-first rewrites, FAQ blocks, and optional `llms.txt` (kept low priority given current vendor statements).
- P2.7.6 GEO recommendations engine: per page a prioritised list tied to measured evidence, never generic checklists.
- P2.7.7 Report: "AI visibility" section in the weekly report (P2.13).
- Done when: the platform reports baseline citation / mention share for the site's priority prompts and can run one controlled GEO experiment end to end.

## P2.8 Opportunity engine (M)
Needs: P2.3, P2.4, P2.6.
- P2.8.1 Keyword and topic opportunity sources: striking-distance queries (positions 8–20), high-impression low-CTR queries, competitor gaps (Ahrefs / SE Ranking), question keywords, decaying pages, missing cluster spokes, seasonal demand.
- P2.8.2 Clustering: group keywords by SERP overlap and semantic similarity, choose pillar and spokes, map to existing pages (refresh vs new).
- P2.8.3 Scoring: expected traffic and value, difficulty, business relevance (from brand memory), effort, cannibalisation risk, AI-citation potential; transparent score breakdown.
- P2.8.4 Backlog: ideas with status (`idea`, `approved`, `in_progress`, `published`, `rejected` with reason feeding memory); dedupe against existing content and previous rejections.
- P2.8.5 Editorial calendar generation respecting capacity settings, seasonality and campaign alignment (ads and social).
- Done when: the backlog contains scored, deduplicated ideas each linked to evidence, and rejected ideas are not proposed again.

## P2.9 Article production pipeline (XL → split)
Needs: P2.3, P2.8, P1.4, P1.5, P2.11.
- P2.9.1 Brief generation: target query and cluster, intent, audience, angle, differentiation vs top SERP results, required sections, questions to answer, entities to include, internal links to add, external sources to cite, word range, tone from brand voice, target locale, CTA, compliance notes.
- P2.9.2 Research step: SERP analysis, competitor outline extraction, source gathering with URL verification; every fact needs a source record; untrusted-content handling for fetched pages.
- P2.9.3 Consistency check against site knowledge (P2.3.6): reuse terminology, avoid contradicting earlier claims, avoid duplicating existing articles, decide link targets and anchor text.
- P2.9.4 Outline stage with approval option (configurable per autonomy level).
- P2.9.5 Drafting agent: writes in the target locale with the brand voice; follows GEO structure (answer-first intro, question headings, concise definitions, tables, FAQ); no fabricated statistics, quotes or sources (grounded generation with citation markers).
- P2.9.6 Editing passes as separate steps: fact check against sources, style and voice pass, SEO pass (title, meta, headings, keyword usage, internal links, alt text, schema plan), readability pass, compliance pass (banned claims, legal disclaimers).
- P2.9.7 Media: featured image and inline images through the media pipeline (P3.8 generator port, or stock with licence tracking), alt text, compression, WebP / AVIF, dimensions.
- P2.9.8 Structured data plan: Article, FAQPage (only where visible on the page), BreadcrumbList; validated before publish.
- P2.9.9 Quality gate: automated scoring with the P2.6 rubric and P2.5 rules; must pass thresholds; failing drafts loop back with specific instructions, bounded retries.
- P2.9.10 Human review UI: side-by-side preview, diff against previous iteration, inline comments that become feedback events, one-click approve or request changes.
- P2.9.11 Multilingual handling: original in one language, translation as separate items linked via Polylang / WPML (`link-post-translation`), each translation adapted not literal, hreflang consistency, glossary enforcement.
- P2.9.12 Post-publish tasks: request indexing where appropriate, add internal links from existing articles to the new one (as proposed edits), share to social pipeline (P6/P7) as a draft, schedule performance checks at 7 / 30 / 90 days.
- Done when: a full article goes from approved backlog idea to a published (or scheduled) WordPress post with correct meta, schema, links and images, and every claim has a source record.

## P2.10 Refresh and consolidation pipeline (L)
Needs: P2.6, P2.9, P2.11.
- P2.10.1 Refresh brief from the verdict: what to update (data, sections, examples, intro, FAQs, links), what to keep (ranking passages), how to preserve URL and history.
- P2.10.2 Edit proposal as a diff (paragraph level) against the current content; protect passages flagged as ranking well; never silently rewrite human-edited sections.
- P2.10.3 Consolidation: choose target URL, merge content, generate 301 redirect map, update internal links, update sitemap, verify no orphaning; high-risk action (R3) requiring approval.
- P2.10.4 Before / after tracking: snapshot metrics, rerun audit, measure change at fixed windows, store as outcome memory.
- P2.10.5 Rollback: restore previous version and redirects.
- Done when: a refresh on staging shows a clean diff, applies, and can be rolled back exactly.

## P2.11 Publishing and rollback (M)
Needs: P2.1, P1.5.
- P2.11.1 Action kinds: `content.create_draft`, `content.update`, `content.publish`, `content.schedule`, `content.unpublish`, `content.set_seo_meta`, `content.set_schema`, `content.redirect`, `content.upload_media`, `content.moderate_comment`, `content.reply_comment` with risk classes (publishing to production is at least R2).
- P2.11.2 Pre-publish validation: required meta present, no placeholder text, links resolve, images have alt and dimensions, schema valid, no banned terms, canonical correct, slug rules, category and tag rules, translation links.
- P2.11.3 Preview: Elementor preview link or rendered draft screenshot for review.
- P2.11.4 Post-publish verification: fetch public URL, check status 200, canonical, indexability, schema validity, sitemap inclusion.
- P2.11.5 Scheduling with WordPress native scheduling vs platform scheduler decision; timezone handling.
- P2.11.6 Rollback via revision restore and unpublish; audit trail.
- Done when: publishing and rollback are verified by post-publish checks and audit events.

## P2.12 Continuous review scheduler (M)
Needs: P2.5–P2.7, P1.8.
- P2.12.1 Crawl queue with priority score: traffic, revenue relevance, age since last audit, recent changes, ranking volatility, issue history; per-day crawl budget so the host is never stressed.
- P2.12.2 Rotation policy: full-site cycle target (for example every 30 days), high-value pages weekly, changed pages within a day.
- P2.12.3 Delta reporting: new findings, resolved findings, regressions since last cycle.
- P2.12.4 Trigger-based audits: after deployments or plugin updates (detected through plugin list changes), after traffic drops, after publishing.
- P2.12.5 Site-level regression baseline ("SEO drift"): snapshot key signals and compare, alert on meaningful negative deltas.
- Done when: the queue covers the full site within the configured cycle and produces a weekly delta.

## P2.13 Reports and ideas (M)
Needs: P2.4–P2.8, P1.9.
- P2.13.1 Weekly SEO + GEO report: performance, wins and losses, new findings, experiments, AI visibility, top opportunities, recommended actions with expected impact.
- P2.13.2 Monthly strategic review: cluster progress, content velocity, technical debt trend, competitor movement (P8 hooks).
- P2.13.3 Idea generation sessions on demand ("what should we publish next quarter?") using the opportunity engine and memory.
- P2.13.4 Report rendering in the user's locale, exportable (PDF / share link), archived and diffable.
- Done when: the weekly report is generated automatically, reviewed by the owner, and its recommendations can be turned into approved actions with one click.

## P2.14 Website menu UI (L)
Needs: P0.13, P2.2–P2.13.
- P2.14.1 Overview: site health score, traffic and conversion trend, AI visibility, open findings, pending approvals.
- P2.14.2 Content: inventory table, item detail (versions, audits, metrics, links, opportunities), bulk actions.
- P2.14.3 SEO: findings board by severity and effort, fix proposals with preview and apply, rule settings.
- P2.14.4 GEO: prompts, citation share charts, experiments, crawler access status.
- P2.14.5 Editorial: backlog, calendar, briefs, drafts with the review UI.
- P2.14.6 Inbound: contact events timeline and filters (P2.16).
- P2.14.7 Settings: connection, sync cadence, audit rules, crawl budget, publishing rules.
- P2.14.8 All strings in the catalog; e2e for "approve draft → publish → verify".
- Done when: every P2 capability is reachable and operable from the Website menu.

## P2.15 Comments moderation and replies (S)
Needs: P2.1, P1.13.
- P2.15.1 Sync comments; classify spam, toxicity, questions, leads, support issues; treat text as untrusted.
- P2.15.2 Proposed actions: approve, spam, trash, reply draft in brand voice; auto-mode only for obvious spam at autonomy L3.
- P2.15.3 Escalate leads and support issues as inbound events (P2.16).
- Done when: spam is cleared automatically and replies wait for approval.

## P2.16 Contact and conversion tracking (L)
Needs: P2.1, P2.4.2, P0.16.7.
Interpretation: capture every inbound contact event from the website (forms, emails, calls, chats), attribute it to a source, and notify. No CRM features.
- P2.16.1 [spike] Discover contact channels: form plugins in use (from `SiteProfile`), contact emails, click-to-call links, WhatsApp / chat widgets, booking tools; choose capture method per channel.
- P2.16.2 Capture methods.
  - [ ] Form submissions: webhook or companion plugin hook posting signed events to the platform (with replay protection); field mapping per form; spam filtering.
  - [ ] Email-based leads: WP Mail SMTP logs (`list-email-logs`, `get-email-stats`) as a secondary source and health check (failed deliveries alert).
  - [ ] Click-to-call and click-to-chat events through GA4 events; GA4 key events import.
  - [ ] Optional server-side tracking snippet (first-party, consent-aware) to record landing page, referrer, UTM parameters, click ids (`gclid`, `fbclid`, `msclkid`, and OpenAI ads click id if provided) into the event.
- P2.16.3 `InboundEvent` model and dedupe (same person submitting twice), status lifecycle (`new`, `seen`, `handled`), minimal PII with retention policy, consent flag capture.
- P2.16.4 Attribution: first-touch and last-touch from UTM and click ids, mapping to ad campaigns (P3–P5) and social posts; unknown source handling; cross-check against GA4.
- P2.16.5 Notifications: instant alert for new contact with summary and source; daily digest; SLA reminders for unhandled events.
- P2.16.6 Offline conversion feedback (optional): send conversion events back to Meta Conversions API and Google Ads only with consent and configured rules (implemented in P3.13 / P4.10).
- P2.16.7 Reports: contacts by source / campaign / page, conversion rate per landing page, form abandonment if measurable.
- P2.16.8 Privacy and compliance: consent recording, data minimisation, deletion request tooling, no message content in logs.
- Done when: every contact form submission on the site appears within one minute with correct source attribution and a notification, and health checks detect a broken form.
