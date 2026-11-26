"""Launch an approved draft: image, campaign, ad groups, then their ads, all created paused.

No AI is involved here. Each step is a normal proposed action, created with a key derived from the
conversation and the step, so running the launcher again after a stop never duplicates anything: it
skips what exists and carries on. Progress is written to the conversation so the screen can show it.
"""

import ipaddress
import socket
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx
from django.db import transaction
from django.utils import timezone

from actions.models import ActionStatus
from ads.models import AdEntity, AdLevel, AssistSession, AssistStatus
from ads.openai import changes
from ads.openai.assist import MICROS, Draft
from connections.models import Connection
from core.errors import ConflictError, ValidationFailedError
from core.logging import get_logger
from identity.models import User

log = get_logger(__name__)

IMAGE_CHECK_SECONDS = 10.0
MAX_IMAGE_BYTES = 16 * 1024 * 1024


class Stop(Exception):  # noqa: N818 - control flow, not an error
    """A step cannot continue now. ``waiting`` means a human has to approve something first."""

    def __init__(self, message: str, *, waiting: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.waiting = waiting


def check_image_url(url: str) -> None:
    """Fail early when the link is a web page (or nothing) instead of a picture."""
    host = urlparse(url).hostname or ""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError as exc:
        raise ValidationFailedError("the image link could not be reached") from exc
    if any(
        ipaddress.ip_address(a).is_private or ipaddress.ip_address(a).is_loopback for a in addresses
    ):
        raise ValidationFailedError("the image link must be a public address")
    try:
        with httpx.stream(
            "GET", url, timeout=IMAGE_CHECK_SECONDS, follow_redirects=True
        ) as response:
            kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
            size = int(response.headers.get("content-length") or 0)
            ok = response.status_code == httpx.codes.OK
    except (httpx.HTTPError, ValueError) as exc:
        raise ValidationFailedError("the image link could not be reached") from exc
    if not ok:
        raise ValidationFailedError("the image link did not load")
    if kind not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValidationFailedError(
            "the link is not a JPEG, PNG or WebP image file (it looks like a web page); "
            "use the direct link to the picture"
        )
    if size > MAX_IMAGE_BYTES:
        raise ValidationFailedError("the image is larger than 16 MB")


def _log(session: AssistSession, kind: str, **fields: Any) -> None:
    session.history = [
        *session.history,
        {"type": kind, "at": timezone.now().isoformat(), **fields},
    ]
    session.save(update_fields=["history", "updated_at"])


def _step(
    session: AssistSession,
    connection: Connection,
    user: User,
    name: str,
    suffix: str,
    payload: dict[str, Any],
    summary: str,
) -> dict[str, Any]:
    action, _ = changes.submit(
        connection,
        user,
        suffix,
        payload,
        summary=summary,
        apply_now=True,
        idempotency_key=f"launch-{session.pk}-{name}",
        wait=True,
    )
    if action.status in changes.DONE:
        return dict(action.result or {})
    if action.status == ActionStatus.AWAITING_APPROVAL:
        raise Stop(
            f"“{summary}” is waiting in Approvals. Approve it, then press Continue.", waiting=True
        )
    detail = str(action.error or action.status)[:300]
    raise Stop(f"“{summary}” did not go through: {detail}")


def _campaign_payload(draft: Draft) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": draft.campaign_name,
        "objective": draft.objective,
        "budget": {"daily_spend_limit_micros": round(draft.daily_budget * MICROS)},
        "platforms": list(draft.platforms),
    }
    if draft.description:
        body["description"] = draft.description
    if draft.location_ids:
        body["location_ids"] = draft.location_ids
        body["excluded_location_ids"] = draft.excluded_location_ids
    else:
        body["countries"] = list(draft.countries)
    return body


def _group_payload(campaign_id: str, group: Any) -> dict[str, Any]:
    bidding: dict[str, Any] = {"strategy": group.strategy, "billing_event_type": "click"}
    if group.strategy == "fixed_bid" and group.max_bid:
        bidding["max_bid_micros"] = round(group.max_bid * MICROS)
    return {
        "campaign_id": campaign_id,
        "name": group.name,
        "context_hints": group.context_hints,
        "bidding": bidding,
    }


def _account_image(connection: Connection) -> tuple[str, str] | None:
    """An image already uploaded to this account: (file id, name of an ad that uses it)."""
    for ad in AdEntity.objects.filter(
        connection=connection, level=AdLevel.AD, missing_since__isnull=True
    ).order_by("-created_at"):
        file_id = (ad.data.get("creative") or {}).get("file_id")
        if file_id:
            return str(file_id), ad.name
    return None


def _resolve_image(session: AssistSession, connection: Connection, step: Any) -> str:
    """The file id for every ad: an upload, a given file id, or an image already in the account."""
    if session.image_url.startswith("https://"):
        uploaded = step(
            "image", "image.upload", {"image_url": session.image_url}, "Upload the ad image"
        )
        return str(uploaded["file_id"])
    if session.image_url:
        return session.image_url
    reused = _account_image(connection)
    if not reused:
        return ""
    _log(
        session,
        "launch_step",
        text=f"No image given: using the image of the ad {reused[1]} already in this account",
    )
    return reused[0]


def run(session_id: uuid.UUID, user_id: uuid.UUID) -> str:
    session = (
        AssistSession.objects.select_related("workspace", "connection")
        .filter(pk=session_id)
        .first()
    )
    user = User.objects.filter(pk=user_id).first()
    if session is None or user is None or not session.draft:
        return "skipped"
    draft = Draft.model_validate(session.draft)
    connection = session.connection
    created = {"campaign": 0, "groups": 0, "ads": 0}
    try:

        def step(name: str, suffix: str, payload: dict[str, Any], summary: str) -> dict[str, Any]:
            result = _step(session, connection, user, name, suffix, payload, summary)
            _log(session, "launch_step", text=summary)
            return result

        file_id = _resolve_image(session, connection, step)

        campaign = step(
            "campaign",
            "campaign.create",
            _campaign_payload(draft),
            f"Create campaign {draft.campaign_name}",
        )
        campaign_id = str(campaign["resource_id"])
        created["campaign"] = 1
        for g_index, group in enumerate(draft.ad_groups):
            made = step(
                f"group-{g_index}",
                "ad_group.create",
                _group_payload(campaign_id, group),
                f"Create ad group {group.name}",
            )
            created["groups"] += 1
            for a_index, ad in enumerate(group.ads if file_id else []):
                step(
                    f"group-{g_index}-ad-{a_index}",
                    "ad.create",
                    {
                        "ad_group_id": str(made["resource_id"]),
                        "name": ad.name,
                        "creative": {
                            "type": "chat_card",
                            "title": ad.title,
                            "body": ad.body,
                            "target_url": ad.url,
                            "file_id": file_id,
                        },
                    },
                    f"Create ad {ad.name}",
                )
                created["ads"] += 1
    except Stop as stop:
        _log(session, "launch_stopped", text=stop.message, waiting=stop.waiting, **created)
        return "waiting" if stop.waiting else "stopped"
    except Exception:
        log.exception("launch_crashed")
        _log(
            session,
            "launch_stopped",
            text="Something went wrong. Press Continue to retry.",
            **created,
        )
        return "crashed"
    _log(session, "launch_done", ads_skipped=0 if file_id else draft.ad_count, **created)
    return "done"


def begin(session: AssistSession, user: User, image: str) -> AssistSession:
    """Start (or resume) the launch in the background. ``image`` is an https link or a file id."""
    resumable = session.status == AssistStatus.LAUNCHED and (
        session.history and session.history[-1].get("type") == "launch_stopped"
    )
    if not (session.status == AssistStatus.DRAFT or resumable) or not session.draft:
        raise ConflictError("there is nothing to launch")
    if image.startswith("https://") and image != session.image_url:
        check_image_url(image)
    elif image and not image.startswith(("https://", "file_")):
        raise ValidationFailedError("the image must be an https link to a picture")
    session.image_url = image
    session.status = AssistStatus.LAUNCHED
    session.save(update_fields=["status", "image_url", "updated_at"])
    _log(session, "launch_started")
    from ads.tasks import launch_task

    transaction.on_commit(lambda: launch_task.delay(str(session.pk), str(user.pk)))
    return session
