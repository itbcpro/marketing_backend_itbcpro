"""Landing page audit for ads: what decides whether a click becomes a lead, found without AI.

Each final URL of a campaign is fetched safely (public addresses only) and scored from facts in the
page: it loads and is secure, matches what the ad promised, shows a clear next step (form, phone,
button), works on a phone and has conversion tracking. The result is a score, itemised issues with
codes the screen turns into sentences, and the facts the AI optimiser reads.
"""

import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from content import net, probe
from core.security import UnsafeUrlError

SLOW_SECONDS = 2.5
HEAVY_BYTES = 2_500_000
THIN_WORDS = 150
MATCH_LOW = 0.3
MATCH_OK = 0.5
SCRIPT = re.compile(r"<(script|style|noscript)[\s\S]*?</\1>", re.I)
TAGS = re.compile(r"<[^>]+>")
H1_TEXT = re.compile(r"<h1[^>]*>([\s\S]*?)</h1>", re.I)
IMG = re.compile(r"<img\b[^>]*>", re.I)
IMG_ALT = re.compile(r"\balt\s*=\s*[\"'][^\"']+[\"']", re.I)
FORM = re.compile(r"<form\b", re.I)
TEL = re.compile(r"href\s*=\s*[\"']tel:", re.I)
MAILTO = re.compile(r"href\s*=\s*[\"']mailto:", re.I)
BUTTON = re.compile(r"<(?:a|button)\b[^>]*>([\s\S]*?)</(?:a|button)>", re.I)
CTA_WORDS = (
    "get a quote",
    "get started",
    "contact",
    "book",
    "call",
    "schedule",
    "request",
    "free",
    "sign up",
    "buy",
    "order",
    "talk to",
    "start",
    "consult",
)
TRACKING = {
    "google_tag": re.compile(r"googletagmanager\.com|gtag\(|\bAW-\d+|\bG-[A-Z0-9]{6,}", re.I),
    "meta_pixel": re.compile(r"fbq\(|connect\.facebook\.net", re.I),
}
TRUST = re.compile(r"testimonial|review|trusted by|years of experience|certified|guarantee", re.I)
STOP = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "your",
        "you",
        "our",
        "are",
        "from",
        "that",
        "this",
        "have",
        "has",
        "near",
        "best",
        "top",
        "all",
        "any",
        "can",
        "get",
        "now",
        "new",
        "more",
        "than",
        "into",
        "about",
        "what",
        "how",
        "why",
        "who",
        "when",
        "where",
    ]
)
WORD = re.compile(r"[a-z0-9']{3,}")
MAX_HTML_CHARS = 400_000
MAX_URLS = 6
MAX_CTA_CHARS = 40
WEIGHTS = {"fail": 20, "warn": 8, "info": 0, "ok": 0}


def _text(html: str) -> str:
    return " ".join(TAGS.sub(" ", SCRIPT.sub(" ", html)).split())


def _terms(texts: list[str]) -> set[str]:
    return {w for t in texts for w in WORD.findall(t.lower()) if w not in STOP}


def _match(ad_texts: list[str], page_head: str) -> float | None:
    wanted = _terms(ad_texts)
    if not wanted:
        return None
    found = _terms([page_head])
    return len(wanted & found) / len(wanted)


def _issue(code: str, level: str, **params: Any) -> dict[str, Any]:
    return {"code": code, "level": level, "params": params}


def _ctas(html: str) -> list[str]:
    found: list[str] = []
    for raw in BUTTON.findall(html):
        label = " ".join(TAGS.sub(" ", raw).split()).lower()
        if label and len(label) <= MAX_CTA_CHARS and any(w in label for w in CTA_WORDS):
            found.append(label)
    return found[:8]


def facts_of(html: str, elapsed: float, size: int) -> dict[str, Any]:
    base = probe.page_facts(html)
    h1 = H1_TEXT.search(html)
    heading = " ".join(TAGS.sub(" ", h1.group(1)).split()) if h1 else ""
    body = _text(html)
    images = IMG.findall(html)
    return {
        **base,
        "h1": heading[:200],
        "words": len(body.split()),
        "forms": len(FORM.findall(html)),
        "phone_link": bool(TEL.search(html)),
        "email_link": bool(MAILTO.search(html)),
        "ctas": _ctas(html),
        "images": len(images),
        "images_without_alt": sum(1 for i in images if not IMG_ALT.search(i)),
        "tracking": [name for name, pattern in TRACKING.items() if pattern.search(html)],
        "trust_signals": bool(TRUST.search(body)),
        "seconds": round(elapsed, 2),
        "bytes": size,
        "excerpt": body[:1200],
    }


def _loading_issues(url: str, facts: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not url.startswith("https://"):
        issues.append(_issue("not_https", "fail"))
    if facts["seconds"] > SLOW_SECONDS:
        issues.append(_issue("slow", "warn", seconds=facts["seconds"]))
    if facts["bytes"] > HEAVY_BYTES:
        issues.append(_issue("heavy", "warn", megabytes=round(facts["bytes"] / 1_000_000, 1)))
    if not facts["viewport"]:
        issues.append(_issue("no_viewport", "fail"))
    if "noindex" in str(facts["robots_meta"]).lower():
        issues.append(_issue("noindex", "info"))
    return issues


def _content_issues(facts: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if facts["h1_count"] == 0:
        issues.append(_issue("no_h1", "warn"))
    elif facts["h1_count"] > 1:
        issues.append(_issue("many_h1", "info", count=facts["h1_count"]))
    if facts["words"] < THIN_WORDS:
        issues.append(_issue("thin_page", "warn", words=facts["words"]))
    if not facts["trust_signals"]:
        issues.append(_issue("no_trust", "info"))
    if facts["images_without_alt"]:
        issues.append(_issue("images_no_alt", "info", count=facts["images_without_alt"]))
    return issues


def _conversion_issues(facts: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not (facts["forms"] or facts["phone_link"] or facts["ctas"]):
        issues.append(_issue("no_next_step", "fail"))
    elif not (facts["forms"] or facts["phone_link"]):
        issues.append(_issue("no_form_or_phone", "warn"))
    if not facts["tracking"]:
        issues.append(_issue("no_tracking", "fail"))
    elif "google_tag" not in facts["tracking"]:
        issues.append(_issue("no_google_tag", "warn"))
    return issues


def _message_issues(facts: dict[str, Any], ad_texts: list[str]) -> list[dict[str, Any]]:
    match = _match(ad_texts, f"{facts['title']} {facts['h1']} {facts['excerpt']}")
    if match is None:
        return []
    percent = round(match * 100)
    if match < MATCH_LOW:
        return [_issue("message_mismatch", "fail", percent=percent)]
    if match < MATCH_OK:
        return [_issue("message_weak", "warn", percent=percent)]
    return [_issue("message_match", "ok", percent=percent)]


def issues_of(url: str, facts: dict[str, Any], ad_texts: list[str]) -> list[dict[str, Any]]:
    return [
        *_loading_issues(url, facts),
        *_content_issues(facts),
        *_conversion_issues(facts),
        *_message_issues(facts, ad_texts),
    ]


def score_of(issues: list[dict[str, Any]]) -> int:
    return max(0, 100 - sum(WEIGHTS[i["level"]] for i in issues))


def audit(url: str, ad_texts: list[str]) -> dict[str, Any]:
    """Fetch one page and judge it. Never raises: an unreachable page is a finding."""
    started = time.monotonic()
    # Outbound fetches are https only: an http ad link is read through its https twin and flagged.
    secure = "https://" + url.removeprefix("http://") if url.startswith("http://") else url
    try:
        response = net.fetch(secure, timeout=15.0, max_bytes=3_000_000)
    except (UnsafeUrlError, httpx.HTTPError) as exc:
        return {
            "url": url,
            "reachable": False,
            "score": 0,
            "issues": [_issue("unreachable", "fail", reason=type(exc).__name__)],
            "facts": {},
        }
    elapsed = time.monotonic() - started
    if response.status_code >= probe.HTTP_ERROR:
        return {
            "url": url,
            "reachable": False,
            "score": 0,
            "issues": [_issue("http_error", "fail", status=response.status_code)],
            "facts": {},
        }
    html = response.text[:MAX_HTML_CHARS]
    facts = facts_of(html, elapsed, len(response.content))
    issues = issues_of(url, facts, ad_texts)
    return {
        "url": url,
        "reachable": True,
        "score": score_of(issues),
        "issues": issues,
        "facts": facts,
    }


def final_urls(ads: list[Any]) -> dict[str, list[str]]:
    """Each distinct final URL of the given ad entities with the headlines that lead to it."""
    found: dict[str, list[str]] = {}
    for ad in ads:
        creative = ad.data.get("creative") or {}
        texts = list(creative.get("headlines") or []) + list(creative.get("descriptions") or [])
        for url in creative.get("final_urls") or [creative.get("target_url") or ""]:
            clean = str(url or "").strip()
            if clean and urlsplit(clean).scheme in {"http", "https"}:
                found.setdefault(clean, []).extend(texts)
    return dict(list(found.items())[:MAX_URLS])


def audit_campaign(ads: list[Any]) -> list[dict[str, Any]]:
    return [audit(url, texts) for url, texts in final_urls(ads).items()]
