# P4 Google Ads: status

Verified against fake Google Ads server only (536 backend tests, 16 frontend tests). Not yet run against a live account.

## Done
- Asset library (images, video files, YouTube links, crops, per-destination checks) under `/assets`.
- Connection: Google sign-in (offline token), account picker with manager (`login-customer-id`), health check.
- REST client (v25): search with paging, mutate, `validateOnly`, token refresh on 401, error mapping.
- Sync into the shared mirror: campaigns, ad groups, responsive search ads, asset groups and their assets, keywords, budgets, impression share, conversion actions, 30 days of daily numbers, change history.
- Shared dashboards at every level (campaign, ad group, ad, asset group, keyword) with Google's own status, ad strength, impression share and automatic checks.
- AI: persisted reviews compared with the previous one, three responsive-search-ad versions (choose one to publish), keyword suggestions from real search terms (apply selected).
- Actions (risk classed, approval, validate-only preview): pause/activate/archive, budget, keyword bid, ad text, keywords, negatives, extensions, Search campaign, Performance Max campaign, asset group assets add/remove.
- Conversion audit and alerts (opened, kept and resolved automatically after each sync; daily sync via Celery beat).
- Screens: `/ads/google` (connect, accounts, campaigns, all ads, AI health, conversions, alerts, create campaign), `/ads/google/<id>` dashboards.

## Open
- Enhanced conversions, offline conversion import, billing/quota alerts, daily digest and weekly narrative.
- Uploading video to YouTube (today: link a YouTube video by id).
- Confirm language/geo constants, `containsEuPoliticalAdvertising` and asset REST shapes with `validateOnly` on a real account.

## Needed from the owner to test live
1. Developer token with access level (Google Ads manager account, API Center).
2. OAuth client (Google Cloud, Google Ads API enabled); add the redirect URI shown on the Google Ads page.
3. Manager and client customer IDs; a YouTube channel for video assets.
4. Set `APP_GOOGLE_OAUTH_CLIENT_ID`, `APP_GOOGLE_OAUTH_CLIENT_SECRET`, `APP_GOOGLE_ADS_DEVELOPER_TOKEN` and restart the API.
