"""Hard rules for text the AI wrote, applied in code after the model answered.

The prompt asks for these rules too, but a prompt is a request; this module is the guarantee. Text
that breaks a rule that cannot be fixed by trimming is flagged, and flagged text is never sent
automatically.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

URL = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{8,}\d)(?!\d)")
HASHTAG = re.compile(r"(?<!\w)#\w+")
SENTENCE_END = re.compile(r"[.!?]\s")
EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿\U0001f000-\U0001f2ff️]", re.UNICODE)
MAX_EMOJI_LIGHT = 2


@dataclass(slots=True)
class Cleaned:
    text: str
    violations: list[str] = field(default_factory=list)


def _domain(url: str) -> str:
    target = url if "//" in url else f"https://{url}"
    return (urlsplit(target).hostname or "").lower().removeprefix("www.")


def _allowed(url: str, allowed: list[str]) -> bool:
    host = _domain(url)
    return any(
        host == d.lower().removeprefix("www.") or host.endswith(f".{d.lower()}") for d in allowed
    )


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = list(SENTENCE_END.finditer(cut))
    if ends and ends[-1].end() > limit * 0.5:
        return cut[: ends[-1].end()].strip()
    return cut.rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"


def _blocked(text: str, words: list[str]) -> list[str]:
    lowered = text.lower()
    return [w for w in words if w and re.search(rf"(?<!\w){re.escape(w.lower())}(?!\w)", lowered)]


def clean_reply(
    text: str,
    *,
    max_chars: int,
    allowed_domains: list[str],
    blocked_words: list[str],
    forbid_contact_details: bool,
    emoji: str = "light",
    sign_off: str = "",
) -> Cleaned:
    """Strip hashtags and disallowed links, enforce the length, and flag what cannot be fixed."""
    violations: list[str] = []
    body = HASHTAG.sub("", text).strip()
    for link in URL.findall(body):
        if not _allowed(link, allowed_domains):
            body = body.replace(link, "").strip()
            violations.append("link")
    if forbid_contact_details and (EMAIL.search(body) or PHONE.search(body)):
        violations.append("contact_details")
    if _blocked(body, blocked_words):
        violations.append("blocked_word")
    if emoji == "none" and EMOJI.search(body):
        body = EMOJI.sub("", body).strip()
    elif emoji == "light" and len(EMOJI.findall(body)) > MAX_EMOJI_LIGHT:
        violations.append("too_many_emoji")
    body = re.sub(r"[ \t]{2,}", " ", body)
    if sign_off:
        body = f"{body} {sign_off}".strip()
    body = _trim(body, max_chars)
    if not body:
        violations.append("empty")
    return Cleaned(body, list(dict.fromkeys(violations)))


def mentions_any(text: str, keywords: list[str]) -> bool:
    """Whole-word, case-insensitive match of any keyword (so "stop" does not match "stopwatch")."""
    lowered = text.lower()
    return any(
        re.search(rf"(?<!\w){re.escape(k.lower())}(?!\w)", lowered) for k in keywords if k.strip()
    )
