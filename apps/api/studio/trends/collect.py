"""Collect trend signals from every enabled source of a brand, tolerant of failing sources."""

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit

from django.utils import timezone

from core.errors import DomainError
from core.logging import get_logger
from studio import config_store
from studio.models import TrendItem, TrendSource
from studio.trends.ports import RawTrend, get_trend_source, validate_params

log = get_logger(__name__)

KEEP_FACTOR = 3
MAX_PER_SOURCE = 50
TRACKING = re.compile(r"^(utm_|fbclid|gclid|ref$|ref_)", re.IGNORECASE)
NON_WORD = re.compile(r"\W+")


@dataclass(slots=True)
class CollectResult:
    sources: int = 0
    failed: int = 0
    new_items: int = 0
    errors: dict[str, str] = field(default_factory=dict)


def _clean_url(url: str) -> str:
    parts = urlsplit(url.strip())
    kept = [(k, v) for k, v in parse_qsl(parts.query) if not TRACKING.match(k)]
    return parts._replace(query=urlencode(kept), fragment="").geturl()


def fingerprint(kind: str, item: RawTrend) -> str:
    title = NON_WORD.sub(" ", item.title.lower()).strip()
    basis = f"{title}|{_clean_url(item.url)}" if item.url else f"{kind}|{title}"
    return hashlib.sha1(basis.encode()).hexdigest()  # noqa: S324 - dedupe key, not security


def _avoided(item: RawTrend, avoid: list[str]) -> bool:
    text = f"{item.title} {item.summary}".lower()
    return any(re.search(rf"(?<!\w){re.escape(a.lower())}(?!\w)", text) for a in avoid if a)


def _store(source: TrendSource, items: list[RawTrend], avoid: list[str]) -> int:
    created = 0
    for item in items[:MAX_PER_SOURCE]:
        if not item.title.strip() or _avoided(item, avoid):
            continue
        _, was_created = TrendItem.objects.update_or_create(
            brand_id=source.brand_id,
            fingerprint=fingerprint(source.kind, item),
            defaults={
                "workspace_id": source.workspace_id,
                "source": source,
                "kind": source.kind,
                "title": item.title,
                "summary": item.summary,
                "url": item.url,
                "score": item.score,
                "published_at": item.published_at,
            },
        )
        created += int(was_created)
    return created


def collect_source(source: TrendSource, avoid: list[str]) -> int:
    """Fetch one source and store what it returned; records the outcome on the source."""
    kind = get_trend_source(source.kind)
    params = validate_params(source.kind, source.params)
    items = kind.fetch(source.workspace_id, params)
    created = _store(source, items, avoid)
    source.last_fetched_at = timezone.now()
    source.last_count = len(items)
    source.last_error = ""
    source.save(update_fields=["last_fetched_at", "last_count", "last_error", "updated_at"])
    return created


def collect_brand(brand_id: uuid.UUID) -> CollectResult:
    config = config_store.load(brand_id)
    result = CollectResult()
    for source in TrendSource.objects.filter(brand_id=brand_id, enabled=True):
        result.sources += 1
        try:
            result.new_items += collect_source(source, config.trends.avoid_topics)
        except DomainError as exc:
            _record_failure(source, exc.code.value, result)
        except Exception:
            log.exception("studio_source_crashed", source=str(source.pk))
            _record_failure(source, "unknown", result)
    prune(brand_id, config.trends.max_age_days)
    return result


def _record_failure(source: TrendSource, code: str, result: CollectResult) -> None:
    source.last_error = code
    source.last_fetched_at = timezone.now()
    source.save(update_fields=["last_error", "last_fetched_at", "updated_at"])
    result.failed += 1
    result.errors[str(source.pk)] = code


def prune(brand_id: uuid.UUID, max_age_days: int) -> int:
    """Drop signals that are too old to matter (kept for a few times the useful age)."""
    cutoff = timezone.now() - timedelta(days=max_age_days * KEEP_FACTOR)
    deleted, _ = TrendItem.objects.filter(brand_id=brand_id, created_at__lt=cutoff).delete()
    return deleted


def recent_items(brand_id: uuid.UUID, max_age_days: int, limit: int) -> list[TrendItem]:
    """The strongest recent, not-ignored signals, newest first among equals."""
    since = timezone.now() - timedelta(days=max_age_days)
    rows = TrendItem.objects.filter(
        brand_id=brand_id, ignored=False, created_at__gte=since
    ).order_by("-score", "-created_at")[:limit]
    return list(rows)
