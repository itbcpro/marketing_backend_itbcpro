# P6 Instagram: status

Verified against a stateful fake Instagram server only (`apps/api/social/tests/fake_instagram.py`):
160 backend tests for the social app, 25 frontend tests. **Not yet run against a live account.** The
Graph API field names, metric names and error codes were written from Meta's documentation and must be
confirmed with a real test account (see "Needed from the owner").

## What exists

### See everything
- **Account**: profile, followers, account type, connection health, missing permissions, token expiry,
  last update, live (webhook) or polling mode.
- **Posts, reels, videos, carousels, stories**: synced into a local copy; grid with filters by format, sort by
  date or any metric (reach, views, interactions, saves, shares, engagement rate), caption search, unanswered
  comment counts. Posts that disappear from Instagram are marked removed.
- **Report of each post**: reach, views, interactions, saves, shares, engagement rate, reel watch time,
  numbers over time (daily snapshots), position among the account's own posts of the same format, and an
  **AI analysis** per post (verdict, why, what to learn, what to change, ideas built on it). A post younger
  than 48 hours is always "too early".
- **Account insights**: 28 day totals and daily series (reach, new followers, followers), audience
  demographics (age, gender, cities, countries) and peak hours, publishing quota (used of the 24 hour limit).
- **Statistics the AI reads**: medians per format, weekday, time of day (workspace time zone), caption length
  and hashtag count, with sample sizes; best and weakest posts; cadence and posting gaps; community numbers.
  Groups with fewer than five posts are labelled "too early" and have no lift value.

### AI manages the community
- **Comments**: ingested by polling (every 10 minutes) and webhooks; AI classifies (question, praise,
  complaint, lead, competitor, off topic, spam, abuse, sensitive, other), sentiment, language, priority and
  confidence, and drafts replies in the brand voice. Inbox with filters, edit the draft, send, hide, ignore,
  delete. Complaints, sensitive topics and urgent comments always go to a person.
- **Direct messages**: threads with summary, intent, urgency, sentiment, lead capture (name, need, contact,
  budget, timeline only when the person said it), 24 hour window indicator, take over and hand back, AI draft
  on demand, lead status.
- **Response settings** (per account): per comment category off, draft or automatic; automatic hiding of
  spam and abuse; confidence threshold; delay; hourly cap; maximum age; length; emoji policy; tone;
  sign-off; banned words; allowed link domains. For messages: enabled, off, draft or automatic;
  approved knowledge text; greeting; away message with business hours and time zone; always-human topics;
  handoff words; opt-out words; allowed domains; per hour and per conversation caps; lead capture.
  Allowing "automatic" needs the autonomy right. A panic button switches all automation off.
- **Safety** (see ADR 0006): untrusted text is fenced and the model has no tools; hard text rules in code;
  autonomy grants and the kill switch apply; opt-out is honoured before any model call.

### AI creates content
- **Studio**: write a post from a brief (image, carousel, reel plan, story): caption with a hook, two
  alternative openings, hashtags (three to five), first comment, alt text, carousel slides, reel script and
  shot list, rationale. Pictures: branded text cards drawn locally (exact text, AA contrast) or an AI image
  when an image service is connected, cropped to Instagram ratios and flagged as AI generated.
  Improve with feedback (keeps what works), redraw pictures, phone-style preview with the "more" cut-off.
- **Automatic checks** before approval: caption length, hook length, hashtag count, mentions, links, banned
  words from guardrails, carousel size, reel needs a video, aspect ratios, small images, missing alt text,
  AI disclosure note.
- **Reel videos**: the reel plan is rendered locally (ffmpeg bundled by `imageio-ffmpeg`, no vendor) into a
  1080x1920 MP4: hook, one branded text scene per on-screen line, call to action, fades, a progress bar, a
  silent audio track. Filming notes (beats, shot list) are never shown. A video the person attached is never
  replaced; "redraw pictures" re-renders a generated one. Played in the editor preview.
- **Four-week plan**: AI plans pillars, cadence, formats and times from the account's own results;
  one click puts every item on the calendar as an idea.
- **Calendar** by week plus a backlog of undated ideas and drafts.
- **Publishing**: approve a post (records who approved it), publish now or at a time; image, carousel,
  reel (with cover) and story; container flow with polling and resume; PNG converted to JPEG; quota check
  with a safety margin; first comment; verification of the published post; missed-slot protection;
  signed expiring public media links.
- **Account analysis**: whole-account AI report (verdict, score, what works, what does not, priorities with
  impact and effort, experiments, post ideas, risks, change since the last report) with history.

### Agents
`social-manager` (reads results, drafts posts) and `community-responder` (handles the inbox through
proposed actions). Tools: overview, stats, posts, inbox (untrusted, taints the run), draft post, reply,
hide, delete, toggle comments, send message. Agents cannot publish.

### Background jobs
Inbox cycle every 10 minutes (sync, triage, send what the settings allow), full sync every 6 hours,
automatic dispatch every minute, due posts every minute, token refresh daily. Needs `make dev-worker` and
`make dev-beat` (queues `ingest.social` and `publish.social` were added to the dev worker).

## Open
- Run against a live Business account and fix any field or metric name differences.
- Meta app review and the webhook configuration for production.
- Reel video beyond text scenes (P6.7): voiceover, music, subtitles from speech, AI footage from a vendor
  (Runway, Veo, Kling), story videos. Today the render is branded text scenes only.
- Hashtag research, mentions and UGC permission workflow (P6.11), collab and product tags (P6.5.8).
- Per-day outcome windows feeding memory (P6.6.3): daily snapshots are stored, the memory hook is not yet.
- Facebook Page management (P6.10).
- Playwright end-to-end tests for compose, approve, schedule, publish and DM handoff.

## Connecting
With the Meta app keys configured (once, by an administrator) the screen shows a single **Connect Instagram**
button that opens Instagram's own sign-in; the administrator steps are folded away. More accounts can be added
from the header. Webhook subscription per account happens automatically on the first sync. In development mode
the Meta app only accepts accounts added under Roles > Instagram Testers; App Review and Business Verification
are needed before arbitrary accounts can connect.

## Needed from the owner to test live
1. A Meta app with the product **Instagram API with Instagram Login**; add the redirect address and the
   webhook callback shown on the Instagram screen, subscribe to comments and messages.
2. An Instagram **Business or Creator** account with a role on the app (or app review for others).
3. `APP_INSTAGRAM_APP_ID`, `APP_INSTAGRAM_APP_SECRET`, `APP_INSTAGRAM_WEBHOOK_VERIFY_TOKEN`
   (or paste the keys on the Instagram screen) and `APP_PUBLIC_BASE_URL` set to a public https address
   (a tunnel while developing) so Instagram can download post media.
4. Optional: an OpenAI API key connection ("OpenAI image generation") for AI pictures.
5. A Claude API key (workspace connection or `APP_ANTHROPIC_API_KEY`) for everything AI.
