"""Feature flags: per-workspace override first, then the global default, cached briefly."""

import uuid
from typing import Any

from django.core.cache import cache

from core.models import FeatureFlag

CACHE_SECONDS = 60


def _key(name: str, workspace_id: uuid.UUID | None) -> str:
    return f"flag:{name}:{workspace_id or 'global'}"


def is_enabled(name: str, workspace_id: uuid.UUID | None = None, *, default: bool = False) -> bool:
    if workspace_id is not None:
        override = _lookup(name, workspace_id)
        if override is not None:
            return override
    global_value = _lookup(name, None)
    return default if global_value is None else global_value


def _lookup(name: str, workspace_id: uuid.UUID | None) -> bool | None:
    key = _key(name, workspace_id)
    cached = cache.get(key)
    if cached is not None:
        return None if cached == "unset" else bool(cached)
    flag = FeatureFlag.objects.filter(name=name, workspace_id=workspace_id).first()  # type: ignore[misc]
    cache.set(key, "unset" if flag is None else flag.enabled, CACHE_SECONDS)
    return None if flag is None else flag.enabled


def set_flag(name: str, enabled: bool, workspace_id: uuid.UUID | None = None) -> FeatureFlag:
    lookup: dict[str, Any] = {"name": name, "workspace_id": workspace_id}
    flag, _ = FeatureFlag.objects.update_or_create(**lookup, defaults={"enabled": enabled})
    cache.delete(_key(name, workspace_id))
    return flag
