"""The "fix these first" list on the Website screen, and setting items aside and back."""

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Query, Router, Schema

from content import attention
from content.models import ContentItem, IgnoredCheck, Website
from core.errors import NotFoundError
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["websites"], auth=api_auth)
MAX_ROWS = 200


class AttProblem(Schema):
    key: str
    status: str
    weight: int


class AttRow(Schema):
    id: uuid.UUID
    title: str
    url: str
    kind: str
    score: int
    problems: list[AttProblem]


class AttIgnored(Schema):
    id: uuid.UUID
    item_key: str
    check_key: str
    title: str
    url: str
    item_id: uuid.UUID | None
    ignored_at: datetime


class AttOut(Schema):
    threshold: int
    total: int
    items: list[AttRow]
    ignored: list[AttIgnored]


class AttIn(Schema):
    item_id: uuid.UUID
    check_key: str = ""


class AttQuery(Schema):
    threshold: int = attention.DEFAULT_THRESHOLD


def _site(request: HttpRequest, website_id: uuid.UUID, permission: Permission) -> Website:
    membership = resolve_workspace(request, permission)
    site = Website.objects.filter(pk=website_id, workspace_id=membership.workspace_id).first()
    if site is None:
        raise NotFoundError("website not found")
    return site


def _ignored(site: Website) -> list[AttIgnored]:
    rows = list(IgnoredCheck.objects.filter(website=site))
    items = {
        i.key: i
        for i in ContentItem.objects.filter(website=site, key__in=[r.item_key for r in rows])
    }
    return [
        AttIgnored(
            id=r.pk,
            item_key=r.item_key,
            check_key=r.check_key,
            title=(items[r.item_key].title or items[r.item_key].url)
            if r.item_key in items
            else r.item_key,
            url=items[r.item_key].url if r.item_key in items else "",
            item_id=items[r.item_key].pk if r.item_key in items else None,
            ignored_at=r.created_at,
        )
        for r in rows
    ]


@router.get("/{website_id}/attention", response=AttOut, operation_id="websites_attention")
def get_attention(request: HttpRequest, website_id: uuid.UUID, filters: Query[AttQuery]) -> AttOut:
    site = _site(request, website_id, Permission.VIEW)
    threshold = max(1, min(filters.threshold, 101))
    rows = attention.worst(site, threshold)
    return AttOut(
        threshold=threshold,
        total=len(rows),
        items=[
            AttRow(
                id=r.item.pk,
                title=r.item.title,
                url=r.item.url,
                kind=r.item.kind,
                score=r.score,
                problems=[AttProblem(**p) for p in r.problems],
            )
            for r in rows[:MAX_ROWS]
        ],
        ignored=_ignored(site),
    )


@router.post("/{website_id}/ignores", response=AttIgnored, operation_id="websites_ignore")
def ignore(request: HttpRequest, website_id: uuid.UUID, payload: AttIn) -> AttIgnored:
    site = _site(request, website_id, Permission.EDIT_CONTENT)
    item = ContentItem.objects.filter(website=site, pk=payload.item_id).first()
    if item is None:
        raise NotFoundError("page not found")
    row, _ = IgnoredCheck.objects.get_or_create(
        website=site,
        item_key=item.key,
        check_key=payload.check_key,
        defaults={"workspace_id": site.workspace_id},
    )
    return next(i for i in _ignored(site) if i.id == row.pk)


@router.delete(
    "/{website_id}/ignores/{ignore_id}", response={204: None}, operation_id="websites_unignore"
)
def unignore(request: HttpRequest, website_id: uuid.UUID, ignore_id: uuid.UUID) -> tuple[int, None]:
    site = _site(request, website_id, Permission.EDIT_CONTENT)
    IgnoredCheck.objects.filter(website=site, pk=ignore_id).delete()
    return 204, None
