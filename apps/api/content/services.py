"""Website use cases: register a site, probe it, keep its status current."""

import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from connections.models import Connection
from content import probe
from content.models import Website, WebsiteKind, WebsiteStatus
from content.wordpress import factory
from content.wordpress.client import PROVIDER as WORDPRESS
from content.wordpress.client import normalize_site_url
from core.errors import ConflictError, DomainError, NotFoundError, ValidationFailedError

REPO_PATTERN = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"


def _origin(value: str) -> str:
    try:
        return normalize_site_url(value)
    except DomainError as exc:
        raise ValidationFailedError(str(exc)) from exc


def create_website(
    *,
    workspace_id: uuid.UUID,
    name: str,
    kind: str,
    actor: Actor,
    base_url: str = "",
    connection_id: uuid.UUID | None = None,
    repo: str = "",
    branch: str = "",
) -> Website:
    connection: Connection | None = None
    if kind == WebsiteKind.WORDPRESS:
        if connection_id is None:
            raise NotFoundError("WordPress connection not found")
        connection = Connection.objects.filter(
            pk=connection_id, workspace_id=workspace_id, provider=WORDPRESS
        ).first()
        if connection is None:
            raise NotFoundError("WordPress connection not found")
        base_url = str(connection.config.get("form", {}).get("site_url") or "") or (
            connection.external_account_id
        )
    elif kind == WebsiteKind.GIT_SITE:
        if repo and not _valid_repo(repo):
            raise ValidationFailedError("repository must look like owner/name")
    else:
        raise ValidationFailedError("unknown website kind")
    base_url = _origin(base_url)
    try:
        with transaction.atomic():
            website = Website.objects.create(
                workspace_id=workspace_id,
                name=name.strip() or urlsplit(base_url).hostname or base_url,
                kind=kind,
                base_url=base_url,
                connection=connection,
                repo=repo.strip(),
                branch=branch.strip(),
            )
            record_event(
                action="website.created",
                actor=actor,
                workspace_id=workspace_id,
                target_type="website",
                target_id=website.pk,
                after={"kind": kind, "base_url": base_url},
            )
    except IntegrityError as exc:
        raise ConflictError("this site is already added") from exc
    run_probe(website)
    return website


def _valid_repo(value: str) -> bool:
    return re.fullmatch(REPO_PATTERN, value.strip()) is not None


def run_probe(website: Website) -> Website:
    """Refresh the site profile and status. Never raises for an unreachable site."""
    previous = website.status
    try:
        if website.kind == WebsiteKind.WORDPRESS and website.connection is not None:
            profile = probe.probe_wordpress(factory.for_connection(website.connection))
        else:
            profile = probe.probe_public_site(website.base_url)
    except DomainError as exc:
        profile = {"warnings": ["probe_failed"], "error": exc.code.value}
        status, detail = WebsiteStatus.UNREACHABLE, str(exc)[:300]
    else:
        status, detail = _judge(profile)
    website.profile = profile
    website.status = status
    website.status_detail = detail
    website.probed_at = timezone.now()
    website.save(update_fields=["profile", "status", "status_detail", "probed_at", "updated_at"])
    if previous != status:
        record_event(
            action="website.status_changed",
            actor=Actor.system("content"),
            workspace_id=website.workspace_id,
            target_type="website",
            target_id=website.pk,
            before={"status": previous},
            after={"status": str(status), "detail": detail},
        )
    return website


BLOCKING = {"site_unreachable", "home_returned_error", "credentials_rejected", "home_is_noindex"}


def _judge(profile: dict[str, Any]) -> tuple[WebsiteStatus, str]:
    warnings = [str(w) for w in profile.get("warnings", [])]
    if profile.get("reachable") is False or "site_unreachable" in warnings:
        return WebsiteStatus.UNREACHABLE, "the site did not answer"
    blocking = [w for w in warnings if w in BLOCKING]
    if blocking:
        return WebsiteStatus.DEGRADED, ", ".join(blocking)
    return WebsiteStatus.HEALTHY, ""


def delete_website(website: Website, *, actor: Actor) -> None:
    record_event(
        action="website.deleted",
        actor=actor,
        workspace_id=website.workspace_id,
        target_type="website",
        target_id=website.pk,
        before={"base_url": website.base_url},
    )
    website.delete()
