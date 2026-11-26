# CRM-3 Send gateway and email channel

Needs: CRM-2. ADR 0010.

- [x] CRM-3.1 Gateway: permission, consent, window, quiet hours, plan limit, throttle; blocked-message records with reason codes.
- [x] CRM-3.2 `ChannelAdapter` port, contract test suite, fake adapter.
- [x] CRM-3.3 Email adapter behind a port, sending domains, DNS record generation and live verification (SPF, DKIM, DMARC, return path).
- [~] CRM-3.4 (done: raw email parser with quote stripping, auto-reply and bounce detection, threading ids, bounce and complaint handling, suppression list with gateway check, signed one-click unsubscribe with List-Unsubscribe headers; open: public inbound-email and provider-event webhooks, which come with the chosen provider, and the web page for the unsubscribe link) Inbound email parse, threading headers, bounce and complaint handling, suppression list, one-click unsubscribe.
- [x] CRM-3.5 Campaigns (audience from a filter frozen at launch, batch drip, schedule, pause, resume, cancel, per-recipient reasons, stats, open/click tracking and an A/B split with per-version results).
- [x] CRM-3.6 Usage ledger and wallet (versioned rate cards, SMS segment counting, per-message usage records, prepaid wallet ledger with idempotent references, markup per organization, low-balance event, prepaid and daily spend cap rules in the gateway, wallet and usage API). Rates and top-ups are added by management commands until a payment provider is chosen.
- [x] CRM-3.7 Email UI (domain setup, suppression list, unsubscribe page, template editor, campaign list, create form with tracking and A/B fields, results page with per-version opens and clicks).
