"""Content studio API: the companies (brands) the studio works for."""

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema

from identity.authentication import api_auth
from studio import brands
from studio.models import Brand
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["studio-brands"], auth=api_auth)


class StudioBrandOut(Schema):
    id: uuid.UUID
    name: str
    slug: str
    archived_at: datetime | None


def brand_out(brand: Brand) -> StudioBrandOut:
    return StudioBrandOut(
        id=brand.pk, name=brand.name, slug=brand.slug, archived_at=brand.archived_at
    )


@router.get("/brands", response=list[StudioBrandOut], operation_id="studio_brands")
def list_brands(request: HttpRequest) -> list[StudioBrandOut]:
    """The companies of this workspace. A workspace with none gets one named after itself."""
    membership = resolve_workspace(request, Permission.VIEW)
    return [brand_out(b) for b in brands.listing(membership.workspace_id)]


class StudioBrandIn(Schema):
    name: str


@router.post("/brands", response=StudioBrandOut, operation_id="studio_brand_create")
def create_brand(request: HttpRequest, body: StudioBrandIn) -> StudioBrandOut:
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    return brand_out(brands.create(membership.workspace_id, body.name))


class StudioBrandUpdateIn(Schema):
    name: str | None = None
    archived: bool | None = None


@router.patch("/brands/{brand_id}", response=StudioBrandOut, operation_id="studio_brand_update")
def update_brand(
    request: HttpRequest, brand_id: uuid.UUID, body: StudioBrandUpdateIn
) -> StudioBrandOut:
    """Rename a company, or hide it (its posts and settings are kept and it can come back)."""
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    brand = brands.get(membership.workspace_id, brand_id)
    if body.name is not None:
        brands.rename(brand, body.name)
    if body.archived is True:
        brands.archive(brand)
    elif body.archived is False:
        brands.restore(brand)
    return brand_out(brand)
