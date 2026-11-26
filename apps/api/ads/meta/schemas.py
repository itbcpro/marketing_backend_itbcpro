"""Validated payloads of the Meta Ads action kinds.

Limits mirror Meta's documented rules so a bad request is rejected before a person sees it. Money is
integer micros of the account currency. Everything is created paused.
"""

import uuid
from typing import Annotated, Literal, Self
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

MIN_BUDGET_MICROS = 1_000_000
NAME_CHARS = 200
HEADLINE_CHARS = 40
DESCRIPTION_CHARS = 30
PRIMARY_TEXT_CHARS = 2000
MAX_COUNTRIES = 25

Resource = Literal["campaigns", "ad_groups", "ads"]
Level = Literal["campaign", "ad_set"]
Objective = Literal[
    "OUTCOME_TRAFFIC", "OUTCOME_AWARENESS", "OUTCOME_ENGAGEMENT", "OUTCOME_LEADS", "OUTCOME_SALES"
]
CallToAction = Literal[
    "LEARN_MORE", "SHOP_NOW", "SIGN_UP", "CONTACT_US", "GET_QUOTE", "BOOK_TRAVEL", "DOWNLOAD",
    "APPLY_NOW", "SUBSCRIBE", "GET_OFFER", "ORDER_NOW", "SEND_MESSAGE",
]  # fmt: skip
CONVERSION_OBJECTIVES = {"OUTCOME_LEADS", "OUTCOME_SALES"}


class Connected(BaseModel):
    connection_id: uuid.UUID = Field(description="The Meta Ads connection to act on")
    summary: str = Field(default="", description="One sentence a human approver will read")


class ResourceRef(Connected):
    resource: Resource
    resource_id: str


class Activate(ResourceRef):
    daily_spend_micros: int = Field(default=0, ge=0)


class UpdateBudget(Connected):
    """Change a daily budget on the campaign (campaign budget) or on one ad set."""

    level: Level
    entity_id: str
    daily_spend_limit_micros: int = Field(ge=MIN_BUDGET_MICROS)


class CampaignPlan(Connected):
    """One paused campaign with one ad set and one image ad, created in a single approved step."""

    name: Annotated[str, Field(min_length=3, max_length=NAME_CHARS)]
    objective: Objective
    daily_budget_micros: int = Field(ge=MIN_BUDGET_MICROS)
    countries: list[Annotated[str, Field(min_length=2, max_length=2)]] = Field(
        min_length=1, max_length=MAX_COUNTRIES
    )
    age_min: int = Field(default=18, ge=18, le=65)
    age_max: int = Field(default=65, ge=18, le=65)
    page_id: str = Field(min_length=3, description="The Facebook Page the ad runs as")
    instagram_user_id: str = ""
    pixel_id: str = Field(default="", description="Required for lead and sales objectives")
    link: str
    headline: Annotated[str, Field(min_length=1, max_length=HEADLINE_CHARS)]
    primary_text: Annotated[str, Field(min_length=1, max_length=PRIMARY_TEXT_CHARS)]
    description: Annotated[str, Field(max_length=DESCRIPTION_CHARS)] = ""
    call_to_action: CallToAction = "LEARN_MORE"
    image_media_id: uuid.UUID
    utm_campaign: str = Field(default="", max_length=100)

    @field_validator("countries")
    @classmethod
    def _upper(cls, value: list[str]) -> list[str]:
        codes = [c.upper() for c in value]
        if len(set(codes)) != len(codes):
            raise ValueError("countries must all be different")
        return codes

    @field_validator("link")
    @classmethod
    def _https(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("the link must be a full https:// address")
        return value.strip()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.age_max < self.age_min:
            raise ValueError("the maximum age is below the minimum age")
        if self.objective in CONVERSION_OBJECTIVES and not self.pixel_id:
            raise ValueError("lead and sales campaigns need the pixel to count results")
        return self
