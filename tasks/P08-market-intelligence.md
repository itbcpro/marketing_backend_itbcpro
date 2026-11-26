# P8 — Market Intelligence

Goal: continuously understand the market: trends, competitors, what they are doing now, and gaps we can own. Output feeds content (P2), social (P6, P7), ads (P3–P5) and the executive briefing (P9).
Needs: P0, P1, P2.4 (SEO data), P1.6 (memory).

Legal and ethical rules for this phase (binding): use official APIs, licensed data vendors and public pages within site terms and robots.txt; no scraping behind logins; no fake accounts; respect rate limits; store only public business information; every source has a recorded terms note.

---

## P8.1 Source registry and compliance (M)
Needs: P1.
- P8.1.1 `Source` model: type (API, vendor, public web page, RSS, sitemap), owner, terms note, allowed use, rate limits, cost, reliability score, last success.
- P8.1.2 Compliance gate: a source cannot be enabled without a recorded terms review; robots.txt honoured by the fetcher; per-domain politeness (rate, concurrency, user agent that identifies the platform).
- P8.1.3 Source health monitoring and alerting.
- Done when: the registry blocks unreviewed sources and enforces politeness.

## P8.2 Trend detection (L)
Needs: P8.1, P2.4.
- P8.2.1 Signals: search demand changes (GSC, Ahrefs / SE Ranking volumes and history), Google Trends data via the official or licensed route decided in a spike, news and industry publications (RSS or news API), Reddit / Hacker News / YouTube / community discussions via official APIs, industry reports, regulatory changes, product launches in the market.
- P8.2.2 Trend model: normalise signals, detect emerging topics (growth rate, novelty, breadth across sources), classify (rising, seasonal, fad, structural), relevance scoring against brand memory and offer catalogue.
- P8.2.3 Trend lifecycle and decay handling; deduplicate similar trends; evidence links for each trend.
- P8.2.4 Timing advice: content and ad opportunity windows, lead time to produce.
- P8.2.5 Output items: `TrendSignal` with score, evidence, recommended actions (article, post, ad angle, landing page), sent to backlog and calendars as proposals.
- Done when: a weekly trend digest lists scored trends with evidence and turns selected ones into backlog items.

## P8.3 Competitor registry and profiling (M)
Needs: P8.1.
- P8.3.1 Competitor discovery: organic competitors from SEO tools, paid competitors, social competitors, AI-answer competitors (who gets cited for our prompts); owner curates the final list with tiers (direct, indirect, aspirational).
- P8.3.2 Profile: positioning, offers and pricing (public), target segments, geography, tone, technology stack (public detection), team size signals, funding / news, reviews and ratings summary.
- P8.3.3 Snapshot storage with versioning so changes are diffable.
- Done when: each tracked competitor has a structured, sourced profile.

## P8.4 Competitor monitoring (L)
Needs: P8.3, P8.1.2.
- P8.4.1 Website change monitoring: sitemap diffs (new pages), key pages (pricing, services, homepage, landing pages) content diffs with semantic change summaries, new blog posts and topics, publication cadence.
- P8.4.2 SEO movement: ranking changes, new keywords, backlink acquisition, top pages by traffic (vendor data), content gaps and wins.
- P8.4.3 Advertising intelligence: ad libraries that are officially accessible (Meta Ad Library within its allowed scope, Google Ads Transparency Center via its public dataset if available, LinkedIn Ad Library, TikTok library if relevant), messaging themes, offers, formats, longevity as a proxy for performance; SERP ad copy from data vendors; document scope limits per source (for example Meta's API coverage for commercial ads outside political / EU may be limited).
- P8.4.4 Social monitoring via official APIs or licensed vendors: posting cadence, formats, engagement rates, top posts, campaign themes; never scrape platforms against terms.
- P8.4.5 Reputation: review sites and public ratings changes.
- P8.4.6 Change significance scoring to avoid noise; alerts for high-significance changes (price change, new service, big campaign, new market).
- P8.4.7 "What are they doing now" digest per competitor and overall.
- Done when: a competitor's pricing page change or new campaign is detected, summarised and alerted within the agreed latency.

## P8.5 Market gap analysis (L)
Needs: P8.2, P8.4, P2.8.
- P8.5.1 Content gaps: topics competitors rank for that we do not, topics no one covers well (weak SERPs), question keywords with poor answers.
- P8.5.2 Offer and positioning gaps: features, services, price points, segments, geographies competitors serve or ignore; customer language from our DMs, comments and contact events versus competitor messaging.
- P8.5.3 AI-answer gaps: prompts where competitors are cited and we are not (P2.7 data), with the content needed to change that.
- P8.5.4 Channel gaps: channels where competitors are active and we are not, or where they are absent.
- P8.5.5 Opportunity scoring: size, difficulty, fit, speed to market, evidence quality; confidence levels and uncertainty notes.
- P8.5.6 Gap-to-action: generate proposals (article, landing page, ad test, post series, service idea for the owner) routed through approvals; strategic ideas marked as such and never auto-executed.
- Done when: the gap list is scored, evidence-linked and produces actionable proposals across channels.

## P8.6 Market and positioning analysis (M)
Needs: P8.3–P8.5.
- P8.6.1 Positioning map (dimensions chosen by the analyst agent and confirmed by the owner), SWOT with evidence, share-of-voice across search, AI answers and social.
- P8.6.2 Customer insight synthesis from first-party sources (DMs, comments, contact events, reviews) with privacy safeguards.
- P8.6.3 Pricing intelligence where public, with caution notes.
- P8.6.4 Quarterly market report with methodology and confidence.
- Done when: the quarterly report is generated with sources and reviewed by the owner.

## P8.7 Research agent and ad-hoc questions (M)
Needs: P1.4, P8.1.
- P8.7.1 On-demand research runs ("what are competitors doing in X?", "is topic Y growing?") with source-cited answers, bounded cost and time, untrusted-content handling.
- P8.7.2 Saved research artefacts with citations, reusable by other agents through memory search.
- P8.7.3 Fact-check pass for claims before they influence strategy.
- Done when: an ad-hoc research answer is reproducible with citations and cost report.

## P8.8 Alerts and digests (S)
Needs: P8.2–P8.6, P1.9.
- P8.8.1 Alert rules: major competitor change, trend spike in our niche, regulatory or platform news affecting ads or SEO, new competitor entering.
- P8.8.2 Weekly market briefing in the user's locale, feeding P9.
- Done when: alerts and briefing are delivered with links to evidence.

## P8.9 Market menu UI (M)
Needs: P0.13, P8.2–P8.8.
- P8.9.1 Trends board, competitors list and profiles with change timelines, gap explorer with scoring breakdown, research page, source registry admin, settings.
- P8.9.2 Strings in the catalog; e2e for "gap → proposal → approval".
- Done when: every P8 capability is operable from the Market menu.
