"""Tracked emails: rewrite the links, add the pixel, and count what comes back."""

from __future__ import annotations

import html
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction

from core.errors import NotFoundError
from crm.tracking.models import EmailEvent, EventKind

SALT = "crm.tracking"
OPEN_PATH = "/api/v1/crm-public/t/o"
CLICK_PATH = "/api/v1/crm-public/t/c"
LINK = re.compile(r"https?://[^\s<>\"']+")
TRAILING = ".,;:!?)"
SKIP_MARKERS = ("/unsubscribe/",)  # the way out is never wrapped
URL_LIMIT = 500
SECONDS_PER_DAY = 86_400
OPTION_OPENS = "track_opens"
OPTION_CLICKS = "track_clicks"


@dataclass(frozen=True)
class Decorated:
    text: str
    html: str


def _base() -> str:
    return settings.PUBLIC_BASE_URL.rstrip("/")


def _token(workspace_id: uuid.UUID, message_id: uuid.UUID, contact_id: uuid.UUID, url: str) -> str:
    return signing.dumps(
        {"w": str(workspace_id), "m": str(message_id), "c": str(contact_id), "u": url}, salt=SALT
    )


def _split_trailing(url: str) -> tuple[str, str]:
    cut = len(url)
    while cut and url[cut - 1] in TRAILING:
        cut -= 1
    return url[:cut], url[cut:]


def _as_html(text: str) -> str:
    """Escape the text and turn every link into an anchor, keeping punctuation outside it."""
    out: list[str] = []
    last = 0
    for match in LINK.finditer(text):
        url, tail = _split_trailing(match.group(0))
        out.append(html.escape(text[last : match.start()]))
        out.append(f'<a href="{html.escape(url)}">{html.escape(url)}</a>{html.escape(tail)}')
        last = match.end()
    out.append(html.escape(text[last:]))
    return "".join(out)


def decorate(
    workspace_id: uuid.UUID,
    message_id: uuid.UUID,
    contact_id: uuid.UUID,
    text: str,
    *,
    opens: bool,
    clicks: bool,
) -> Decorated:
    """The plain text with tracked links, and an HTML version that also carries the pixel."""

    def wrap(url: str) -> str:
        if not clicks or any(marker in url for marker in SKIP_MARKERS) or len(url) > URL_LIMIT:
            return url
        return f"{_base()}{CLICK_PATH}/{_token(workspace_id, message_id, contact_id, url)}"

    def swap(match: re.Match[str]) -> str:
        url, tail = _split_trailing(match.group(0))
        return wrap(url) + tail

    tracked_text = LINK.sub(swap, text)
    body = _as_html(tracked_text)
    pixel = ""
    if opens:
        src = f"{_base()}{OPEN_PATH}/{_token(workspace_id, message_id, contact_id, '')}"
        pixel = f'<img src="{src}" width="1" height="1" alt="" style="display:none">'
    page = f'<div style="white-space:pre-wrap">{body}</div>{pixel}'
    return Decorated(text=tracked_text, html=page)


def _read(token: str) -> dict[str, str]:
    max_age = settings.CRM_TRACKING_MAX_AGE_DAYS * SECONDS_PER_DAY
    try:
        data = signing.loads(token, salt=SALT, max_age=max_age)
        return {k: str(data[k]) for k in ("w", "m", "c", "u")}
    except (signing.BadSignature, KeyError) as exc:
        raise NotFoundError("not found") from exc


def _record(data: dict[str, str], kind: str, url: str = "") -> None:
    try:
        with transaction.atomic():
            EmailEvent.objects.create(
                workspace_id=uuid.UUID(data["w"]),
                message_id=uuid.UUID(data["m"]),
                contact_id=uuid.UUID(data["c"]),
                kind=kind,
                url=url[:URL_LIMIT],
            )
    except IntegrityError:
        return  # a repeated open


def record_open(token: str) -> None:
    _record(_read(token), EventKind.OPEN)


def record_click(token: str) -> str:
    """Count the click and return where to send the reader. Only http and https go anywhere."""
    data = _read(token)
    target = data["u"]
    if urlsplit(target).scheme not in ("http", "https"):
        raise NotFoundError("not found")
    _record(data, EventKind.CLICK, target)
    return target


def counts_for_messages(workspace_id: uuid.UUID, message_ids: list[uuid.UUID]) -> dict[str, int]:
    """Messages opened and messages clicked (each counted once), and total clicks."""
    rows = EmailEvent.objects.for_workspace(workspace_id).filter(message_id__in=message_ids)
    opened = set(rows.filter(kind=EventKind.OPEN).values_list("message_id", flat=True))
    click_rows = Counter(rows.filter(kind=EventKind.CLICK).values_list("message_id", flat=True))
    return {"opened": len(opened), "clicked": len(click_rows), "clicks": sum(click_rows.values())}


def opened_messages(workspace_id: uuid.UUID, message_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    rows = EmailEvent.objects.for_workspace(workspace_id).filter(
        message_id__in=message_ids, kind=EventKind.OPEN
    )
    return set(rows.values_list("message_id", flat=True))


def clicked_messages(workspace_id: uuid.UUID, message_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    rows = EmailEvent.objects.for_workspace(workspace_id).filter(
        message_id__in=message_ids, kind=EventKind.CLICK
    )
    return set(rows.values_list("message_id", flat=True))
