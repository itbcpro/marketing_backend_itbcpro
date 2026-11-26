# OpenAI Ads adapter status

Code: `apps/api/ads/openai/` (client, schemas, actions, tools, provider). Vendor notes:
`docs/vendor/openai-ads-api.md`.

## What exists
- **Connection**: provider `openai_ads`, one API key per ad account, health check via `GET /ad_account`
  (active, degraded, or expired when the key is rejected).
- **Client**: bearer auth, per-connection rate limiting (shared Redis bucket, penalised on HTTP 429),
  `Idempotency-Key` on every create/update derived from the action id, cursor pagination, error
  mapping (401/403 auth expired, 429 rate limited, 408/409/425/5xx retryable, other 4xx permanent).
- **Action kinds** (all validated against the documented limits before a human sees them):

  | Kind | Risk | Notes |
  | --- | --- | --- |
  | `ads.openai.campaign.create` | R2 | Always created paused; spend = budget. |
  | `ads.openai.ad_group.create` | R1 | Always paused; bidding compatibility checked. |
  | `ads.openai.ad.create` | R2 | Chat-card creative, always paused. |
  | `ads.openai.image.upload` | R1 | Public HTTPS image URL. |
  | `ads.openai.activate` | R2 | Starts delivery; carries expected daily spend for caps. |
  | `ads.openai.pause` | R1 | Stops delivery. |
  | `ads.openai.archive` | R3 | Irreversible; never auto-approved; no rollback. |
  | `ads.openai.campaign.update_budget` | R2 | Stores previous budget; rollback restores it. |
  | `ads.openai.ad_group.update_bid` | R2 | Stores previous bidding; rollback restores it. |

- **Agent tools**: `openai_ads_overview`, `openai_ads_insights` (reads) plus one proposing tool per
  kind. The ads strategist has all of them.
- Creating never spends: a resource only delivers after a separately approved `activate`.

## Screens (web)
- **Connections** (`/connections`): add an OpenAI Ads key or a Claude (Anthropic) key; the account is
  discovered from the key, so nobody types an account id. Test and remove from the same page. A
  workspace's Claude connection is used by its agents first, the platform key second.
- **OpenAI Ads** (`/ads/openai`): campaigns, ad groups and ads with status, budgets, bids and review
  state; pause, activate, archive; edit names, budgets, bids, hints and creatives. Edits are sent as
  proposed actions and applied at once when the user may approve, otherwise they wait in Approvals.
  Billing tab (spend today, month, 30 days, spending limits, account status). Reports tab (level,
  date range, period, country/device/platform breakdown, totals, CSV export).
- **Ad assistant** (top of `/ads/openai`): the owner describes a goal; Claude (`ads/openai-assist` prompt)
  asks for missing essentials, then drafts one campaign, one to three ad groups with context hints and two
  or three ad variants, with a rationale per part, best-practice reminders and warnings (spend cap, links
  off the account's website). The draft can be refined by feedback. Launch hands the approved draft to the
  ads strategist, which creates everything paused, step by step, through Approvals; activation stays a
  separate approved action. Conversations are saved (`AssistSession`): the goal, questions, answers, every draft version, feedback and
  errors form a thread that survives refreshes and devices. Drafting runs as a background job
  (`agents.assist_turn`) and the screen polls, so a slow draft is never lost. Endpoints under
  `/ads/openai/assist/sessions` (list, start, get, answers, feedback, retry, launch, delete).
  Needs the Celery worker and beat (`make dev-worker`, `make dev-beat`).
- **Campaign builder (prompt v2)**: the brief may state the number of ads and ad groups; the draft is
  checked in code against them (up to 10 groups, 10 ads per group, 40 ads in total), retried once on a
  mismatch, and a warning is shown if it still differs. Places are named by the model and resolved to ids
  through `GET /v1/geo_lookup/search` (response key `results`, max `limit` 100; types seen live: `country`,
  `region`, `postal_code`; no city or metro type for Canada). A province is one id; a city ("Vancouver, BC")
  is every postal area (FSA) of that city; a postal code is matched by its first three characters (US: ZIP).
  Ids go to `targeting.locations.include`/`excluded_locations.include` and replace country-wide targeting;
  a draft with no matched place warns that it would cover the whole country. Daily budget minimum is 25
  (the platform rejects less).
- The API exposes no invoices or payment methods; the billing tab says so and points to Ads Manager.

## Not done yet
- Bulk API with `validate_only` as the approval dry run, custom audiences, product feeds,
  conversion setup and the Conversions API (server events), partner-key `OpenAI-Ad-Account` header.
- Scheduled insight sync into the reporting app and the review-status alert (`rejected`).
- Probing a live low-budget account for the undocumented items listed in the vendor notes.
- Existing workspaces keep their old agent definitions; new tools reach them when the agent
  definition is edited or recreated.
