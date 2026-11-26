"""Website screens: list, add (WordPress or Git-built site), probe and remove."""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema, Status

from audit.actors import Actor
from content import services
from content.brief import Brief
from content.models import Website
from core.errors import NotFoundError, ValidationFailedError
from identity.authentication import api_auth
from identity.models import User
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["websites"], auth=api_auth)


class WebsiteOut(Schema):
    id: uuid.UUID
    name: str
    kind: str
    base_url: str
    connection_id: uuid.UUID | None
    repo: str
    branch: str
    status: str
    status_detail: str
    profile: dict[str, Any]
    probed_at: datetime | None
    brief: dict[str, Any]


class SiteBriefIn(Schema):
    country: str = ""
    region: str = ""
    audience: str = ""
    goal: str = ""


class WebsiteCreateIn(Schema):
    name: str = ""
    kind: str
    base_url: str = ""
    connection_id: uuid.UUID | None = None
    repo: str = ""
    branch: str = ""


def _out(site: Website) -> WebsiteOut:
    return WebsiteOut(
        id=site.pk,
        name=site.name,
        kind=site.kind,
        base_url=site.base_url,
        connection_id=site.connection_id,
        repo=site.repo,
        branch=site.branch,
        status=site.status,
        status_detail=site.status_detail,
        profile=site.profile,
        probed_at=site.probed_at,
        brief=site.brief,
    )


def _get(request: HttpRequest, website_id: uuid.UUID, permission: Permission) -> Website:
    membership = resolve_workspace(request, permission)
    site = Website.objects.filter(pk=website_id, workspace_id=membership.workspace_id).first()
    if site is None:
        raise NotFoundError("website not found")
    return site


@router.get("/", response=list[WebsiteOut], operation_id="websites_list")
def list_websites(request: HttpRequest) -> list[WebsiteOut]:
    membership = resolve_workspace(request, Permission.VIEW)
    return [_out(s) for s in Website.objects.for_workspace(membership.workspace_id)]


@router.post("/", response=WebsiteOut, operation_id="websites_create")
def create_website(request: HttpRequest, payload: WebsiteCreateIn) -> WebsiteOut:
    membership = resolve_workspace(request, Permission.MANAGE_CONNECTIONS)
    user: User = request.auth  # type: ignore[attr-defined]
    site = services.create_website(
        workspace_id=membership.workspace_id,
        name=payload.name,
        kind=payload.kind,
        base_url=payload.base_url,
        connection_id=payload.connection_id,
        repo=payload.repo,
        branch=payload.branch,
        actor=Actor.human(user.pk),
    )
    return _out(site)


@router.put("/{website_id}/brief", response=WebsiteOut, operation_id="websites_brief")
def save_brief(request: HttpRequest, website_id: uuid.UUID, payload: SiteBriefIn) -> WebsiteOut:
    """Defaults for every AI review of this site (country, region, audience, goal)."""
    site = _get(request, website_id, Permission.EDIT_CONTENT)
    try:
        site.brief = Brief(**payload.dict()).cleaned()
    except ValueError as exc:
        raise ValidationFailedError(str(exc)[:300]) from exc
    site.save(update_fields=["brief", "updated_at"])
    return _out(site)


@router.get("/{website_id}", response=WebsiteOut, operation_id="websites_get")
def get_website(request: HttpRequest, website_id: uuid.UUID) -> WebsiteOut:
    return _out(_get(request, website_id, Permission.VIEW))


@router.post("/{website_id}/probe", response=WebsiteOut, operation_id="websites_probe")
def probe_website(request: HttpRequest, website_id: uuid.UUID) -> WebsiteOut:
    site = _get(request, website_id, Permission.MANAGE_CONNECTIONS)
    return _out(services.run_probe(site))


@router.delete("/{website_id}", response={204: None}, operation_id="websites_delete")
def delete_website(request: HttpRequest, website_id: uuid.UUID) -> Status[None]:
    site = _get(request, website_id, Permission.MANAGE_CONNECTIONS)
    user: User = request.auth  # type: ignore[attr-defined]
    services.delete_website(site, actor=Actor.human(user.pk))
    return Status(204, None)
