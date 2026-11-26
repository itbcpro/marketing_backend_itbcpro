# Content studio: status

Multi-channel social content: research what is happening, plan a calendar, write posts and make
pictures or video, then publish or hand over. Lives in `apps/api/studio`, `apps/web/src/features/studio`
and `prompts/studio/*`. Page: `/studio`.

Verified with 142 studio tests (fake model, fake feeds, fake Instagram) and 8 frontend tests, plus a
live run in the dev stack with the real model and real news feeds (ideas, posts, daily routine,
approval, hand-posting kit). **Not yet run against a live Instagram account** (publishing goes through
the existing, fake-tested Instagram pipeline). Facebook and LinkedIn have no publisher yet (see Open).

## Several companies in one studio

A workspace can run several companies (brands), for example ITBCPro, ITBCSoftware and ShiftVale. The
switcher at the top of `/studio` chooses the company; every studio request carries it as
`X-Studio-Brand` and everything is kept apart per company: strategy, trend sources and signals, ideas,
posts, runs, design-family history, brand colours, logo, and the social account each channel posts to.
Rules that keep companies from leaking into each other:

- A post only goes to an account chosen in that company's channel settings. With several companies an
  account is never guessed; not chosen means posts are prepared for hand posting.
- A logo is matched to a company by its name or tags (an upload called "ITBCPro logo" belongs to
  ITBCPro). A company with no logo of its own gets none; it never borrows another's.
- Prompts name the company; the workspace-wide brand profile is not used when there are several.
- Existing data from before this change became one company named after the workspace (migration
  `0002_brands`).

## How it works

1. **Strategy** (`/studio`, Strategy tab) is data, not code: goals, audience, offers, content pillars with
   weights, voice (tone, banned phrases, emoji), visual style and source order, topics to follow and
   avoid, and per channel: on or off, post types, posts per week, days, times, who publishes.
2. **Research**: trend sources collect signals (any RSS or Atom feed, news about a topic, trending
   searches by country, a Reddit community, Hacker News, and the company's own rising Search Console
   queries). Sources are fetched through the SSRF guard, deduplicated, filtered by topics to avoid and
   pruned by age. A failing source never stops the others.
3. **Ideas**: the strategist (`prompts/studio/trend-ideas`) turns signals plus strategy, recent posts and
   measured results into channel-neutral ideas: angle, why now, pillar, timely or evergreen, which
   channel and post type fits, and what only the owner can supply. At least half should not depend on a
   trend. Signals are fenced as untrusted text and the model has no tools.
4. **Calendar**: free slots come from each channel's cadence in the workspace time zone
   (`studio/slots.py`, pure functions). Ideas are assigned by score, timeliness, pillar weights and
   post-type rotation. One idea can become a post on several channels.
5. **Writing**: one post per channel and type (`prompts/studio/piece-writer`) within that channel's
   limits (`studio/channels`). The text is checked by `studio/voice.py` (an editable phrase and pattern
   list in `studio/data/ai_tells.json`, plus the owner's banned phrases); if it reads as machine written,
   the writer gets one repair pass. Banned phrases block approval.
6. **Pictures and video**: people read the picture, not the caption, so the message lives in the
   picture. Per channel the owner picks the cover style: **designed** (default: the image model draws
   the finished cover, photo, headline and accent colours together from `prompts/studio/cover-design`;
   Claude then reads the picture back, `studio/visuals/proofread.py`, and it is drawn again up to
   three times, told what was wrong, until the text is exact; if it never is, a plain photo gets the
   headline set by the studio, `overlay.py`), **headline** (plain photo, studio-set headline) or
   **plain**. Designed feed pictures are drawn square (1:1) and never cropped, because image models do not reliably keep text away from the edges and cropping to 4:5 cut headlines off; the last of three tries drops the supporting line, since less text means fewer mistakes. Every prompt and every proofreading verdict is kept on the post and shown in the editor ("How the picture was made"). Carousels open with a photo cover; inner slides are exact text cards or, by choice,
   designed and proofread one by one. Real photos from the media library (never reused within 60
   days) get a studio-set headline. The writer puts the real content in the pictures and slides and
   keeps Instagram and Facebook captions short. A post using an AI-made picture gets a "label it as
   AI" warning (it also keeps the post out of automatic approval). Video is rendered locally from the
   plan; its cover is designed like any cover. Sources are tried in the owner's order and a source
   that cannot help hands over to the next one; text is never lost.
7. **Publishing**, per channel: `manual` (kit to paste, files to download, mark as posted), `review`
   (a person approves, the platform posts) or `auto` (a clean post is approved by the person who enabled
   automation, whose right to approve is re-checked at that moment; anything with a warning waits).
   Instagram goes through the existing post pipeline (checks, approval record, scheduling, kill switch,
   audit, verification). A channel without a connected account falls back to the manual kit.
8. **Daily routine**: Celery beat `studio.daily_tick` (every 15 minutes) starts a run for each workspace
   whose hour has come: collect trends, fill the calendar for the coming days, start writing. Runs are
   recorded and visible. "Run it now" does the same on demand; "Pause everything automatic" is the
   panic button.

## Running it

Needs the worker (queues `agents`, `ingest.social`, `publish.social`, `low` are already in
`make dev-worker`) and `make dev-beat` for the daily routine and hand-post reminders. A Claude key is
needed for ideas and posts; an OpenAI image key (`openai_images` connection) only for AI photographs.
Restart the worker after pulling new code.

## Extending (no existing code changes)

- **Channel**: register a `ChannelSpec` (`studio/channels/builtin.py`): post types, limits, shapes.
- **Trend source**: a class with `kind`, `params_model`, `fetch`; register in `studio/bootstrap.py`. The
  UI form is generated from the parameter schema.
- **Picture or video provider** (Runway, Veo, Kling, a stock library): implement the port in
  `studio/visuals/ports.py` and register it; the owner orders it in the strategy.
- **Publisher** (Facebook Pages, LinkedIn): implement `studio/publishers/ports.Publisher`
  (`accounts`, `hand_over`, `refresh`, `withdraw`) and register it; the channel then publishes instead
  of falling back to the manual kit.

## Open

- Facebook Pages and LinkedIn publishers (needs their OAuth apps and connection providers). Until then
  those channels produce ready-to-post kits.
- Real footage and voice-over for video (only branded text scenes today; a vendor provider is the slot).
- Run against a live Instagram account; Meta app review applies as for the rest of the Instagram module.
- Learning loop: results of published studio posts are visible in the Instagram module; feeding them
  back per idea or pillar is not built beyond the "what worked" digest the strategist already reads.
- Studio agent tools (letting the command box create ideas or posts) are not registered.
