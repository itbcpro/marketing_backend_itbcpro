"""API drift watch: pinned versions nearing sunset and vendor documentation changes."""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx
from django.conf import settings
from django.utils import timezone

from audit import events
from audit.actors import Actor
from audit.services import record_event
from connections.models import DocSnapshot
from core.logging import get_logger
from core.security import UnsafeUrlError, safe_fetch

log = get_logger(__name__)

WARN_DAYS = 90
CRITICAL_DAYS = 30
Fetcher = Callable[[str], httpx.Response]


@dataclass(frozen=True, slots=True)
class SunsetFinding:
    provider: str
    version: str
    sunset: date
    days_left: int
    level: str


def _level(days_left: int) -> str:
    if days_left < 0:
        return "expired"
    return "critical" if days_left <= CRITICAL_DAYS else "warning"


def check_version_sunsets(
    versions: dict[str, str], sunsets: dict[str, str], *, today: date | None = None
) -> list[SunsetFinding]:
    """Compare pinned versions with known sunset dates (``"provider:version" -> ISO date``)."""
    today = today or datetime.now(UTC).date()
    findings: list[SunsetFinding] = []
    for provider, version in sorted(versions.items()):
        raw = sunsets.get(f"{provider}:{version}")
        if not raw:
            continue
        sunset = date.fromisoformat(raw)
        days_left = (sunset - today).days
        if days_left <= WARN_DAYS:
            level = (
                "expired"
                if days_left < 0
                else "critical"
                if days_left <= CRITICAL_DAYS
                else "warning"
            )
            findings.append(SunsetFinding(provider, version, sunset, days_left, level))
    return findings


_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def normalise_document(html: str) -> str:
    """Strip markup so cosmetic page changes do not trigger alerts."""
    return _SPACE.sub(" ", _TAGS.sub(" ", _SCRIPT_OR_STYLE.sub(" ", html))).strip()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def watch_docs(urls: dict[str, str], *, fetch: Fetcher | None = None) -> list[str]:
    """Fetch each documentation URL; returns the names whose content changed since last time."""
    fetch = fetch or (lambda url: safe_fetch(url, timeout=15.0))
    changed: list[str] = []
    for name, url in sorted(urls.items()):
        try:
            response = fetch(url)
            response.raise_for_status()
        except (UnsafeUrlError, httpx.HTTPError) as exc:
            log.warning("doc_watch_fetch_failed", name=name, error=type(exc).__name__)
            continue
        digest = _hash(normalise_document(response.text))
        snapshot = DocSnapshot.objects.filter(name=name).first()
        if snapshot is None:
            DocSnapshot.objects.create(name=name, url=url, content_hash=digest)
            continue
        if snapshot.content_hash != digest:
            snapshot.previous_hash = snapshot.content_hash
            snapshot.content_hash = digest
            snapshot.changed_at = timezone.now()
            snapshot.url = url
            snapshot.save()
            changed.append(name)
            record_event(
                action="platform.docs_changed",
                actor=Actor.system("drift-watch"),
                target_type="doc",
                target_id=name,
                after={"url": url},
            )
            events.publish(
                events.EventTopic.PLATFORM_DRIFT, {"kind": "docs", "name": name, "url": url}
            )
        else:
            snapshot.save(update_fields=["updated_at"])
    return changed


def run_drift_watch(
    *, today: date | None = None, fetch: Fetcher | None = None
) -> dict[str, object]:
    sunsets = check_version_sunsets(
        settings.PROVIDER_API_VERSIONS, settings.PROVIDER_SUNSET_DATES, today=today
    )
    for finding in sunsets:
        events.publish(
            events.EventTopic.PLATFORM_DRIFT,
            {
                "kind": "sunset",
                "provider": finding.provider,
                "version": finding.version,
                "days_left": finding.days_left,
                "level": finding.level,
            },
        )
    docs = watch_docs(settings.PROVIDER_DOC_URLS, fetch=fetch)
    return {
        "sunsets": [f.level for f in sunsets],
        "docs_changed": docs,
        "at": datetime.now(UTC).isoformat(),
    }
