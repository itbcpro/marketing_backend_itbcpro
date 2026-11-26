"""Provider-neutral pieces of keeping a local copy of an ad account.

Every provider maps its structure into the same entity levels and change tracking, so the screens,
the AI reviews and the change history work the same way for all of them.
"""

from typing import Any

from ads.models import AdChange, AdEntity, AdLevel
from connections.models import Connection

TRACKED: dict[str, tuple[str, ...]] = {
    AdLevel.CAMPAIGN: (
        "name", "status", "description", "budget", "start_time", "end_time", "targeting",
        "bidding_type",
    ),
    AdLevel.AD_GROUP: (
        "name", "status", "description", "context_hints", "bidding_config", "targeting",
        "learning_stage",
    ),
    AdLevel.AD: ("name", "status", "review_status", "creative"),
    AdLevel.ASSET_GROUP: ("name", "status", "final_urls", "asset_keys"),
    AdLevel.KEYWORD: ("name", "status", "match_type", "negative", "max_cpc_micros"),
}  # fmt: skip
CREATIVE_KEYS = (
    "type", "title", "body", "target_url", "file_id", "price", "headlines", "descriptions",
    "path1", "path2",
)  # fmt: skip


def tracked_view(level: str, row: dict[str, Any]) -> dict[str, Any]:
    view = {key: row.get(key) for key in TRACKED[level]}
    creative = view.get("creative")
    if isinstance(creative, dict):
        view["creative"] = {k: creative.get(k) for k in CREATIVE_KEYS if k in creative}
    return view


def diff_views(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in after
        if before.get(key) != after.get(key)
    }


def upsert_entity(
    connection: Connection,
    level: str,
    row: dict[str, Any],
    parent: AdEntity | None,
    now: Any,
) -> AdEntity:
    entity = AdEntity.objects.filter(
        connection=connection, level=level, external_id=row["id"]
    ).first()
    if entity is None:
        entity = AdEntity(
            workspace_id=connection.workspace_id,
            connection=connection,
            level=level,
            external_id=row["id"],
        )
    else:
        changes = diff_views(tracked_view(level, entity.data), tracked_view(level, row))
        if changes:
            AdChange.objects.create(
                workspace_id=connection.workspace_id, entity=entity, at=now, diff=changes
            )
    entity.parent = parent
    entity.name = str(row.get("name") or "")[:1000]
    entity.status = str(row.get("status") or "")[:20]
    entity.review_status = str(row.get("review_status") or "")[:20]
    entity.data = {k: v for k, v in row.items() if k not in {"ad_groups", "ads"}}
    entity.last_seen_at = now
    entity.missing_since = None
    entity.save()
    return entity
