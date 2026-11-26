"""Safe handling of a post body: sanitise what the AI writes, compare it with the original.

The sanitiser is an allow-list rebuilt from the parsed tags, so scripts, event handlers, iframes
and ``javascript:`` addresses can never reach WordPress, whatever the model returned.
"""

import html
import re
from html.parser import HTMLParser
from typing import Any

ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "a", "strong", "b",
    "em", "i", "blockquote", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "img", "figure",
    "figcaption", "div", "span", "code", "pre", "sup", "sub",
}  # fmt: skip
VOID_TAGS = {"br", "hr", "img"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "form", "svg", "noscript"}
COMMON_ATTRS = {"class", "id"}
TAG_ATTRS = {
    "a": {"href", "title", "rel", "target"},
    "img": {"src", "alt", "width", "height", "loading", "title"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
}
SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*):", re.I)
SAFE_SCHEMES = {"http", "https", "mailto", "tel"}
CONTROL = re.compile(r"[\x00-\x20\x7f]+")
SAFE_ATTR_VALUE = re.compile(r"^[\w\s\-:./,#%()@+=?&;]*$")
SHORTCODE = re.compile(r"\[[a-z][a-z0-9_-]*(?:[\s/\]])", re.I)
RISKY_MARKUP = re.compile(
    r"<(script|iframe|form|style|video|audio|object|embed|svg)\b|<!--\s*wp:", re.I
)
WORD = re.compile(r"[\w'-]+")
TAG = re.compile(r"<[^>]+>")
HREF = re.compile(r"""<a\b[^>]*?\shref\s*=\s*["']([^"']+)["']""", re.I)
SRC = re.compile(r"""<img\b[^>]*?\ssrc\s*=\s*["']([^"']+)["']""", re.I)


class _Cleaner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.open: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skip or tag in DROP_WITH_CONTENT:
            if tag in DROP_WITH_CONTENT and tag not in VOID_TAGS:
                self.skip += 1
            return
        name = tag
        if name not in ALLOWED_TAGS:
            return
        kept = _attributes(name, attrs)
        self.out.append(f"<{name}{kept}>")
        if name not in VOID_TAGS:
            self.open.append(name)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        name = tag
        if self.skip or name not in ALLOWED_TAGS or name in VOID_TAGS:
            return
        if name in self.open:
            while self.open:
                last = self.open.pop()
                self.out.append(f"</{last}>")
                if last == name:
                    break

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def close(self) -> None:
        super().close()
        while self.open:
            self.out.append(f"</{self.open.pop()}>")


def _safe_url(tag: str, address: str) -> bool:
    """Relative and http(s), mail and phone addresses pass; script-like schemes never do."""
    compact = CONTROL.sub("", html.unescape(address))
    scheme = SCHEME.match(compact)
    if scheme is None:
        return True
    name = scheme.group(1).lower()
    return name in SAFE_SCHEMES or (tag == "img" and compact.lower().startswith("data:image/"))


def _attributes(tag: str, attrs: list[tuple[str, str | None]]) -> str:
    allowed = COMMON_ATTRS | TAG_ATTRS.get(tag, set())
    parts: list[str] = []
    for key, value in attrs:
        name = key.lower()
        text = (value or "").strip()
        if name not in allowed:
            continue
        if name in {"href", "src"} and not _safe_url(tag, text):
            continue
        if name in {"class", "id", "rel", "target", "loading", "scope"} and not (
            SAFE_ATTR_VALUE.match(text)
        ):
            continue
        parts.append(f' {name}="{html.escape(text, quote=True)}"')
    if tag == "a" and 'target="_blank"' in "".join(parts) and "rel=" not in "".join(parts):
        parts.append(' rel="noopener"')
    return "".join(parts)


def clean_html(source: str) -> str:
    parser = _Cleaner()
    parser.feed(source)
    parser.close()
    return "".join(parser.out).strip()


def demote_new_h1(before: str, after: str) -> str:
    """Keep the H1s the post already had; an H1 added to a post that had none becomes an H2."""
    opening = re.compile(r"<h1[\s>]", re.I)
    if opening.search(before) or not opening.search(after):
        return after
    return re.sub(r"<(/?)h1(?=[\s>])", r"<\g<1>h2", after, flags=re.I)


def risky_reason(source: str) -> str:
    """Why this body should not be rewritten by the AI (empty when it is plain HTML)."""
    if RISKY_MARKUP.search(source):
        return "special_markup"
    if SHORTCODE.search(source):
        return "shortcodes"
    return ""


def is_builder_post(row: dict[str, Any]) -> bool:
    """True when the post is laid out by Elementor, so its body is not the plain HTML field."""
    raw = row.get("meta")
    meta: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return bool(meta.get("_elementor_data")) or meta.get("_elementor_edit_mode") == "builder"


def word_count(source: str) -> int:
    return len(WORD.findall(html.unescape(TAG.sub(" ", source))))


def links_of(source: str) -> set[str]:
    return {html.unescape(u).strip() for u in HREF.findall(source)}


def images_of(source: str) -> set[str]:
    return {html.unescape(u).strip() for u in SRC.findall(source)}


def lost_assets(before: str, after: str) -> list[str]:
    """Links and images of the original that the rewrite no longer contains."""
    return sorted((links_of(before) - links_of(after)) | (images_of(before) - images_of(after)))


def new_links(before: str, after: str) -> set[str]:
    return links_of(after) - links_of(before)
