"""What differs between ad platforms, behind one small registry.

Screens, AI reviews and change history are shared; each platform only supplies how to sync,
which facts it reports about an item, which rule-based checks apply and what the AI should know
about the platform.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ads.google import checks as google_checks
from ads.google import sync as google_sync
from ads.meta import checks as meta_checks
from ads.meta import sync as meta_sync
from ads.models import AdEntity
from ads.openai import checks as openai_checks
from ads.openai import sync as openai_sync
from connections.models import Connection

OPENAI = "openai_ads"
GOOGLE = "google_ads"
META = "meta"


@dataclass(frozen=True, slots=True)
class AdsProvider:
    name: str
    label: str
    action_prefix: str
    sync: Callable[[Connection], dict[str, int]]
    platform_view: Callable[[AdEntity], dict[str, Any]]
    run_checks: Callable[[AdEntity, dict[str, Any]], list[dict[str, Any]]]
    guidance: str


OPENAI_GUIDANCE = (
    "OpenAI Ads places ads inside ChatGPT conversations. Copy should read like a helpful "
    "answer; context hints (8 to 20) tell the platform when an ad fits."
)
GOOGLE_GUIDANCE = (
    "Google Ads. Search campaigns use responsive search ads (up to 15 headlines of 30 characters "
    "and 4 descriptions of 90) matched to keywords; judge keyword relevance, match types, quality "
    "score, search terms, negatives, impression share lost to budget and to rank, and whether "
    "conversion tracking exists. Performance Max campaigns use asset groups: judge asset "
    "coverage (headlines, long headlines, descriptions, landscape and square images, logos, "
    "video) and asset performance labels. Conversions and cost per conversion matter more than "
    "clicks; say so when conversions are not tracked."
)

META_GUIDANCE = (
    "Meta Ads (Facebook and Instagram). Campaigns have an objective (awareness, traffic, "
    "engagement, leads, sales); ad sets hold budget, audience, placements and the optimisation "
    "goal; ads hold the creative. Judge creative first: a strong hook in the first line of the "
    "primary text, one clear offer, a call to action, a vertical or square visual. Judge the "
    "learning phase (ad sets need about 50 results a week to leave it; many small ad sets slow "
    "learning), frequency and creative fatigue, cost per result against the goal, and whether the "
    "pixel and conversion events exist for sales and lead objectives. Prefer broad targeting with "
    "Advantage+ over narrow interest stacks unless the business rules require otherwise."
)

PROVIDERS: dict[str, AdsProvider] = {
    OPENAI: AdsProvider(
        OPENAI,
        "OpenAI Ads",
        "ads.openai.",
        openai_sync.sync_connection,
        openai_checks.platform_view,
        openai_checks.run_checks,
        OPENAI_GUIDANCE,
    ),
    GOOGLE: AdsProvider(
        GOOGLE,
        "Google Ads",
        "ads.google.",
        google_sync.sync_connection,
        google_checks.platform_view,
        google_checks.run_checks,
        GOOGLE_GUIDANCE,
    ),
    META: AdsProvider(
        META,
        "Meta Ads",
        "ads.meta.",
        meta_sync.sync_connection,
        meta_checks.platform_view,
        meta_checks.run_checks,
        META_GUIDANCE,
    ),
}


def for_connection(connection: Connection) -> AdsProvider:
    return PROVIDERS[connection.provider]


def for_entity(entity: AdEntity) -> AdsProvider:
    return PROVIDERS[entity.connection.provider]
