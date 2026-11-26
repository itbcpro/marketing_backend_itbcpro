"""Structured logging with secret scrubbing and request correlation."""

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any, cast

import structlog
from django.conf import settings

_SENSITIVE_KEY = re.compile(
    r"(password|passwd|secret|token|authorization|api[_-]?key|cookie|credential|session)", re.I
)
REDACTED = "[redacted]"
_configured = False


def scrub_mapping(value: Any) -> Any:
    """Return a copy of ``value`` with sensitive keys redacted, recursively."""
    if isinstance(value, dict):
        return {
            k: REDACTED if _SENSITIVE_KEY.search(str(k)) else scrub_mapping(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [scrub_mapping(v) for v in value]
    return value


def scrub_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Redact values whose keys look sensitive, recursively."""

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: REDACTED if _SENSITIVE_KEY.search(str(k)) else clean(v) for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return [clean(v) for v in value]
        return value

    return {
        k: REDACTED if _SENSITIVE_KEY.search(str(k)) else clean(v) for k, v in event_dict.items()
    }


def configure_logging() -> None:
    global _configured  # noqa: PLW0603
    if _configured:
        return
    level = getattr(logging, str(getattr(settings, "LOG_LEVEL", "INFO")).upper(), logging.INFO)
    renderer: Any = (
        structlog.dev.ConsoleRenderer()
        if getattr(settings, "DEBUG", False)
        else structlog.processors.JSONRenderer()
    )
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        scrub_secrets,
    ]
    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    return cast("structlog.typing.FilteringBoundLogger", structlog.get_logger(name))
