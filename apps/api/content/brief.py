"""The audit brief: what the owner tells the reviewer about a page before it is judged."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from content.models import ContentItem, Website

Goal = Literal["", "leads", "sales", "bookings", "signups", "information", "other"]
MAX_KEYWORDS = 8


class Brief(BaseModel):
    primary_keyword: str = Field(default="", max_length=120)
    secondary_keywords: list[str] = Field(default_factory=list, max_length=MAX_KEYWORDS)
    country: str = Field(default="", max_length=80)
    region: str = Field(default="", max_length=120)
    audience: str = Field(default="", max_length=300)
    goal: Goal = ""

    def cleaned(self) -> dict[str, Any]:
        data = self.model_dump()
        data["secondary_keywords"] = [k.strip() for k in data["secondary_keywords"] if k.strip()]
        return {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()}


def merged(website: Website, item: ContentItem) -> dict[str, Any]:
    """The site's defaults, overridden by whatever the page's own brief fills in."""
    result = dict(Brief().cleaned())
    for source in (website.brief or {}, item.brief or {}):
        result.update({key: value for key, value in source.items() if value})
    return result
