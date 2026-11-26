"""Site probes: collect the facts the platform needs before it can audit or change a site.

A probe never writes to the site. Each returns a plain dict stored as ``Website.profile``.
"""

import re
from typing import Any
from urllib.parse import urljoin

import httpx

from content import net, seo_meta
from content.wordpress.client import WordPressClient, detect_plugins
from core.errors import AuthExpiredError, DomainError
from core.security import UnsafeUrlError

HTTP_OK = 200
HTTP_ERROR = 400
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
META = re.compile(r"<meta\s+[^>]*>", re.I)
ATTR = re.compile(r'([a-zA-Z:-]+)\s*=\s*["\']([^"\']*)["\']')
LINK_CANONICAL = re.compile(r"<link\s+[^>]*rel=[\"']canonical[\"'][^>]*>", re.I)
LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
H1 = re.compile(r"<h1[\s>]", re.I)
SITEMAP_LINE = re.compile(r"^\s*sitemap:\s*(\S+)", re.I | re.M)
LANG = re.compile(r"<html[^>]*\blang=[\"']([^\"']+)[\"']", re.I)


def _meta(html: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for tag in META.findall(html):
        attrs = {k.lower(): v for k, v in ATTR.findall(tag)}
        key = attrs.get("name") or attrs.get("property")
        if key and "content" in attrs:
            found.setdefault(key.lower(), attrs["content"])
    return found


def _fetch(url: str) -> httpx.Response | None:
    try:
        return net.fetch(url)
    except UnsafeUrlError, httpx.HTTPError:
        return None


def _seo_meta_state(client: WordPressClient, plugin: str) -> dict[str, Any]:
    """Whether the SEO plugin's title and description fields can be written over REST."""
    keys = seo_meta.keys_for(plugin)
    if not keys:
        return {"plugin": plugin, "writable": False}
    try:
        exposed = client.exposed_meta_keys("posts")
    except DomainError:
        return {"plugin": plugin, "writable": False}
    return {"plugin": plugin, "writable": all(key in exposed for key in keys.values())}


def probe_wordpress(client: WordPressClient) -> dict[str, Any]:
    """Platform facts through the authenticated REST API (read-only)."""
    warnings: list[str] = []
    profile: dict[str, Any] = {"kind": "wordpress", "https": True}
    root = client.root()
    namespaces = [str(n) for n in root.get("namespaces", [])]
    profile.update(
        name=root.get("name", ""),
        description=root.get("description", ""),
        home=root.get("home", ""),
        timezone=root.get("timezone_string", ""),
        gmt_offset=root.get("gmt_offset", 0),
        namespaces=namespaces,
        detected=detect_plugins(namespaces),
    )
    try:
        me = client.me()
        profile["user"] = {
            "id": me.get("id"),
            "name": me.get("name", ""),
            "roles": me.get("roles", []),
            "can_publish": bool((me.get("capabilities") or {}).get("publish_posts")),
        }
    except AuthExpiredError:
        warnings.append("credentials_rejected")
    try:
        site = client.settings()
        profile["settings"] = {
            "title": site.get("title", ""),
            "language": site.get("language", ""),
            "timezone": site.get("timezone", ""),
            "default_comment_status": site.get("default_comment_status", ""),
        }
    except DomainError:
        warnings.append("settings_unreadable")
    try:
        plugins = client.plugins()
        profile["plugins"] = [
            {"name": p.get("name", ""), "version": p.get("version", ""), "status": p.get("status")}
            for p in plugins
        ]
    except DomainError:
        warnings.append("plugins_unreadable")
    profile["counts"] = client.counts()
    profile["seo_meta"] = _seo_meta_state(client, profile["detected"].get("seo", ""))
    if not profile["detected"].get("seo"):
        warnings.append("no_seo_plugin_detected")
    profile["warnings"] = warnings
    return profile


def page_facts(html: str) -> dict[str, Any]:
    meta = _meta(html)
    canonical = LINK_CANONICAL.search(html)
    attrs = {k.lower(): v for k, v in ATTR.findall(canonical.group(0))} if canonical else {}
    title = TITLE.search(html)
    lang = LANG.search(html)
    return {
        "title": title.group(1).strip() if title else "",
        "description": meta.get("description", ""),
        "generator": meta.get("generator", ""),
        "lang": lang.group(1) if lang else "",
        "viewport": bool(meta.get("viewport")),
        "canonical": attrs.get("href", ""),
        "robots_meta": meta.get("robots", ""),
        "og_title": bool(meta.get("og:title")),
        "h1_count": len(H1.findall(html)),
    }


def _crawl_files(base_url: str) -> dict[str, Any]:
    """robots.txt and the first sitemap it names (or ``/sitemap.xml``)."""
    robots = _fetch(urljoin(base_url, "/robots.txt"))
    robots_text = robots.text if robots is not None and robots.status_code == HTTP_OK else ""
    named = SITEMAP_LINE.findall(robots_text)
    sitemap_url = named[0] if named else urljoin(base_url, "/sitemap.xml")
    sitemap = _fetch(sitemap_url)
    body = sitemap.text if sitemap is not None and sitemap.status_code == HTTP_OK else ""
    return {
        "robots_txt": bool(robots_text),
        "sitemap_url": sitemap_url,
        "sitemap_entries": len(LOC.findall(body)),
        "sitemap_is_index": "<sitemapindex" in body,
    }


def _public_warnings(profile: dict[str, Any]) -> list[str]:
    checks = (
        (profile["status_code"] >= HTTP_ERROR, "home_returned_error"),
        (not profile["title"], "missing_title"),
        (not profile["description"], "missing_meta_description"),
        (profile["h1_count"] != 1, "h1_count_not_one"),
        (not profile["robots_txt"], "no_robots_txt"),
        (profile["sitemap_entries"] == 0, "no_sitemap"),
        ("noindex" in profile["robots_meta"].lower(), "home_is_noindex"),
        (not profile["viewport"], "missing_viewport"),
    )
    return [name for failed, name in checks if failed]


def probe_public_site(base_url: str) -> dict[str, Any]:
    """What a visitor and a crawler see: home page, robots.txt and the sitemap."""
    home = _fetch(base_url)
    profile: dict[str, Any] = {"kind": "public", "https": base_url.startswith("https://")}
    if home is None:
        profile.update(reachable=False, warnings=["site_unreachable"])
        return profile
    profile.update(reachable=True, status_code=home.status_code)
    profile.update(page_facts(home.text))
    profile.update(_crawl_files(base_url))
    profile["warnings"] = _public_warnings(profile)
    return profile
