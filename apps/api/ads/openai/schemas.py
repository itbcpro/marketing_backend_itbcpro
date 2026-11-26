"""Validated payloads of the OpenAI Ads action kinds.

The limits mirror the documented API rules so an invalid request is rejected before it ever
reaches a human approver. Money is always integer micros of the account currency.
"""

import re
import uuid
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

MIN_BUDGET_MICROS = 1_000_000
MIN_DAILY_BUDGET_MICROS = 25_000_000  # the platform rejects less (seen live: HTTP 400)
UNIX_MIN, UNIX_MAX = 946_684_800, 4_102_444_800
MAX_CONTEXT_HINTS = 2000
COUNTRY = re.compile(r"^[A-Z]{2}$")
LOCATION_ID = re.compile(r"^\d{1,12}$")
MAX_LOCATIONS = 2500

Name = Annotated[str, Field(min_length=3, max_length=1000)]
Objective = Literal["impressions", "clicks", "conversions"]
Strategy = Literal["fixed_bid", "maximize_clicks", "maximize_conversions"]
Resource = Literal["campaigns", "ad_groups", "ads"]
Platform = Literal["ios_app", "android_app", "web"]


class Connected(BaseModel):
    connection_id: uuid.UUID = Field(description="The OpenAI Ads connection to act on")
    summary: str = Field(default="", description="One sentence a human approver will read")


class Budget(BaseModel):
    """Exactly one of daily or lifetime."""

    daily_spend_limit_micros: int | None = Field(default=None, ge=MIN_BUDGET_MICROS)
    lifetime_spend_limit_micros: int | None = Field(default=None, ge=MIN_BUDGET_MICROS)

    @model_validator(mode="after")
    def exactly_one(self) -> Self:
        if (self.daily_spend_limit_micros is None) == (self.lifetime_spend_limit_micros is None):
            raise ValueError("give exactly one of daily or lifetime spend limit")
        return self

    def as_api(self) -> dict[str, int]:
        return self.model_dump(exclude_none=True)

    @property
    def amount_micros(self) -> int:
        return self.daily_spend_limit_micros or self.lifetime_spend_limit_micros or 0


class CampaignCreate(Connected):
    """Created paused. Turning delivery on is a separate, separately approved action."""

    name: Name
    description: str | None = None
    objective: Objective = "clicks"
    budget: Budget
    start_time: int | None = Field(default=None, ge=UNIX_MIN, le=UNIX_MAX)
    end_time: int | None = Field(default=None, ge=UNIX_MIN, le=UNIX_MAX)
    countries: list[str] = Field(default_factory=list)
    location_ids: list[str] = Field(
        default_factory=list,
        max_length=MAX_LOCATIONS,
        description="Region or market ids from the geo lookup. When given they replace countries",
    )
    excluded_location_ids: list[str] = Field(default_factory=list, max_length=MAX_LOCATIONS)
    platforms: list[Platform] = Field(default_factory=list)
    conversion_event_setting_id: str | None = None

    @field_validator("countries")
    @classmethod
    def country_codes(cls, value: list[str]) -> list[str]:
        if any(not COUNTRY.match(code) for code in value):
            raise ValueError("countries must be ISO 3166-1 alpha-2 codes in upper case")
        return value

    @field_validator("location_ids", "excluded_location_ids")
    @classmethod
    def location_codes(cls, value: list[str]) -> list[str]:
        if any(not LOCATION_ID.match(item) for item in value):
            raise ValueError("location ids are numeric strings from the geo lookup")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.objective == "conversions" and not self.conversion_event_setting_id:
            raise ValueError("a conversions campaign needs conversion_event_setting_id")
        if self.objective != "conversions" and self.conversion_event_setting_id:
            raise ValueError("conversion_event_setting_id is only for conversions campaigns")
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        daily = self.budget.daily_spend_limit_micros
        if daily is not None and daily < MIN_DAILY_BUDGET_MICROS:
            raise ValueError("a daily budget must be at least 25 in the account currency")
        if set(self.location_ids) & set(self.excluded_location_ids):
            raise ValueError("a location cannot be both included and excluded")
        return self

    def api_body(self) -> dict[str, object]:
        body: dict[str, object] = {
            "name": self.name.strip(),
            "status": "paused",
            "bidding_type": self.objective,
            "budget": self.budget.as_api(),
        }
        if self.description:
            body["description"] = self.description
        if self.start_time:
            body["start_time"] = self.start_time
        if self.end_time:
            body["end_time"] = self.end_time
        if self.conversion_event_setting_id:
            body["conversion_event_setting_ids"] = [self.conversion_event_setting_id]
        targeting: dict[str, object] = {}
        if self.location_ids:
            targeting["locations"] = {"include": [{"id": item} for item in self.location_ids]}
        elif self.countries:
            targeting["locations"] = {"countries": self.countries}
        if self.excluded_location_ids:
            targeting["excluded_locations"] = {
                "include": [{"id": item} for item in self.excluded_location_ids]
            }
        if self.platforms:
            targeting["platforms"] = {"included": self.platforms}
        if targeting:
            body["targeting"] = targeting
        return body


class Bidding(BaseModel):
    strategy: Strategy = "fixed_bid"
    billing_event_type: Literal["impression", "click"]
    max_bid_micros: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def compatible(self) -> Self:
        if self.strategy == "fixed_bid" and self.max_bid_micros is None:
            raise ValueError("fixed_bid needs max_bid_micros")
        if self.strategy != "fixed_bid":
            if self.max_bid_micros is not None:
                raise ValueError("maximize strategies must not set max_bid_micros")
            if self.billing_event_type != "click":
                raise ValueError("maximize strategies bill per click")
        return self

    def as_api(self) -> dict[str, object]:
        return self.model_dump(exclude_none=True)


class AdGroupCreate(Connected):
    campaign_id: str
    name: Name
    description: str | None = None
    context_hints: list[str] = Field(default_factory=list, max_length=MAX_CONTEXT_HINTS)
    bidding: Bidding

    def api_body(self) -> dict[str, object]:
        body: dict[str, object] = {
            "campaign_id": self.campaign_id,
            "name": self.name.strip(),
            "status": "paused",
            "bidding_config": self.bidding.as_api(),
        }
        if self.description:
            body["description"] = self.description
        if self.context_hints:
            body["context_hints"] = self.context_hints
        return body


class ChatCard(BaseModel):
    type: Literal["chat_card"] = "chat_card"
    title: str = Field(min_length=3, max_length=50)
    body: str = Field(min_length=1, max_length=100)
    target_url: str = Field(max_length=2048, pattern=r"^https?://")
    file_id: str


class AdCreate(Connected):
    ad_group_id: str
    name: Name
    creative: ChatCard

    def api_body(self) -> dict[str, object]:
        return {
            "ad_group_id": self.ad_group_id,
            "name": self.name.strip(),
            "status": "paused",
            "creative": self.creative.model_dump(),
        }


class ImageUpload(Connected):
    image_url: str = Field(pattern=r"^https://", max_length=2048)


class ResourceRef(Connected):
    resource: Resource
    resource_id: str


class Activate(ResourceRef):
    daily_spend_micros: int = Field(
        default=0, ge=0, description="Expected daily spend once delivery starts, for spend caps"
    )


class UpdateBudget(Connected):
    campaign_id: str
    budget: Budget


class UpdateBid(Connected):
    ad_group_id: str
    bidding: Bidding


class CampaignUpdate(Connected):
    campaign_id: str
    name: Name | None = None
    description: str | None = None
    end_time: int | None = Field(default=None, ge=UNIX_MIN, le=UNIX_MAX)

    @model_validator(mode="after")
    def something_changes(self) -> Self:
        if not (self.name or self.description is not None or self.end_time):
            raise ValueError("nothing to update")
        return self

    def api_body(self) -> dict[str, object]:
        return self.model_dump(
            exclude={"connection_id", "summary", "campaign_id"}, exclude_none=True
        )


class AdGroupUpdate(Connected):
    ad_group_id: str
    name: Name | None = None
    context_hints: list[str] | None = Field(default=None, max_length=MAX_CONTEXT_HINTS)

    @model_validator(mode="after")
    def something_changes(self) -> Self:
        if self.name is None and self.context_hints is None:
            raise ValueError("nothing to update")
        return self

    def api_body(self) -> dict[str, object]:
        return self.model_dump(
            exclude={"connection_id", "summary", "ad_group_id"}, exclude_none=True
        )


class AdUpdate(Connected):
    """The API needs the whole creative on update, so the caller sends it complete."""

    ad_id: str
    name: Name | None = None
    creative: ChatCard | None = None

    @model_validator(mode="after")
    def something_changes(self) -> Self:
        if self.name is None and self.creative is None:
            raise ValueError("nothing to update")
        return self

    def api_body(self) -> dict[str, object]:
        return self.model_dump(exclude={"connection_id", "summary", "ad_id"}, exclude_none=True)
