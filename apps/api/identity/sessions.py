"""Tracking and revoking browser sessions."""

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.contrib.auth.signals import user_logged_in
from django.contrib.sessions.models import Session
from django.dispatch import receiver
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from core.errors import NotFoundError
from identity.models import SessionRecord, User

TOUCH_INTERVAL = timedelta(minutes=5)


def _client_ip(request: HttpRequest) -> str | None:
    return request.META.get("REMOTE_ADDR") or None


@receiver(user_logged_in)
def _record_session(sender: Any, request: HttpRequest | None, user: User, **_: Any) -> None:
    if request is None or not request.session.session_key:
        return
    SessionRecord.objects.update_or_create(
        session_key=request.session.session_key,
        defaults={
            "user": user,
            "user_agent": request.headers.get("User-Agent", "")[:300],
            "ip_address": _client_ip(request),
            "last_seen_at": timezone.now(),
        },
    )


class SessionActivityMiddleware:
    """Refresh ``last_seen_at`` at most every few minutes for signed-in users."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        key = request.session.session_key if hasattr(request, "session") else None
        if key and request.user.is_authenticated:
            SessionRecord.objects.filter(
                session_key=key, last_seen_at__lt=timezone.now() - TOUCH_INTERVAL
            ).update(last_seen_at=timezone.now())
        return response


def list_sessions(user: User) -> list[SessionRecord]:
    live = set(
        Session.objects.filter(expire_date__gt=timezone.now()).values_list("session_key", flat=True)
    )
    records = SessionRecord.objects.filter(user=user).order_by("-last_seen_at")
    stale = [r.pk for r in records if r.session_key not in live]
    if stale:
        SessionRecord.objects.filter(pk__in=stale).delete()
    return [r for r in records if r.session_key in live]


def revoke_session(user: User, record_id: uuid.UUID) -> None:
    record = SessionRecord.objects.filter(pk=record_id, user=user).first()
    if record is None:
        raise NotFoundError("session not found")
    Session.objects.filter(session_key=record.session_key).delete()
    record.delete()


def revoke_all_sessions(user: User, *, keep: str | None = None) -> int:
    records = SessionRecord.objects.filter(user=user)
    if keep:
        records = records.exclude(session_key=keep)
    keys = list(records.values_list("session_key", flat=True))
    Session.objects.filter(session_key__in=keys).delete()
    records.delete()
    return len(keys)
