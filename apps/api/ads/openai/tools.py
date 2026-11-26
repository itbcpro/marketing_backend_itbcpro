"""Agent tools for OpenAI Ads: read tools plus one proposing tool per action kind."""

from datetime import timedelta
from typing import Any

from django.utils import timezone
from pydantic import BaseModel, Field

from actions.registry import get_action_kind
from ads.openai import actions
from ads.openai.actions import PREFIX
from ads.openai.client import PROVIDER
from agents.tools import ToolContext, action_tool, read_tool, registry
from connections.models import Connection, ConnectionStatus
from core.errors import NotFoundError

MAX_INSIGHT_DAYS = 90

WRITE_TOOLS = {
    "openai_ads_create_campaign": "campaign.create",
    "openai_ads_create_ad_group": "ad_group.create",
    "openai_ads_create_ad": "ad.create",
    "openai_ads_upload_image": "image.upload",
    "openai_ads_activate": "activate",
    "openai_ads_pause": "pause",
    "openai_ads_archive": "archive",
    "openai_ads_update_budget": "campaign.update_budget",
    "openai_ads_update_bid": "ad_group.update_bid",
    "openai_ads_update_campaign": "campaign.update",
    "openai_ads_update_ad_group": "ad_group.update",
    "openai_ads_update_ad": "ad.update",
}
TOOL_NAMES = ["openai_ads_overview", "openai_ads_insights", *WRITE_TOOLS]


def _connection(ctx: ToolContext) -> Connection:
    found = (
        Connection.objects.filter(workspace_id=ctx.workspace_id, provider=PROVIDER)
        .exclude(status__in=[ConnectionStatus.REVOKED, ConnectionStatus.PENDING])
        .order_by("created_at")
        .first()
    )
    if found is None:
        raise NotFoundError("No active OpenAI Ads connection in this workspace")
    return found


class OverviewInput(BaseModel):
    include_ads: bool = Field(default=False, description="Also list every ad group and ad")


def _overview(ctx: ToolContext, args: OverviewInput) -> dict[str, Any]:
    connection = _connection(ctx)
    with actions.client_factory(connection) as client:
        account = client.get_account()
        campaigns = client.list_resource("campaigns")
        tree: list[dict[str, Any]] = []
        for campaign in campaigns:
            node: dict[str, Any] = {"campaign": campaign}
            if args.include_ads:
                groups = client.list_resource("ad_groups", parent=("campaign_id", campaign["id"]))
                node["ad_groups"] = [
                    {
                        "ad_group": group,
                        "ads": client.list_resource("ads", parent=("ad_group_id", group["id"])),
                    }
                    for group in groups
                ]
            tree.append(node)
    return {"connection_id": str(connection.pk), "account": account, "campaigns": tree}


class InsightsInput(BaseModel):
    level: str = Field(default="account", pattern="^(account|campaign|ad_group|ad)$")
    entity_id: str | None = Field(default=None, description="Required unless level is account")
    days: int = Field(default=7, ge=1, le=MAX_INSIGHT_DAYS)
    granularity: str = Field(default="daily", pattern="^(hourly|daily|monthly|none)$")


def _insights(ctx: ToolContext, args: InsightsInput) -> dict[str, Any]:
    if args.level != "account" and not args.entity_id:
        raise NotFoundError("entity_id is required for this level")
    connection = _connection(ctx)
    until = timezone.now().date()
    since = until - timedelta(days=args.days)
    with actions.client_factory(connection) as client:
        rows = client.insights(
            args.level,
            args.entity_id,
            date_from=since.isoformat(),
            date_to=until.isoformat(),
            granularity=args.granularity,
        )
    return {"connection_id": str(connection.pk), "rows": rows}


def register_tools() -> None:
    registry.register(
        read_tool(
            "openai_ads_overview",
            "List the OpenAI Ads account, campaigns and (optionally) ad groups and ads with "
            "their status, budgets and review state. "
            "Returns the connection_id the write tools need.",
            OverviewInput,
            _overview,
        )
    )
    registry.register(
        read_tool(
            "openai_ads_insights",
            "Delivery metrics (impressions, clicks, spend, ctr, cpc, cpm) for the account or one "
            "campaign, ad group or ad over the last N days.",
            InsightsInput,
            _insights,
        )
    )
    for tool_name, suffix in WRITE_TOOLS.items():
        kind = PREFIX + suffix
        registry.register(action_tool(tool_name, kind, get_action_kind(kind).description))
