"""Publishing a post to Instagram: containers, processing, quota, publish, verify.

The steps are resumable. Container ids and the published media id are saved on the draft as soon as
Instagram returns them, so a retry after a crash or a still-processing reel continues instead of
creating a second post.
"""

import time
import uuid
from collections.abc import Callable
from typing import Any

from django.utils import timezone

from actions.models import ProposedAction
from connections.models import Connection, ConnectionStatus
from core.errors import (
    AuthExpiredError,
    DomainError,
    NotFoundError,
    PermanentProviderError,
    RateLimitedError,
    RetryableProviderError,
)
from core.logging import get_logger
from media.models import MediaAsset, MediaKind
from social import images, sync
from social.instagram import factory
from social.instagram.client import PROVIDER, InstagramClient
from social.models import DraftStatus, PostDraft, SocialAccount
from social.public_media import public_url
from social.schemas import PublishPost

log = get_logger(__name__)

sleeper: Callable[[float], None] = time.sleep
"""Replaced in tests so polling does not wait."""

WAIT_SECONDS = 150
POLL_FIRST = 3.0
POLL_MAX = 15.0
QUOTA_MARGIN = 2
DEFAULT_QUOTA = 50
FINISHED = "FINISHED"
FAILED_STATES = {"ERROR", "EXPIRED"}
PUBLISHED = "PUBLISHED"
RECOVERY_LOOKBACK = 5


def _connection(action: ProposedAction) -> Connection:
    found = (
        Connection.objects.filter(
            pk=str(action.effective_payload["connection_id"]),
            workspace_id=action.workspace_id,
            provider=PROVIDER,
        )
        .exclude(status=ConnectionStatus.REVOKED)
        .first()
    )
    if found is None:
        raise NotFoundError("Instagram connection not found for this workspace")
    return found


def _assets(workspace_id: uuid.UUID, payload: PublishPost) -> dict[uuid.UUID, MediaAsset]:
    wanted = {a.asset_id for a in payload.media}
    if payload.cover_asset_id:
        wanted.add(payload.cover_asset_id)
    found = {a.pk: a for a in MediaAsset.objects.filter(workspace_id=workspace_id, pk__in=wanted)}
    if wanted - set(found):
        raise NotFoundError("a media file of this post no longer exists")
    return found


def _ready(asset: MediaAsset) -> MediaAsset:
    """Instagram takes JPEG images; other formats are converted first."""
    if asset.kind == MediaKind.IMAGE and asset.file_format != "JPEG":
        return images.to_jpeg(asset)
    return asset


def _url(asset: MediaAsset) -> str:
    return public_url(_ready(asset))


def _child_fields(asset: MediaAsset, alt_text: str) -> dict[str, Any]:
    fields: dict[str, Any] = {"is_carousel_item": True}
    if asset.kind == MediaKind.VIDEO:
        fields.update({"media_type": "VIDEO", "video_url": _url(asset)})
    else:
        fields["image_url"] = _url(asset)
        if alt_text:
            fields["alt_text"] = alt_text[:1000]
    return fields


def _optional(payload: PublishPost) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if payload.location_id:
        fields["location_id"] = payload.location_id
    if payload.collaborators:
        fields["collaborators"] = payload.collaborators
    return fields


def _single_fields(payload: PublishPost, assets: dict[uuid.UUID, MediaAsset]) -> dict[str, Any]:
    entry = payload.media[0]
    asset = assets[entry.asset_id]
    if payload.kind == "story":
        key = "video_url" if asset.kind == MediaKind.VIDEO else "image_url"
        return {"media_type": "STORIES", key: _url(asset)}
    if payload.kind == "reel":
        fields: dict[str, Any] = {
            "media_type": "REELS",
            "video_url": _url(asset),
            "caption": payload.caption,
            "share_to_feed": payload.share_to_feed,
            **_optional(payload),
        }
        if payload.cover_asset_id:
            fields["cover_url"] = _url(assets[payload.cover_asset_id])
        return fields
    fields = {"image_url": _url(asset), "caption": payload.caption, **_optional(payload)}
    if entry.alt_text:
        fields["alt_text"] = entry.alt_text[:1000]
    return fields


def create_container(
    client: InstagramClient,
    payload: PublishPost,
    assets: dict[uuid.UUID, MediaAsset],
    state: dict[str, Any],
) -> str:
    if payload.kind != "carousel":
        return client.create_container(_single_fields(payload, assets))
    children: list[str] = list(state.get("children", []))
    for entry in payload.media[len(children) :]:
        children.append(
            client.create_container(_child_fields(assets[entry.asset_id], entry.alt_text))
        )
        state["children"] = children
    parent = {
        "media_type": "CAROUSEL",
        "children": ",".join(children),
        "caption": payload.caption,
        **_optional(payload),
    }
    return client.create_container(parent)


def wait_ready(client: InstagramClient, container_id: str, state: dict[str, Any]) -> None:
    """Poll until Instagram finished processing; a long reel raises a retryable error instead."""
    children = [str(c) for c in state.get("children", [])]
    deadline = time.monotonic() + WAIT_SECONDS
    delay = POLL_FIRST
    pending = [*children, container_id]
    while pending:
        status = client.container_status(pending[-1])
        code = str(status.get("status_code") or "")
        if code in {FINISHED, PUBLISHED}:
            pending.pop()
            continue
        if code in FAILED_STATES:
            raise PermanentProviderError(
                f"Instagram could not process the media: {status.get('status') or code}",
                provider=PROVIDER,
            )
        if time.monotonic() >= deadline:
            raise RetryableProviderError(
                "Instagram is still processing the media", provider=PROVIDER
            )
        sleeper(delay)
        delay = min(delay * 1.5, POLL_MAX)


def check_quota(client: InstagramClient) -> None:
    try:
        limit = client.publishing_limit()
    except AuthExpiredError:
        raise
    except DomainError:
        return  # the endpoint is informative; Instagram enforces the limit itself
    total = int((limit.get("config") or {}).get("quota_total") or DEFAULT_QUOTA)
    used = int(limit.get("quota_usage") or 0)
    if used + 1 > total - QUOTA_MARGIN:
        raise RateLimitedError(
            f"the 24 hour publishing limit is nearly reached ({used}/{total})", provider=PROVIDER
        )


def _recover(client: InstagramClient, payload: PublishPost) -> str | None:
    """A publish call that timed out may have worked: find the post by its caption."""
    for item in client.list_media(max_items=RECOVERY_LOOKBACK):
        if str(item.get("caption") or "") == payload.caption:
            return str(item["id"])
    return None


def publish_container(
    client: InstagramClient, container_id: str, payload: PublishPost, state: dict[str, Any]
) -> str:
    if state.get("publish_started"):
        status = client.container_status(container_id)
        if status.get("status_code") == PUBLISHED:
            found = _recover(client, payload)
            if found:
                return found
    state["publish_started"] = True
    return client.publish_container(container_id)


def _save_state(draft: PostDraft, state: dict[str, Any]) -> None:
    PostDraft.objects.filter(pk=draft.pk).update(publish_state=state, updated_at=timezone.now())


def _fail(draft: PostDraft, exc: DomainError) -> None:
    PostDraft.objects.filter(pk=draft.pk).update(
        status=DraftStatus.FAILED,
        error_code=exc.code.value,
        error_detail=str(exc)[:500],
        updated_at=timezone.now(),
    )


def _first_comment(client: InstagramClient, media_id: str, text: str) -> None:
    if not text:
        return
    try:
        client.post_comment(media_id, text)
    except DomainError:
        log.warning("instagram_first_comment_failed", media=media_id)


def _finish(
    client: InstagramClient,
    draft: PostDraft,
    account: SocialAccount | None,
    media_id: str,
    action_id: uuid.UUID,
) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    try:
        detail = client.media_detail(media_id)
    except DomainError:
        log.warning("instagram_permalink_lookup_failed", media=media_id)
    permalink = str(detail.get("permalink") or "")
    if account is not None and detail:
        sync.upsert_media(account, detail)
    PostDraft.objects.filter(pk=draft.pk).update(
        status=DraftStatus.PUBLISHED,
        external_media_id=media_id,
        permalink=permalink[:500],
        published_at=timezone.now(),
        action_id=action_id,
        error_code="",
        error_detail="",
        updated_at=timezone.now(),
    )
    return {"media_id": media_id, "permalink": permalink}


def execute(action: ProposedAction) -> dict[str, Any]:
    payload = PublishPost.model_validate(action.effective_payload)
    connection = _connection(action)
    draft = PostDraft.objects.filter(pk=payload.draft_id, workspace_id=action.workspace_id).first()
    if draft is None:
        raise NotFoundError("the draft of this post no longer exists")
    if draft.status == DraftStatus.PUBLISHED and draft.external_media_id:
        return {"media_id": draft.external_media_id, "permalink": draft.permalink}
    PostDraft.objects.filter(pk=draft.pk).update(status=DraftStatus.PUBLISHING)
    state = dict(draft.publish_state)
    try:
        assets = _assets(action.workspace_id, payload)
        with factory.open_client(connection) as client:
            media_id = str(state.get("media_id") or "")
            if not media_id:
                media_id = _publish_new(client, payload, assets, state, draft)
            if not state.get("commented"):
                _first_comment(client, media_id, payload.first_comment)
                state["commented"] = True
                _save_state(draft, state)
            account = SocialAccount.objects.filter(connection=connection).first()
            return _finish(client, draft, account, media_id, action.pk)
    except RetryableProviderError:
        raise
    except DomainError as exc:
        _fail(draft, exc)
        raise


def _publish_new(
    client: InstagramClient,
    payload: PublishPost,
    assets: dict[uuid.UUID, MediaAsset],
    state: dict[str, Any],
    draft: PostDraft,
) -> str:
    check_quota(client)
    container = str(state.get("container_id") or "")
    try:
        if not container:
            container = create_container(client, payload, assets, state)
            state["container_id"] = container
        wait_ready(client, container, state)
        media_id = publish_container(client, container, payload, state)
    finally:
        _save_state(draft, state)
    state["media_id"] = media_id
    _save_state(draft, state)
    return media_id


def verify(action: ProposedAction) -> bool:
    """The post exists on Instagram and carries the caption that was approved."""
    result = action.result or {}
    media_id = str(result.get("media_id") or "")
    if not media_id:
        return False
    payload = PublishPost.model_validate(action.effective_payload)
    with factory.open_client(_connection(action)) as client:
        detail = client.media_detail(media_id)
    return str(detail.get("caption") or "").strip() == payload.caption.strip()
