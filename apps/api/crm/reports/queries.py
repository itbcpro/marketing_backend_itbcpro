"""Report queries. Each one answers a single question for a workspace and a period."""

from __future__ import annotations

import statistics
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from core.errors import ValidationFailedError
from crm.activity import services as activity
from crm.contacts import services as contacts
from crm.inbox import services as inbox
from crm.pipelines import services as pipelines
from crm.reports.models import FirstResponse
from workspaces.models import Membership

DELIVERED = ("delivered", "read")
ATTEMPTED = ("sent", "delivered", "read", "failed")
P90 = 0.9
SAMPLE_LIMIT = 50_000


def period(since: datetime | None, until: datetime | None) -> tuple[datetime, datetime]:
    """The period asked for, or the last ``CRM_REPORT_DEFAULT_DAYS`` days."""
    end = until or timezone.now()
    start = since or end - timedelta(days=settings.CRM_REPORT_DEFAULT_DAYS)
    if start >= end or (end - start).days > settings.CRM_REPORT_MAX_DAYS:
        raise ValidationFailedError("invalid_period")
    return start, end


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _summary(values: list[int]) -> dict[str, Any]:
    return {
        "count": len(values),
        "median_seconds": int(statistics.median(values)) if values else None,
        "p90_seconds": _percentile(values, P90),
        "average_seconds": int(statistics.fmean(values)) if values else None,
    }


def _answered(workspace_id: uuid.UUID, since: datetime, until: datetime) -> list[FirstResponse]:
    rows = FirstResponse.objects.for_workspace(workspace_id).filter(
        responded_at__gte=since, responded_at__lt=until
    )
    return list(rows[:SAMPLE_LIMIT])


def response_time(workspace_id: uuid.UUID, since: datetime, until: datetime) -> dict[str, Any]:
    rows = _answered(workspace_id, since, until)
    grouped: dict[tuple[str, str], list[int]] = defaultdict(list)
    for row in rows:
        grouped[("responder", row.responder_type)].append(row.seconds or 0)
        grouped[("channel", row.channel)].append(row.seconds or 0)
    waiting = FirstResponse.objects.for_workspace(workspace_id).filter(
        responded_at__isnull=True, inbound_at__lt=until
    )
    return {
        **_summary([r.seconds or 0 for r in rows]),
        "waiting_now": waiting.count(),
        "by_responder": [
            {"responder_type": key, **_summary(values)}
            for (kind, key), values in sorted(grouped.items())
            if kind == "responder"
        ],
        "by_channel": [
            {"channel": key, **_summary(values)}
            for (kind, key), values in sorted(grouped.items())
            if kind == "channel"
        ],
    }


def delivery(workspace_id: uuid.UUID, since: datetime, until: datetime) -> list[dict[str, Any]]:
    channels: dict[str, dict[str, Any]] = {}
    for row in inbox.delivery_stats(workspace_id, since, until):
        entry = channels.setdefault(
            str(row["channel"]), {"channel": row["channel"], "statuses": {}, "errors": {}}
        )
        entry["statuses"][row["status"]] = entry["statuses"].get(row["status"], 0) + row["count"]
        if row["error_code"]:
            entry["errors"][row["error_code"]] = row["count"] + entry["errors"].get(
                row["error_code"], 0
            )
    out = []
    for entry in channels.values():
        statuses = entry["statuses"]
        attempted = sum(statuses.get(s, 0) for s in ATTEMPTED)
        delivered = sum(statuses.get(s, 0) for s in DELIVERED)
        out.append(
            {
                "channel": entry["channel"],
                "statuses": statuses,
                "delivery_rate": round(delivered / attempted, 3) if attempted else None,
                "errors": [
                    {"code": code, "count": n}
                    for code, n in sorted(entry["errors"].items(), key=lambda i: -i[1])
                ],
            }
        )
    return sorted(out, key=lambda e: str(e["channel"]))


def pipeline(workspace_id: uuid.UUID, since: datetime, until: datetime) -> list[dict[str, Any]]:
    return pipelines.pipeline_report(workspace_id, since, until)


def attribution(workspace_id: uuid.UUID, since: datetime, until: datetime) -> dict[str, Any]:
    new = contacts.new_contacts_by_source(workspace_id, since, until)
    won = pipelines.won_value_by_contact(workspace_id, since, until)
    sources: dict[str, dict[str, Any]] = {}
    campaigns: dict[str, int] = defaultdict(int)
    for row in new:
        source = sources.setdefault(row["source"], {"source": row["source"], "contacts": 0})
        source["contacts"] += 1
        if row["campaign_id"]:
            campaigns[row["campaign_id"]] += 1
    # Value is credited to where the contact came from, even when they were created earlier.
    by_contact_source = {r["id"]: r["source"] for r in new}
    for contact_id, value in won.items():
        key = by_contact_source.get(contact_id)
        if key is not None:
            sources[key]["won_value_micros"] = sources[key].get("won_value_micros", 0) + value
    return {
        "sources": sorted(
            ({"won_value_micros": 0, **s} for s in sources.values()), key=lambda s: -s["contacts"]
        ),
        "campaigns": [
            {"campaign_id": cid, "contacts": n}
            for cid, n in sorted(campaigns.items(), key=lambda i: -i[1])
        ],
    }


def agents(workspace_id: uuid.UUID, since: datetime, until: datetime) -> list[dict[str, Any]]:
    sent = inbox.messages_by_sender(workspace_id, since, until)
    tasks = activity.tasks_completed_by_user(workspace_id, since, until)
    won = pipelines.won_by_owner(workspace_id, since, until)
    speeds: dict[str, list[int]] = defaultdict(list)
    for answered in _answered(workspace_id, since, until):
        if answered.responder_type == "human":
            speeds[answered.responder_id].append(answered.seconds or 0)
    members = Membership.objects.filter(workspace_id=workspace_id).select_related("user")
    out: list[dict[str, Any]] = []
    for member in members:
        key = str(member.user_id)
        row = {
            "user_id": key,
            "name": member.user.full_name or member.user.email,
            "messages_sent": sent.get(key, 0),
            "tasks_completed": tasks.get(key, 0),
            "deals_won": won.get(key, {}).get("count", 0),
            "won_value_micros": won.get(key, {}).get("value_micros", 0),
            "median_first_response_seconds": (
                int(statistics.median(speeds[key])) if speeds.get(key) else None
            ),
        }
        if any(row[k] for k in ("messages_sent", "tasks_completed", "deals_won")) or speeds.get(
            key
        ):
            out.append(row)
    return sorted(out, key=lambda r: -int(r["messages_sent"]))
