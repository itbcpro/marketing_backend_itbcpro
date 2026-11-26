# P2 Website module: staged build and test plan

No third-party SEO tool is used (no Ahrefs, no SE Ranking). The platform connects to the owner's own
sites and to Google only (Search Console, GA4, PageSpeed Insights, CrUX). Everything else is built here.
This replaces P2.4.4 of `tasks/P02-wordpress-seo-geo.md`.

Two kinds of site are supported side by side:

- **WordPress**: connected with a user name and an application password (WordPress REST API).
- **Git-built site** (an AI-made site in a GitHub repository that a push builds and deploys): read through its
  public pages, sitemap and robots.txt. The repository name is recorded; changes will arrive as pull requests in
  a later stage (never as direct pushes).

Each stage is shippable and has a manual test. Run them in order.

| Stage | What it adds | Status |
|---|---|---|
| S1 | Websites, WordPress connection, health check, site profile (plugins, counts), Git-built site probe | **Built, tested against a fake server** |
| S2 | Content inventory sync (posts, pages, custom types, terms, authors, SEO plugin data, sitemap cross-check) for both site kinds | **Built, tested, and run read-only against the live WordPress site** |
| S2.5 | Page dashboard: on-page score and checks, Google preview, AI review and suggestions, editing (title, address, SEO title, meta description) through the approval layer, change history with rollback | **Built, tested against fakes; SEO fields need the one-time WordPress snippet** |
| S3 | Google sign-in (OAuth) and Search Console: queries, pages, 16 month backfill, sitemaps | **Built, tested against a fake Google; not yet run against a real Google account** |
| S4 | Rank tracker from Search Console, striking-distance and CTR opportunities | Planned |
| S5 | Index monitor: URL Inspection per page, not-indexed reasons, alerts | Planned |
| S6 | Own crawler and auditor: status, redirects, canonical, meta, headings, schema, images, links; "check this URL"; homepage analyzer | Planned |
| S7 | Speed monitor: PageSpeed Insights and CrUX trends per page | Planned |
| S8 | GA4: sessions, conversions, landing pages next to Search Console data per URL | Planned |
| S9 | Content intelligence: embeddings, topic map, brand voice, opportunity backlog, editorial calendar | Planned |
| S10 | Article pipeline: brief, draft, edits, quality gate, review, publish to WordPress, rollback; for the Git site, a pull request | Planned |
| S11 | Competitor watch (public pages and sitemaps only) and backlink import from the Search Console CSV export | Planned |
| S12 | Automation (publishing cadence, autonomy levels) and report scheduler (weekly, biweekly, monthly) | Planned |

## Honest limits of "no third party"

- Search Console has no backlinks API: backlinks come from a manual CSV export plus GA4 referrals.
- Competitor traffic and rankings cannot be known; only their public content, structure and publishing pace.
- Rankings exist only for queries where the site already has impressions.
- AI visibility can only be sampled with Claude.

## S1 manual test

1. Start services: `make up`, `make migrate seed`, `make dev-api`, `make dev-worker`, `make dev-beat`, `make dev-web`.
2. In WordPress: Users, Profile, Application Passwords, create one for an Editor or Administrator user.
3. Open **Connections**, choose **WordPress**, enter the site address, the user name and the application
   password. Expected: status "Connected". A wrong password is refused at once. A read-only user shows "Degraded".
4. Open **Website**, choose WordPress, pick the connection, **Add and check**. Expected: counts of posts, pages,
   media, categories, tags, comments that match WordPress; detected SEO plugin and page builder; warnings listed
   in plain language (for example no SEO plugin, or an application password that was rotated).
5. Add the Git-built site: kind **Git-built site**, its public address, optionally `owner/name` and the branch.
   Expected: title, sitemap URL count, robots.txt and canonical read; warnings for a missing sitemap, meta
   description, H1 or viewport tag.
6. Press **Check again** after changing something on the site; the profile updates. Every six hours the platform
   checks all sites by itself (`content.probe_all`).

## S1 technical notes

- App `content` (`apps/api/content`): `models.Website`, `probe.py` (read-only probes), `services.py`,
  `api.py` (`/api/v1/websites`), `tasks.py`, `wordpress/` (client, provider descriptor).
- Outbound requests use `core.security.safe_fetch` (https only, public addresses only, pinned IP, size cap),
  through `content/net.py` so tests stay offline. A WordPress site on a private address or plain http is refused.
- Credentials: the application password is stored encrypted as the connection's access token; the site address
  and user name are kept in `Connection.config["form"]` (non-secret form values are now saved generically).
- Verified: 19 new tests with a stateful fake WordPress (`content/tests/fake_wordpress.py`), 709 backend tests and
  24 frontend tests pass; ruff, mypy, i18n and language checks clean. **Not yet run against the live site.**


## S2 manual test

1. Open **Website**, press **Open content inventory** on a site, then **Sync content** (needs `make dev-worker`).
   The status goes from "Waiting for a worker" to "Last sync finished". **Full re-sync** re-reads everything.
2. The tiles show pages, words, removed pages, and "posts: inventory / WordPress" with a check mark when the
   local copy equals the WordPress counts. On itbcpro.ca: 23 of 23 posts and 82 of 82 pages.
3. Click an issue chip (no meta description, thin post, images without alt text, not in sitemap, noindex,
   page error) to filter; use search, type and sort; click a row for headings, categories, tags and a text preview.
4. Edit a post in WordPress, press **Sync content** again: only that post is re-read (incremental) and updated.
   Trash a post: the next sync marks it removed and the "Removed from site" tile grows.
5. Git-built site: the same screen lists every sitemap URL with its title, headings, word count, meta tags and
   HTTP status (a broken page shows "Page error"). Pages whose sitemap date did not change are not fetched again.

## S2 technical notes

- Models `ContentItem` (key `wp:<type>:<id>` or `url:<address>`) and `SyncRun`; `content/sync.py`,
  `normalize.py` (stdlib HTML parser, no new dependency), `sitemap.py`, `selectors.py`, `api_inventory.py`.
- Incremental sync uses WordPress `modified_after` since the last finished run (minus a one hour margin) and always
  lists all ids to detect deletions. Every six hours `content.sync_all` queues a sync for each site.
- Custom post types are discovered from `/wp/v2/types`; internal types (Elementor library, menus, templates) are skipped.
- Yoast data (`yoast_head_json`) fills title, description, canonical and robots. Other SEO plugins are read from the
  rendered page in S6.
- Found on the live site: Elementor prints `<style>` blocks before the JSON of posts, which breaks a strict JSON
  parse. `client.parse_json` tolerates it (tested). A real bug in `core.security.safe_fetch` with gzip responses
  was also fixed (S1 follow-up).
- Verified: 37 content tests, 726 backend tests; frontend typecheck, lint and 24 tests pass.


## S2.5 Page dashboard

Open a page from the inventory (`/website/<site>/page/<page>`): tabs Overview (score, Google preview, checks),
Edit, AI review (brief form, review, suggestions to apply or dismiss), Content, Changes (history, rollback).

- Editing goes through the action layer (`content.update_page`, risk R2): previous values are read live and kept
  for rollback; a change is refused if the page was edited on the site after the owner opened it.
- WordPress does not let outside tools write Yoast/Rank Math fields until they are registered for REST. The Websites
  probe reports `seo_meta.writable`; while false the Edit tab offers title and address only and shows the snippet
  (`content/seo_meta.py: HELPER_SNIPPET`). The snippet has not been run on the live site yet.
- Body text is not editable yet; that arrives with the article pipeline (S10).
- Not done: the full 53-part audit report requested on 2026-10-01 (weighted category scores, GEO score, query
  coverage, clusters, roadmap). The AI review here is a short review; the full report needs the evidence from S3, S6
  and S7 first. See the conversation of that date for the gap analysis.


## Dev note: restart the worker after code changes

`make dev-worker` and `make dev-beat` do not reload code. A worker started before a change does not know new task
names or new action kinds: an approved change then stays "approved" forever (dead letter `UnknownActionKindError`) and
the page says "The change is still being applied". Stop the worker (Ctrl+C) and start it again after pulling or
changing backend code; the Django API server reloads by itself.


## S3 Google sign-in and Search Console

Screen: Websites, **Search performance** (`/website/<site>/search`), and a **Search** tab on every page dashboard.

One-time setup in Google (the screen shows the redirect address to copy):
1. Google Cloud, a project (the Google Ads one can be reused): enable the **Google Search Console API**.
2. OAuth consent screen: add the scope `https://www.googleapis.com/auth/webmasters.readonly`; while the app is in
   testing mode add your own Google account as a test user.
3. Credentials: an OAuth client of type **Web application** with the authorised redirect URI
   `<web address>/bff/api/v1/connections/oauth/google_search/callback`.
4. The Google account must be an owner or full user of the property in Search Console.

Flow: keys (skipped when the Google Ads keys or `APP_GOOGLE_OAUTH_*` exist) -> Sign in with Google -> choose the
property (the domain property `sc-domain:` is preferred) -> the first import reads about sixteen months in
30-day slices -> every 12 hours `content.search_sync_all` re-reads the last six days (Google restates recent days).

Tables: `GscDaily` (site totals), `GscPageDaily`, `GscQueryDaily` (date, query, page), `GscSitemap`, `SearchProperty`.
Position is the impression-weighted average. Data lags about two days; Google hides rare queries, so totals can exceed
the sum of queries. The AI page review now receives the measured numbers of the page (top queries, change vs the
previous 28 days) and is told not to guess when they are missing.

Only the read-only Search Console scope is requested. Analytics (GA4) will ask for its own scope in S8.
Restart `celery worker` and `celery beat` after pulling: `content.sync_search` and `content.search_sync_all` are new.

Verified: 118 content tests with a fake Google (token refresh, pagination, restated days, expired authorisation),
808 backend tests, frontend typecheck/lint/24 tests.


## S2.6 Image alt text

In the page dashboard, the "Image alt text" check opens a list of the page's images without alt text: thumbnail,
file name, the heading above it, an editable alt field. **Suggest alt text with AI** sends each picture (and its file
name and nearby text) to Claude, which looks at it; SVG files and unreadable pictures fall back to file name and context
(and say so). **Apply** saves the alt text to the image in the WordPress media library (action
`content.update_media_alt`, R2, previous values kept for roll back); the page is then read again so the score updates.

- Elementor takes the image alt from the media library when the widget has none, so this fixes most pages. A page whose
  alt is stored inside the page (block editor, or an Elementor widget with its own alt) may still show the problem; it
  needs editing in the page builder.
- Pictures that are not in the media library (hotlinked) are listed but cannot be changed from here.
- Decorative pictures are marked by the AI but still count in the check; marking them as decorative will come later.
- Up to 10 images per suggestion run; images are stored per page when it is synced (Full re-sync, or opening the dialog
  reads the single page).
- Restart the Celery worker: `content.update_media_alt` is a new action kind.
