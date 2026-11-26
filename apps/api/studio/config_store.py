"""Load and save the studio settings of a brand."""

import uuid

from pydantic import ValidationError

from core.errors import ValidationFailedError
from identity.models import User
from studio.channels.registry import get_channel, is_known
from studio.config import StudioConfig
from studio.models import Brand, StudioProfile

AUTONOMOUS_MODES = {"auto"}


def load(brand_id: uuid.UUID) -> StudioConfig:
    row = StudioProfile.objects.filter(brand_id=brand_id).first()
    try:
        return StudioConfig.model_validate(row.config if row else {})
    except ValidationError:
        return StudioConfig()


def profile(brand_id: uuid.UUID) -> StudioProfile | None:
    return StudioProfile.objects.filter(brand_id=brand_id).first()


def validate_channels(config: StudioConfig) -> None:
    """Every configured channel must exist and every chosen post type must be one it supports."""
    for key, plan in config.channels.items():
        if not is_known(key):
            raise ValidationFailedError(f"unknown channel: {key}")
        unknown = set(plan.kinds) - set(get_channel(key).kind_names)
        if unknown:
            raise ValidationFailedError(f"{key} does not support: {', '.join(sorted(unknown))}")


def grants_more_autonomy(new: StudioConfig, old: StudioConfig) -> bool:
    """Whether saving turns on something that publishes without a person."""
    if new.automation.enabled and not old.automation.enabled:
        return any(p.publish_mode in AUTONOMOUS_MODES and p.enabled for p in new.channels.values())
    return any(
        p.enabled
        and p.publish_mode in AUTONOMOUS_MODES
        and old.plan_for(key).publish_mode not in AUTONOMOUS_MODES
        for key, p in new.channels.items()
    )


def save(brand: Brand, config: StudioConfig, user: User) -> StudioConfig:
    validate_channels(config)
    row, _ = StudioProfile.objects.get_or_create(
        brand_id=brand.pk, defaults={"workspace_id": brand.workspace_id}
    )
    row.config = config.model_dump(mode="json")
    row.updated_by = user
    runs_alone = config.automation.enabled and any(
        p.enabled and p.publish_mode in AUTONOMOUS_MODES for p in config.channels.values()
    )
    row.automation_user = user if runs_alone else None
    row.save()
    return config
