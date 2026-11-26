"""Merge fields: ``{{contact.first_name}}`` and ``{{workspace_values.booking_link}}``.

Only the namespaces passed in resolve; nothing is evaluated. Values are HTML-escaped when asked.
"""

import re
from collections.abc import Mapping
from html import escape
from typing import Any

from django.conf import settings

PATTERN = re.compile(r"\{\{\s*([a-z_]+)\.([a-z0-9_]+)\s*\}\}")


def render(
    template: str,
    context: Mapping[str, Mapping[str, Any]],
    *,
    html: bool = False,
    fallback: str | None = None,
) -> str:
    """Replace every known merge field; unknown or empty ones use the fallback text."""
    missing = settings.CRM_TEMPLATE_FALLBACK if fallback is None else fallback

    def substitute(match: re.Match[str]) -> str:
        value = context.get(match.group(1), {}).get(match.group(2))
        text = missing if value in (None, "", []) else str(value)
        return escape(text) if html else text

    return PATTERN.sub(substitute, template)


def referenced_keys(template: str) -> set[tuple[str, str]]:
    return {(m.group(1), m.group(2)) for m in PATTERN.finditer(template)}
