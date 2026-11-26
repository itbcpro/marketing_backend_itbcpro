"""Validated payloads of the Google Ads action kinds.

Limits mirror Google's documented rules so a bad request is rejected before a person sees it.
Money is integer micros of the account currency. Everything is created paused.
"""

import uuid
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

MIN_BUDGET_MICROS = 1_000_000
HEADLINE_CHARS = 30
LONG_HEADLINE_CHARS = 90
DESCRIPTION_CHARS = 90
BUSINESS_NAME_CHARS = 25
PATH_CHARS = 15
KEYWORD_CHARS = 80
MAX_KEYWORDS = 100

MatchType = Literal["exact", "phrase", "broad"]
Resource = Literal["campaigns", "ad_groups", "ads", "asset_groups"]
Bidding = Literal["manual_cpc", "maximize_clicks", "maximize_conversions"]
PMaxBidding = Literal["maximize_conversions", "maximize_conversion_value"]

Headline = Annotated[str, Field(min_length=1, max_length=HEADLINE_CHARS)]
Description = Annotated[str, Field(min_length=1, max_length=DESCRIPTION_CHARS)]
LongHeadline = Annotated[str, Field(min_length=1, max_length=LONG_HEADLINE_CHARS)]

# Geo target and language constants used by the creation forms (Google's documented ids).
COUNTRY_IDS = {
    "US": 2840, "CA": 2124, "GB": 2826, "AU": 2036, "NZ": 2554, "IE": 2372, "DE": 2276,
    "FR": 2250, "ES": 2724, "IT": 2380, "NL": 2528, "BE": 2056, "CH": 2756, "AT": 2040,
    "SE": 2752, "NO": 2578, "DK": 2208, "FI": 2246, "PL": 2616, "PT": 2620, "MX": 2484,
    "BR": 2076, "IN": 2356, "JP": 2392, "KR": 2410, "SG": 2702, "AE": 2784, "SA": 2682,
    "TR": 2792, "ZA": 2710,
}  # fmt: skip
LANGUAGE_IDS = {
    "en": 1000, "de": 1001, "fr": 1002, "es": 1003, "it": 1004, "ja": 1005, "da": 1009,
    "nl": 1010, "fi": 1011, "ko": 1012, "no": 1013, "pt": 1014, "sv": 1015, "zh": 1017,
    "ar": 1019, "hi": 1023, "ru": 1031, "tr": 1037,
}  # fmt: skip


class Connected(BaseModel):
    connection_id: uuid.UUID = Field(description="The Google Ads connection to act on")
    summary: str = Field(default="", description="One sentence a human approver will read")


class ResourceRef(Connected):
    resource: Resource
    resource_id: str


class Activate(ResourceRef):
    daily_spend_micros: int = Field(default=0, ge=0)


class UpdateBudget(Connected):
    campaign_id: str
    daily_spend_limit_micros: int = Field(ge=MIN_BUDGET_MICROS)


class UpdateCpcBid(Connected):
    ad_group_id: str
    max_bid_micros: int = Field(ge=1)


def _unique(values: list[str], label: str) -> list[str]:
    seen = {v.strip().lower() for v in values}
    if len(seen) != len(values):
        raise ValueError(f"{label} must all be different")
    return [v.strip() for v in values]


class UpdateRsa(Connected):
    """Replace the texts of a responsive search ad. Google sends the changed ad back to review."""

    ad_id: str
    headlines: list[Headline] = Field(min_length=3, max_length=15)
    descriptions: list[Description] = Field(min_length=2, max_length=4)

    @field_validator("headlines")
    @classmethod
    def unique_headlines(cls, value: list[str]) -> list[str]:
        return _unique(value, "headlines")

    @field_validator("descriptions")
    @classmethod
    def unique_descriptions(cls, value: list[str]) -> list[str]:
        return _unique(value, "descriptions")


class AddRsa(Connected):
    """A new responsive search ad beside the existing ones, to test copy rather than guess."""

    ad_group_id: str
    final_url: str = Field(pattern=r"^https://", max_length=2048)
    headlines: list[Headline] = Field(min_length=3, max_length=15)
    descriptions: list[Description] = Field(min_length=2, max_length=4)
    path1: str = Field(default="", max_length=PATH_CHARS)
    path2: str = Field(default="", max_length=PATH_CHARS)

    @field_validator("headlines")
    @classmethod
    def unique_headlines(cls, value: list[str]) -> list[str]:
        return _unique(value, "headlines")

    @field_validator("descriptions")
    @classmethod
    def unique_descriptions(cls, value: list[str]) -> list[str]:
        return _unique(value, "descriptions")


class Keyword(BaseModel):
    text: str = Field(min_length=1, max_length=KEYWORD_CHARS)
    match_type: MatchType = "phrase"
    max_cpc_micros: int | None = Field(default=None, ge=1)

    @field_validator("text")
    @classmethod
    def plain(cls, value: str) -> str:
        text = " ".join(value.split())
        if any(ch in text for ch in "!@%^()={};~`<>?\\|"):
            raise ValueError("keywords cannot contain special characters")
        return text


class AddKeywords(Connected):
    ad_group_id: str
    keywords: list[Keyword] = Field(min_length=1, max_length=MAX_KEYWORDS)


class AddNegatives(Connected):
    scope: Literal["campaign", "ad_group"]
    parent_id: str
    keywords: list[Keyword] = Field(min_length=1, max_length=MAX_KEYWORDS)


class PauseKeyword(Connected):
    ad_group_id: str
    criterion_id: str


class Sitelink(BaseModel):
    link_text: str = Field(min_length=1, max_length=25)
    description1: str = Field(default="", max_length=35)
    description2: str = Field(default="", max_length=35)
    final_url: str = Field(pattern=r"^https://", max_length=2048)


class Snippet(BaseModel):
    header: str = Field(min_length=1, max_length=25)
    values: list[Annotated[str, Field(min_length=1, max_length=25)]] = Field(
        min_length=3, max_length=10
    )


class AddExtensions(Connected):
    campaign_id: str
    sitelinks: list[Sitelink] = Field(default_factory=list, max_length=20)
    callouts: list[Annotated[str, Field(min_length=1, max_length=25)]] = Field(
        default_factory=list, max_length=20
    )
    snippets: list[Snippet] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def something(self) -> Self:
        if not (self.sitelinks or self.callouts or self.snippets):
            raise ValueError("add at least one extension")
        return self


class Targeting(BaseModel):
    countries: list[str] = Field(min_length=1, max_length=30)
    languages: list[str] = Field(default_factory=lambda: ["en"], min_length=1, max_length=10)

    @field_validator("countries")
    @classmethod
    def known_countries(cls, value: list[str]) -> list[str]:
        codes = [c.strip().upper() for c in value]
        unknown = [c for c in codes if c not in COUNTRY_IDS]
        if unknown:
            raise ValueError(f"unsupported country codes: {', '.join(unknown)}")
        return codes

    @field_validator("languages")
    @classmethod
    def known_languages(cls, value: list[str]) -> list[str]:
        codes = [c.strip().lower() for c in value]
        unknown = [c for c in codes if c not in LANGUAGE_IDS]
        if unknown:
            raise ValueError(f"unsupported languages: {', '.join(unknown)}")
        return codes


MAX_AD_GROUPS = 20
MAX_ADS_PER_GROUP = 3


class AdCopy(BaseModel):
    """One responsive search ad: its landing page and texts."""

    final_url: str = Field(pattern=r"^https://", max_length=2048)
    headlines: list[Headline] = Field(min_length=3, max_length=15)
    descriptions: list[Description] = Field(min_length=2, max_length=4)
    path1: str = Field(default="", max_length=PATH_CHARS)
    path2: str = Field(default="", max_length=PATH_CHARS)

    @field_validator("headlines")
    @classmethod
    def unique_headlines(cls, value: list[str]) -> list[str]:
        return _unique(value, "headlines")

    @field_validator("descriptions")
    @classmethod
    def unique_descriptions(cls, value: list[str]) -> list[str]:
        return _unique(value, "descriptions")


class AdGroupPlan(BaseModel):
    """One tightly themed ad group: its keywords and two or three ads that test different angles."""

    name: str = Field(min_length=3, max_length=200)
    max_cpc_micros: int | None = Field(default=None, ge=1)
    keywords: list[Keyword] = Field(min_length=1, max_length=MAX_KEYWORDS)
    negative_keywords: list[Keyword] = Field(default_factory=list, max_length=MAX_KEYWORDS)
    ads: list[AdCopy] = Field(min_length=1, max_length=MAX_ADS_PER_GROUP)


_LEGACY_GROUP = (
    "ad_group_name", "keywords", "negative_keywords", "final_url", "headlines", "descriptions",
    "path1", "path2",
)  # fmt: skip


class SearchCampaign(Connected):
    """A paused Search campaign: budget, campaign, targeting and one or more ad groups with ads."""

    name: str = Field(min_length=3, max_length=200)
    daily_budget_micros: int = Field(ge=MIN_BUDGET_MICROS)
    bidding: Bidding = "maximize_clicks"
    max_cpc_micros: int | None = Field(default=None, ge=1)
    targeting: Targeting
    negative_keywords: list[Keyword] = Field(default_factory=list, max_length=MAX_KEYWORDS)
    ad_groups: list[AdGroupPlan] = Field(min_length=1, max_length=MAX_AD_GROUPS)
    search_partners: bool = False

    @model_validator(mode="before")
    @classmethod
    def single_group_shape(cls, data: Any) -> Any:
        """Actions saved before ad groups existed carry one flat ad group; wrap it."""
        if not isinstance(data, dict) or "ad_groups" in data or "ad_group_name" not in data:
            return data
        rest = {k: v for k, v in data.items() if k not in _LEGACY_GROUP}
        ad = {k: data[k] for k in ("final_url", "headlines", "descriptions", "path1", "path2")
              if k in data}  # fmt: skip
        group = {
            "name": data["ad_group_name"],
            "keywords": data.get("keywords", []),
            "negative_keywords": data.get("negative_keywords", []),
            "ads": [ad],
        }
        return {**rest, "ad_groups": [group]}

    @model_validator(mode="after")
    def distinct_groups(self) -> Self:
        names = [g.name.strip().lower() for g in self.ad_groups]
        if len(set(names)) != len(names):
            raise ValueError("ad group names must all be different")
        return self


class AddAdGroup(Connected):
    """A new ad group, with its keywords and ads, inside a campaign that already exists."""

    campaign_id: str
    ad_group: AdGroupPlan


class ImageAsset(BaseModel):
    media_id: uuid.UUID = Field(description="An image from the asset library")
    role: Literal[
        "marketing_image", "square_marketing_image", "portrait_marketing_image", "logo",
        "landscape_logo",
    ]  # fmt: skip


class AssetGroupContent(BaseModel):
    name: str = Field(min_length=3, max_length=200)
    final_url: str = Field(pattern=r"^https://", max_length=2048)
    headlines: list[Headline] = Field(min_length=3, max_length=15)
    long_headlines: list[LongHeadline] = Field(min_length=1, max_length=5)
    descriptions: list[Description] = Field(min_length=2, max_length=5)
    business_name: str = Field(min_length=1, max_length=BUSINESS_NAME_CHARS)
    images: list[ImageAsset] = Field(min_length=3, max_length=40)
    youtube_video_ids: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{11}$")]] = Field(
        default_factory=list, max_length=15
    )

    @model_validator(mode="after")
    def required_roles(self) -> Self:
        roles = {i.role for i in self.images}
        missing = {"marketing_image", "square_marketing_image", "logo"} - roles
        if missing:
            raise ValueError(f"missing image roles: {', '.join(sorted(missing))}")
        return self

    @field_validator("headlines")
    @classmethod
    def unique_headlines(cls, value: list[str]) -> list[str]:
        return _unique(value, "headlines")


class PmaxCampaign(Connected):
    """A paused Performance Max campaign with one asset group built from library images."""

    name: str = Field(min_length=3, max_length=200)
    daily_budget_micros: int = Field(ge=MIN_BUDGET_MICROS)
    bidding: PMaxBidding = "maximize_conversions"
    target_cpa_micros: int | None = Field(default=None, ge=1)
    target_roas: float | None = Field(default=None, gt=0)
    targeting: Targeting
    asset_group: AssetGroupContent


class AddAssets(Connected):
    """Add assets to an existing asset group (texts, library images, YouTube videos)."""

    asset_group_id: str
    headlines: list[Headline] = Field(default_factory=list, max_length=15)
    long_headlines: list[LongHeadline] = Field(default_factory=list, max_length=5)
    descriptions: list[Description] = Field(default_factory=list, max_length=5)
    images: list[ImageAsset] = Field(default_factory=list, max_length=40)
    youtube_video_ids: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{11}$")]] = Field(
        default_factory=list, max_length=15
    )

    @model_validator(mode="after")
    def something(self) -> Self:
        if not (
            self.headlines
            or self.long_headlines
            or self.descriptions
            or self.images
            or self.youtube_video_ids
        ):
            raise ValueError("add at least one asset")
        return self


class RemoveAsset(Connected):
    asset_group_id: str
    asset_id: str
    field_type: str = Field(pattern=r"^[A-Z_]+$", max_length=40)
