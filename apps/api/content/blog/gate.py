"""Measured quality gate for a written article. Numbers come from the text, never from the model."""

import re
from typing import Any

from content.models import Article

WORD = re.compile(r"[\w'-]+")
MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
LINK = re.compile(r"\[[^\]]+\]\((https?://[^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
SENTENCE = re.compile(r"(?<=[.!?])\s+")
BANNED = (
    "in today's digital",
    "it is important to note",
    "whether you are",
    "when it comes to",
    "in conclusion",
    "furthermore",
    "moreover",
    "delve",
    "game-changer",
    "in the ever-evolving",
    "navigate the landscape",
    "unlock the",
    "seamless",
)
SEO_TITLE_MAX = 65
META_MIN, META_MAX = 120, 170
FAQ_MIN = 20
H2_MIN = 5
LINKS_MIN = 3
LONG_SENTENCE = 8
H2, H3 = 2, 3
FAQ_WORDS = (30, 190)
SECTION_SHORT = 0.7
Check = dict[str, Any]


def count_words(text: str) -> int:
    return len(WORD.findall(text))


def body_markdown(article: Article) -> str:
    return "\n\n".join(str(s.get("markdown", "")) for s in article.sections)


def full_text(article: Article) -> str:
    faq = "\n".join(f"{q['question']}\n{q['answer']}" for q in article.faq)
    return f"# {article.meta.get('h1', article.title)}\n\n{body_markdown(article)}\n\n{faq}"


def _check(key: str, ok: bool, value: Any, expected: Any, *, hard: bool = True) -> Check:
    status = "pass" if ok else ("fail" if hard else "warn")
    return {"key": key, "status": status, "value": value, "expected": expected}


def _contains(text: str, phrase: str) -> bool:
    return phrase.lower() in text.lower() if phrase else False


def _slug_has(slug: str, phrase: str) -> bool:
    words = [w for w in re.split(r"\W+", phrase.lower()) if w]
    return bool(words) and all(w in slug for w in words)


def _repeated_sentences(text: str) -> int:
    seen: dict[str, int] = {}
    for sentence in SENTENCE.split(re.sub(r"\s+", " ", text)):
        key = sentence.strip().lower()
        if len(key.split()) >= LONG_SENTENCE:
            seen[key] = seen.get(key, 0) + 1
    return sum(1 for n in seen.values() if n > 1)


def _links(body: str, allowed: set[str]) -> tuple[list[str], list[str]]:
    urls = LINK.findall(body)
    internal = [u for u in urls if u.rstrip("/") in allowed]
    return urls, internal


def run(article: Article, neighbor_urls: set[str], domain: str) -> dict[str, Any]:
    """Run every measurable check and say whether the article may be offered for review."""
    body = body_markdown(article)
    meta = article.meta
    words = count_words(full_text(article))
    heads = HEADING.findall(body)
    h2 = [t for level, t in heads if len(level) == H2]
    h3 = [t for level, t in heads if len(level) == H3]
    keyword = article.primary_keyword
    first_100 = " ".join(WORD.findall(body)[:100])
    allowed = {u.rstrip("/") for u in neighbor_urls}
    urls, internal = _links(body, allowed)
    foreign = [u for u in urls if u.rstrip("/") not in allowed and domain and domain in u]
    banned = sorted({p for p in BANNED if p in full_text(article).lower()})
    answers = [count_words(q["answer"]) for q in article.faq]
    short = [
        s["heading"]
        for s in article.sections
        if s.get("words", 0) < SECTION_SHORT * s.get("target", 0)
    ]
    checks = [
        _check("word_count", words >= article.min_words, words, f">= {article.min_words}"),
        _check("h2_count", len(h2) >= H2_MIN, len(h2), f">= {H2_MIN}"),
        _check(
            "keyword_in_h1", _contains(meta.get("h1", ""), keyword), meta.get("h1", ""), keyword
        ),
        _check("keyword_in_intro", _contains(first_100, keyword), None, keyword, hard=False),
        _check(
            "keyword_in_heading",
            any(_contains(h, keyword) for h in h2 + h3),
            None,
            keyword,
            hard=False,
        ),
        _check(
            "keyword_in_seo_title",
            _contains(meta.get("seo_title", ""), keyword),
            meta.get("seo_title", ""),
            keyword,
            hard=False,
        ),
        _check(
            "keyword_in_meta",
            _contains(meta.get("meta_description", ""), keyword),
            None,
            keyword,
            hard=False,
        ),
        _check(
            "keyword_in_slug",
            _slug_has(meta.get("slug", ""), keyword),
            meta.get("slug", ""),
            keyword,
            hard=False,
        ),
        _check(
            "seo_title_length",
            0 < len(meta.get("seo_title", "")) <= SEO_TITLE_MAX,
            len(meta.get("seo_title", "")),
            f"<= {SEO_TITLE_MAX}",
        ),
        _check(
            "meta_length",
            META_MIN <= len(meta.get("meta_description", "")) <= META_MAX,
            len(meta.get("meta_description", "")),
            f"{META_MIN}-{META_MAX}",
        ),
        _check("faq_count", len(article.faq) >= FAQ_MIN, len(article.faq), f">= {FAQ_MIN}"),
        _check(
            "faq_answer_length",
            all(FAQ_WORDS[0] <= n <= FAQ_WORDS[1] for n in answers),
            [n for n in answers if not FAQ_WORDS[0] <= n <= FAQ_WORDS[1]],
            f"{FAQ_WORDS[0]}-{FAQ_WORDS[1]} words",
            hard=False,
        ),
        _check(
            "internal_links",
            len({u.rstrip("/") for u in internal}) >= LINKS_MIN or len(allowed) < LINKS_MIN,
            len(internal),
            f">= {LINKS_MIN}",
            hard=False,
        ),
        _check("links_only_known_pages", not foreign, foreign, "no invented internal addresses"),
        _check("no_banned_phrases", not banned, banned, "none", hard=False),
        _check(
            "no_repeated_sentences",
            _repeated_sentences(body) == 0,
            _repeated_sentences(body),
            0,
            hard=False,
        ),
        _check(
            "sections_long_enough", not short, short, "each section near its target", hard=False
        ),
    ]
    failed = [c["key"] for c in checks if c["status"] == "fail"]
    return {"checks": checks, "passed": not failed, "failed": failed, "word_count": words}
