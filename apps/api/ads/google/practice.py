"""Best-practice review of a campaign draft, before anything is created (rule based, no AI).

Every check is a code plus parameters; the screen turns codes into sentences. Levels: ``fail``
(will hurt results or is likely to be rejected), ``warn`` (a known cost leak), ``info`` (advice),
``ok`` (done well). The review never blocks creation: the owner decides.
"""

import re
from collections import Counter
from typing import Any

from ads.google.schemas import AdGroupPlan, PmaxCampaign, SearchCampaign
from ads.openai.checks import Check, _check

IDEAL_HEADLINES = 15
IDEAL_DESCRIPTIONS = 4
MIN_KEYWORDS = 5
MIN_ADS_PER_GROUP = 2
MAX_KEYWORDS_PER_GROUP = 20
BROAD_SHARE_WARN = 0.5
MIN_DAILY_BUDGET_MICROS = 10_000_000
BUDGET_TO_CPC_RATIO = 10
IDEAL_IMAGES = 6
KEYWORD_HEADLINES = 2
CALL_TO_ACTION = (
    "call", "get", "book", "request", "contact", "start", "learn", "schedule", "talk",
    "discover", "see", "find", "compare", "buy", "order", "apply", "join", "try", "download",
)  # fmt: skip
INSERTION = re.compile(r"\{\s*keyword\s*:", re.IGNORECASE)
PHONE = re.compile(r"\d[\d\s().-]{6,}\d")
EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")
SHOUTING = re.compile(r"\b[A-Z]{4,}\b")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _copy(headlines: list[str], descriptions: list[str]) -> list[Check]:
    """Checks that apply to responsive text in both campaign types."""
    checks: list[Check] = []
    if any(INSERTION.search(t) for t in headlines + descriptions):
        checks.append(_check("copy_keyword_insertion", "warn"))
    if any("!" in h for h in headlines):
        checks.append(_check("copy_exclamation", "warn"))
    if any(PHONE.search(t) or EMOJI.search(t) for t in headlines + descriptions):
        checks.append(_check("copy_phone_or_emoji", "fail"))
    if any(SHOUTING.search(t) for t in headlines + descriptions):
        checks.append(_check("copy_all_caps", "warn"))
    return checks


def _headline_checks(headlines: list[str], descriptions: list[str], theme: str) -> list[Check]:
    checks: list[Check] = []
    if len(headlines) >= IDEAL_HEADLINES:
        checks.append(_check("rsa_headlines_full", "ok", count=len(headlines)))
    else:
        checks.append(
            _check("rsa_headlines_few", "warn", count=len(headlines), target=IDEAL_HEADLINES)
        )
    if len(descriptions) < IDEAL_DESCRIPTIONS:
        checks.append(
            _check(
                "rsa_descriptions_few", "warn", count=len(descriptions), target=IDEAL_DESCRIPTIONS
            )
        )
    theme_words = set(_words(theme))
    with_theme = [h for h in headlines if theme_words and theme_words <= set(_words(h))]
    if theme_words and len(with_theme) < KEYWORD_HEADLINES:
        checks.append(_check("rsa_theme_missing", "warn", keyword=theme, have=len(with_theme)))
    verbs = {w for t in headlines + descriptions for w in _words(t)}
    if not verbs.intersection(CALL_TO_ACTION):
        checks.append(_check("rsa_no_call_to_action", "info"))
    return checks


def review_search(plan: SearchCampaign, *, tracking: bool | None) -> list[Check]:
    """Review a Search campaign draft. ``tracking`` is None when conversion tracking is unknown."""
    checks: list[Check] = []
    if tracking is False:
        level = "fail" if plan.bidding == "maximize_conversions" else "warn"
        checks.append(_check("no_conversion_tracking", level))
    elif tracking:
        checks.append(_check("tracking_ok", "ok"))
    if plan.bidding == "maximize_conversions" and tracking is not True:
        checks.append(_check("bidding_needs_data", "warn"))
    if plan.bidding == "manual_cpc" and not plan.max_cpc_micros:
        checks.append(_check("manual_cpc_without_bid", "warn"))
    if plan.bidding == "maximize_clicks":
        checks.append(_check("clicks_bidding_advice", "info"))
    checks += _reach_checks(plan)
    shared = {n.text.lower() for n in plan.negative_keywords}
    if not shared and not any(g.negative_keywords for g in plan.ad_groups):
        checks.append(_check("negatives_missing", "warn"))
    many = len(plan.ad_groups) > 1
    for group in plan.ad_groups:
        checks += _group_checks(group, shared, group.name if many else "")
    checks.append(_check("extensions_advice", "info"))
    return checks


def _group_checks(group: AdGroupPlan, shared: set[str], label: str) -> list[Check]:
    """Keyword and ad checks for one ad group; ``label`` names the group when there are several."""
    checks = _keyword_checks(group, shared)
    top = group.keywords[0].text
    for ad in group.ads:
        checks += _headline_checks(ad.headlines, ad.descriptions, top)
        checks += _copy(ad.headlines, ad.descriptions)
    if not any(ad.path1 or ad.path2 for ad in group.ads):
        checks.append(_check("display_path_missing", "info"))
    if len(group.ads) < MIN_ADS_PER_GROUP:
        checks.append(_check("second_ad_advice", "info"))
    if label:
        for check in checks:
            check["params"] = {**check.get("params", {}), "group": label}
    return checks


def _reach_checks(plan: SearchCampaign) -> list[Check]:
    checks: list[Check] = []
    if plan.daily_budget_micros < MIN_DAILY_BUDGET_MICROS:
        checks.append(_check("budget_low", "warn"))
    if plan.max_cpc_micros and plan.daily_budget_micros < plan.max_cpc_micros * BUDGET_TO_CPC_RATIO:
        checks.append(_check("budget_vs_cpc", "warn", ratio=BUDGET_TO_CPC_RATIO))
    if plan.search_partners:
        checks.append(_check("search_partners_on", "warn"))
    if len(plan.targeting.countries) > 3:  # noqa: PLR2004
        checks.append(_check("many_countries", "info", count=len(plan.targeting.countries)))
    return checks


def _keyword_checks(group: AdGroupPlan, shared: set[str]) -> list[Check]:
    checks: list[Check] = []
    keywords = group.keywords
    if len(keywords) < MIN_KEYWORDS:
        checks.append(_check("keywords_few", "info", count=len(keywords), target=MIN_KEYWORDS))
    if len(keywords) > MAX_KEYWORDS_PER_GROUP:
        checks.append(
            _check("keywords_many", "warn", count=len(keywords), limit=MAX_KEYWORDS_PER_GROUP)
        )
    broad = sum(1 for k in keywords if k.match_type == "broad")
    if broad / len(keywords) > BROAD_SHARE_WARN:
        checks.append(_check("keywords_mostly_broad", "warn", broad=broad, total=len(keywords)))
    single = [k.text for k in keywords if len(k.text.split()) == 1]
    if len(single) > len(keywords) / 2:
        checks.append(_check("keywords_single_words", "warn", count=len(single)))
    seen = Counter((k.text.lower(), k.match_type) for k in keywords)
    if any(n > 1 for n in seen.values()):
        checks.append(_check("keywords_duplicates", "warn"))
    clash = {k.text.lower() for k in keywords} & (
        shared | {n.text.lower() for n in group.negative_keywords}
    )
    if clash:
        checks.append(_check("negative_blocks_keyword", "fail", words=", ".join(sorted(clash))))
    return checks


def review_pmax(plan: PmaxCampaign, *, tracking: bool | None) -> list[Check]:
    """Review a Performance Max draft: it lives or dies by conversion data and asset variety."""
    checks: list[Check] = []
    group = plan.asset_group
    if tracking is False:
        checks.append(_check("no_conversion_tracking", "fail"))
    elif tracking:
        checks.append(_check("tracking_ok", "ok"))
    if plan.daily_budget_micros < MIN_DAILY_BUDGET_MICROS:
        checks.append(_check("budget_low", "warn"))
    if len(group.images) < IDEAL_IMAGES:
        checks.append(
            _check("pmax_images_few", "warn", count=len(group.images), target=IDEAL_IMAGES)
        )
    if not group.youtube_video_ids:
        checks.append(_check("pmax_no_video", "info"))
    checks += _headline_checks(group.headlines, group.descriptions, "")
    checks += _copy(group.headlines + group.long_headlines, group.descriptions)
    return checks


def summarize(checks: list[Check]) -> dict[str, Any]:
    """Counts per level and a 0-100 score: fail costs 20 points, warn 8, info nothing."""
    counts = Counter(c["level"] for c in checks)
    score = max(0, 100 - counts["fail"] * 20 - counts["warn"] * 8)
    return {"score": score, "counts": dict(counts), "checks": checks}
