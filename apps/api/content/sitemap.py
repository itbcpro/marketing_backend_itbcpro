"""Read a site's XML sitemaps (plain or index) into a flat list of URLs."""

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from content import net
from content.probe import SITEMAP_LINE

URL_BLOCK = re.compile(r"<url>(.*?)</url>", re.I | re.S)
SITEMAP_BLOCK = re.compile(r"<sitemap>(.*?)</sitemap>", re.I | re.S)
LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
LASTMOD = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.I)
FALLBACKS = ("/sitemap_index.xml", "/sitemap.xml", "/wp-sitemap.xml")
MAX_SITEMAPS = 60
MAX_URLS = 5000
HTTP_OK = 200


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    url: str
    lastmod: str = ""


def canon(url: str) -> str:
    """Comparable form of a page address: lower-case host, no fragment, no trailing slash."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.rstrip("/") or ""
    query = f"?{parts.query}" if parts.query else ""
    return f"{host}{path}{query}"


def _get(url: str) -> str:
    try:
        response = net.fetch(url, max_bytes=8_000_000)
    except Exception:
        return ""
    return response.text if response.status_code == HTTP_OK else ""


def _start_urls(base_url: str) -> list[str]:
    robots = _get(urljoin(base_url, "/robots.txt"))
    named = SITEMAP_LINE.findall(robots)
    return named or [urljoin(base_url, path) for path in FALLBACKS]


def collect(base_url: str) -> list[SitemapEntry]:
    """Every URL of the site's sitemaps (follows sitemap indexes), same host only."""
    host = canon(base_url).split("/")[0]
    queue = _start_urls(base_url)
    seen_maps: set[str] = set()
    entries: dict[str, SitemapEntry] = {}
    while queue and len(seen_maps) < MAX_SITEMAPS and len(entries) < MAX_URLS:
        current = queue.pop(0)
        if current in seen_maps:
            continue
        seen_maps.add(current)
        body = _get(current)
        if not body:
            continue
        children = [m for block in SITEMAP_BLOCK.findall(body) for m in LOC.findall(block)]
        queue.extend(c for c in children if c not in seen_maps)
        for block in URL_BLOCK.findall(body):
            loc = LOC.search(block)
            if loc is None or canon(loc.group(1)).split("/")[0] != host:
                continue
            modified = LASTMOD.search(block)
            entries.setdefault(
                canon(loc.group(1)),
                SitemapEntry(loc.group(1), modified.group(1) if modified else ""),
            )
    return list(entries.values())[:MAX_URLS]
