"""Brands: the companies and pages the studio works for, each with its own strategy and content."""

import uuid

from django.utils import timezone
from django.utils.text import slugify

from core.errors import NotFoundError, ValidationFailedError
from studio.models import Brand
from workspaces.models import Workspace

MAX_NAME = 120
MAX_BRANDS = 30


def _clean(name: str) -> str:
    cleaned = " ".join(name.split())[:MAX_NAME]
    if not cleaned:
        raise ValidationFailedError("give the company a name")
    return cleaned


def _slug(workspace_id: uuid.UUID, name: str) -> str:
    base = slugify(name)[:70] or "company"
    slug, number = base, 2
    while Brand.objects.filter(workspace_id=workspace_id, slug=slug).exists():
        slug, number = f"{base}-{number}", number + 1
    return slug


def create(workspace_id: uuid.UUID, name: str) -> Brand:
    if (
        Brand.objects.filter(workspace_id=workspace_id, archived_at__isnull=True).count()
        >= MAX_BRANDS
    ):
        raise ValidationFailedError("too many companies; archive one first")
    cleaned = _clean(name)
    return Brand.objects.create(
        workspace_id=workspace_id, name=cleaned, slug=_slug(workspace_id, cleaned)
    )


def rename(brand: Brand, name: str) -> Brand:
    brand.name = _clean(name)
    brand.save(update_fields=["name", "updated_at"])
    return brand


def archive(brand: Brand) -> Brand:
    """Hide a company everywhere. Its posts and settings are kept and it can be brought back."""
    brand.archived_at = timezone.now()
    brand.save(update_fields=["archived_at", "updated_at"])
    return brand


def restore(brand: Brand) -> Brand:
    brand.archived_at = None
    brand.save(update_fields=["archived_at", "updated_at"])
    return brand


def listing(workspace_id: uuid.UUID) -> list[Brand]:
    """The workspace's active companies. A workspace that has none gets one named after itself."""
    rows = list(Brand.objects.filter(workspace_id=workspace_id, archived_at__isnull=True))
    if rows or Brand.objects.filter(workspace_id=workspace_id).exists():
        return rows
    name = Workspace.objects.filter(pk=workspace_id).values_list("name", flat=True).first()
    return [create(workspace_id, name or "My company")]


def get(workspace_id: uuid.UUID, brand_id: uuid.UUID) -> Brand:
    found = Brand.objects.filter(pk=brand_id, workspace_id=workspace_id).first()
    if found is None:
        raise NotFoundError("company not found")
    return found


def resolve(workspace_id: uuid.UUID, raw: str) -> Brand:
    """The company a request is about: the one named, or the only one there is."""
    if raw:
        try:
            return get(workspace_id, uuid.UUID(raw))
        except ValueError as exc:
            raise ValidationFailedError("choose a company") from exc
    active = listing(workspace_id)
    if len(active) == 1:
        return active[0]
    raise ValidationFailedError("choose a company")
