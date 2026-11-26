# OpenAI Ads API — working notes (read 2026-09-30)

Source of truth: https://developers.openai.com/ads (index at `/ads/llms.txt`, per-page Markdown at `<page>.md`).
These notes are our own summary for building the P5 adapter. Re-read the source page before implementing
each capability; the platform is in beta and changes.

## Basics
- Base URL `https://api.ads.openai.com/v1`. Auth `Authorization: Bearer <advertiser API key>`; one key per ad account, created in Ads Manager settings. Spend-limit endpoints, including reads, need billing-management permission.
- `Idempotency-Key` header prevents duplicate creation on retries; it is **required** when creating ad groups that use a Maximize Results strategy.
- Rate limits: 600 requests/min per endpoint, 1,200/min per account; bulk job creation 10 per 10 s per account.
- Amounts are integer micros (1 currency unit = 1,000,000). Timestamps in campaign fields are Unix seconds; spend-limit dates are `YYYY-MM-DD` in the account timezone.
- Lists use cursor pagination: `limit` (1–500, default 20), `after`, `before`, `order` (`asc`/`desc`); responses carry `object`, `data`, `first_id`, `last_id`, `has_more`.

## Resources and lifecycle
Hierarchy: ad account → campaign → ad group → ad. Create in that order. Every level has `POST /{resource}/{id}/activate|pause|archive`; archive is irreversible. An ad serves only if its ad group and campaign are active.
- **Ad account**: `GET /ad_account` (`id`, `name`, `url`, `preview_url`, `status`, `timezone`, `currency_code`, `review.status`), `POST /ad_account/brand`, spend limit windows (`/ad_account/spend_limit_windows`, `/ad_account/daily_spend_limit`). Windows and daily limit cannot overlap; limits cover billable ad spend only.
- **Files**: `POST /v1/upload` with `{"image_url": ...}` returns a `file_id` used by creatives. Images at least 640×640, JPEG/PNG/WebP. Request limits: 16 MiB inline, 500,000,000 bytes for file uploads.
- **Campaign**: `name` (3–1000), `status` (`active`/`paused`), budget (`lifetime_spend_limit_micros`, min 1,000,000, or `daily_spend_limit_micros`), optional `start_time`, `end_time`, `description`, `mode` (`product_feed`), `bidding_type` (`impressions`/`clicks`/`conversions`), `conversion_event_setting_ids`, targeting (`locations.include`, `excluded_locations`, `platforms.included` ∈ `ios_app`/`android_app`/`web`, `custom_audiences.ids`, `excluded_custom_audiences.ids`). A daily budget cannot be switched back to lifetime; create a new campaign. Lifetime budgets are for fixed-bid campaigns and cannot drop below spend so far.
- **Ad group**: `campaign_id`, `name`, `status`, `context_hints` (up to 2,000; an update replaces the entire list), `product_set`, `bidding_config`.
  - `bidding_config.billing_event_type`: `impression` or `click`.
  - `bidding_config.strategy`: `fixed_bid` (needs `max_bid_micros`; works with impressions, clicks, conversions; supports `custom_audience_bid_multipliers` 0.1×–10×), `maximize_clicks` (clicks objective only), `maximize_conversions` (conversions objective only; requires `billing_event_type: click`; no bid multipliers).
  - Conversion-optimised campaigns need exactly one active *standard* conversion event setting from the same ad account; custom events cannot be optimisation goals.
- **Ad**: `ad_group_id`, `name` (3–1000), `status` (`active`/`paused`/`archived`), `creative`. A full creative object is required on update; ads cannot move between ad groups. Creative types: `chat_card` (`title` 3–50 chars, `body` ≤100 chars, `target_url` http/https ≤2,048 chars, `file_id`, optional `price`) and `product_ad_template` (a product-feed ad group allows at most one non-archived template ad). `review_status` ∈ `in_review`/`approved`/`rejected` with a `review` object; `POST /ads/{id}/preview` returns a link valid for 24 hours; `include[]=serving_issues` on reads explains why something is not serving.
- Query-string tracking templates combine across campaign, ad group and ad; the destination URL wins on duplicate keys.
- Editing nested settings (targeting, conversion events): read the current object first and resend what must be preserved.

## Reporting
- `GET /ad_account/insights`, `/campaigns/{id}/insights`, `/ad_groups/{id}/insights`, `/ads/{id}/insights`. Metrics: `impressions`, `clicks`, `spend`, `ctr`, `cpc`, `cpm`. Breakdowns: `country`, `device`, `platform`, `product`. `time_granularity` ∈ `hourly`/`daily`/`monthly`/`none`. Date ranges: `unix_range`, `hour_range`, `date_range`. Max span 365 days within the last 5 years; `limit` 1–2,000.
- `POST /v1/conversions/insights`: conversion counts with `attribution_time_basis` (`ad_event_time`/`conversion_time`), `attribution_window_days` (7/14/30), `view_through_attribution_window_days` (0–1). Max 2,000 summary rows (plus 2,000 event rows when expanded); over the limit returns HTTP 413 with no partial data; no cursor, so split by entity or date.

## Conversions
- Server events: `POST https://bzr.openai.com/v1/events?pid=<PIXEL-ID>` (Bearer auth), up to 1,000 events per batch, one bad event fails the whole batch, `validate_only` available. Event: `id`, `type` (for example `lead_created`, `order_created`, `page_viewed`, `custom` with `custom_event_name`), `timestamp_ms` (within the last 7 days, at most 10 minutes ahead), `action_source`, `source_url` for web, `oppref` (OpenAI attribution id), `user`, `data`.
- User matching fields are SHA-256 hex of normalised values (`emails_sha256`, `phone_numbers_sha256`, ...); normalisation: trim and lowercase email; strip formatting from phone numbers. Deduplication key is pixel id + event name + `id`; the first event wins.
- Web tracking alternatives: measurement pixel, image tag, multiple pixels.

## Bulk API
`POST /bulk_mutation_jobs` (1–1,000 operations, `validate_only`, `partial_failure`), `GET /bulk_mutation_jobs/{id}`, `GET /bulk_mutation_jobs/{id}/operations`. Job states `pending`, `in_progress`, `completed`, `partially_failed`, `failed`. Operation results `created`/`updated`/`validated`/`failed` (with `error_code`, `retryable`, `retry_after_seconds`)/`skipped`. Each create carries an `idempotency_key`. Limits: 512 KiB per operation, 16 MiB per request, 5,000 non-archived campaigns/ad groups/ads each.

## Not documented (must be probed in a live low-budget account)
Exact error body shape and the list of serving-issue codes, rejection reason codes, sandbox availability, and regional availability for Canadian advertisers.

## Design consequences for our adapter
1. Use `validate_only` on the bulk API as the dry-run for the approval step (`AdsProvider.validate_plan`).
2. Prefer `daily_spend_limit_micros` for Maximize Results strategies; guard lifetime limits for fixed-bid.
3. Send `Idempotency-Key` on every create, derived from the ProposedAction id and step.
4. Treat `in_review` as a normal state after creation; only `rejected` raises an alert.
5. Always resend full `context_hints`, `creative` and nested targeting on update (merge on our side).
