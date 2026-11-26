# P3 Meta Ads and the shared Meta setup: status

Verified against a fake Graph server only (`apps/api/ads/meta/tests/fake_meta.py`): 36 backend tests for
`ads/meta`, plus the existing 955-test suite. **Not yet run against a live Meta app.** Field names,
permissions and error codes come from Meta's documentation and must be confirmed with a real account.

## One Meta app, one setup screen (`/meta`)
- A Meta app has two sets of keys: the app itself (Facebook Login and the Marketing API: ads, pages) and the
  Instagram product inside it (Instagram Login). The screen saves both in one step; they are stored as the
  workspace connections `meta_app` and `instagram_app`, encrypted, audited, with environment fallbacks
  (`APP_META_APP_ID`, `APP_META_APP_SECRET`, `APP_INSTAGRAM_*`).
- It shows, per product, whether it is ready, the redirect address to paste into the Meta app, the webhook
  address, what is connected, and a short guide to creating the app.
- After the keys exist, every Connect button (Instagram, Meta Ads) is one click: Meta's own sign-in, Allow, back.
  App Review and Business Verification are only needed before accounts that have no role on the app can connect.

## Meta Ads (`/ads/meta`)
- **Connection**: Facebook Login (`ads_management`, `ads_read`), code exchanged for a long-lived user token
  (about 60 days) with the granted permissions read back; renewal by re-exchange, otherwise sign in again.
  Account picker (every ad account the person reaches), pages, Instagram accounts and pixels discovery.
  `appsecret_proof` on every call (the secret comes from the workspace's Meta app).
- **Sync**: campaigns, ad sets and ads into the shared entity levels (campaign, ad group, ad), daily numbers per
  ad in the account time zone (spend, impressions, clicks, results counted once per plain action type), account
  facts (status, spend cap, pixels). Changes between syncs are recorded (targeting and learning stage included).
- **Checks**: review and delivery state, serving issues and policy feedback, learning limited, missing pixel for
  lead and sales objectives, spend cap nearly reached, headline and text length, missing call to action, picture
  and tracking tags. Results checks are shared with the other platforms.
- **Actions** (through the action layer, so approval, guardrails, audit and rollback apply): `pause` (R1),
  `activate` (R2, spend guarded), `archive` (R2), `update_budget` (R2, campaign or ad set, rollback keeps the old
  value), `campaign.create` (R2): a paused campaign with one ad set (Advantage+ audience, automatic placements)
  and one image ad in one approved step. The approver sees the whole structure and a validation first; if any step
  fails everything already created is deleted again.
- **Screens**: campaigns overview, all ads, AI health check, alerts and the shared item dashboard (budget editor,
  creative preview, targeting, learning stage, problems reported by Meta) reuse the existing ad screens.

## Open
- Run against a live app and fix any field or permission differences (notably whether `promote_pages` and
  `instagram_accounts` are readable with `ads_management` alone).
- AI assistant for Meta campaigns (goal to plan to paused campaign), copy variants and AI review actions that
  apply edits (creative edits need a new creative object).
- Video and carousel ads, lead forms, catalog and sales campaigns with value optimisation, special ad categories.
- Facebook Page management (P6.10) and Conversions API (P3.13), webhooks (P3.14), system-user tokens.
- Insights breakdowns (placement, age, gender), frequency and fatigue rules, restatement window beyond 30 days.

## Google Ads: campaign optimiser (added 2026-10-13)
- **Optimize with AI** tab on the Google Ads screen: pick a campaign, optionally state a goal, and one background
  run gathers the campaign, keywords (quality score, cost, conversions), 300 search terms, existing campaign
  negatives, conversion tracking and a **landing page audit** of every final URL. The model returns findings
  (bottlenecks, most important first) and itemised changes: negative keywords, new keywords, keywords to pause,
  new ad text, a budget change (within 30 percent) and landing page tasks.
- Every item is checked in code against the real account and Google's limits before a person sees it (invented
  ids, duplicates, over-long headlines, existing negatives, a budget jump are dropped and counted).
- **Nothing changes until items are chosen.** "Approve and apply" runs the chosen items as the approver;
  "Send to Approvals only" proposes them for someone else. Each item is an ordinary action (guardrails, audit,
  rollback). Items already sent cannot be sent twice.
- Landing page audit (no AI, public https only; http links are read through their https twin): load time and
  weight, https, mobile viewport, headline, word count, form or phone link, buttons, Google tag and other
  tracking, trust proof, and how many words of the ads the page repeats. Page changes are advice, not applied.
- Google no longer requires a developer token (access levels follow the Google Cloud project). The token is
  optional in settings and the form; it is sent only when present.
- Verified against the real account for reading (search terms, negatives, three landing pages scored 80, 84 and
  80); the AI step itself was tested with a fake model only.
