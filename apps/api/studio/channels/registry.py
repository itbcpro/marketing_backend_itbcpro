"""Registry of channels. Populated at startup; code never switches on a channel name."""

from core.errors import ValidationFailedError
from studio.channels.spec import ChannelSpec, KindSpec

_CHANNELS: dict[str, ChannelSpec] = {}


def register_channel(spec: ChannelSpec) -> None:
    _CHANNELS[spec.key] = spec


def get_channel(key: str) -> ChannelSpec:
    spec = _CHANNELS.get(key)
    if spec is None:
        raise ValidationFailedError(f"unknown channel: {key}")
    return spec


def get_kind(channel: str, kind: str) -> KindSpec:
    found = get_channel(channel).kind(kind)
    if found is None:
        raise ValidationFailedError(f"{channel} has no {kind} post type")
    return found


def all_channels() -> list[ChannelSpec]:
    return list(_CHANNELS.values())


def is_known(key: str) -> bool:
    return key in _CHANNELS
