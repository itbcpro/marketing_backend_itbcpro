"""Turn page HTML into the clean facts the platform audits: text, outline, links and images."""

import hashlib
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

SKIPPED = {"script", "style", "noscript", "template", "svg", "iframe"}
BLOCKS = {
    "p", "div", "section", "article", "li", "ul", "ol", "br", "tr", "table", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "footer", "main", "figure", "figcaption",
}  # fmt: skip
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
ATTACHMENT = re.compile(r"\bwp-image-(\d+)\b")
MAX_IMAGES = 80
CONTEXT_CHARS = 160
SPACES = re.compile(r"[ \t\r\f\v]+")
BLANKS = re.compile(r"\n\s*\n+")


class _Reader(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.host = (urlsplit(base_url).hostname or "").lower().removeprefix("www.")
        self.parts: list[str] = []
        self.outline: list[dict[str, Any]] = []
        self.internal = 0
        self.external = 0
        self.images = 0
        self.missing_alt = 0
        self._skip = 0
        self._heading: tuple[int, list[str]] | None = None
        self.image_list: list[dict[str, Any]] = []
        self._seen_src: set[str] = set()
        self._last_heading = ""
        self._tail = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if tag in SKIPPED:
            self._skip += 1
            return
        if tag in BLOCKS:
            self.parts.append("\n")
        if tag in HEADINGS and self._skip == 0:
            self._heading = (HEADINGS[tag], [])
        elif tag == "a" and values.get("href"):
            self._count_link(values["href"])
        elif tag == "img" and self._skip == 0:
            self.images += 1
            if not values.get("alt", "").strip():
                self.missing_alt += 1
            self._remember_image(values)

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED:
            self._skip = max(0, self._skip - 1)
            return
        if tag in HEADINGS and self._heading is not None:
            level, words = self._heading
            text = " ".join("".join(words).split())
            if text:
                self.outline.append({"level": level, "text": text})
                self._last_heading = text
            self._heading = None
        if tag in BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.parts.append(data)
        self._tail = (self._tail + " " + " ".join(data.split()))[-CONTEXT_CHARS:]
        if self._heading is not None:
            self._heading[1].append(data)

    def _remember_image(self, values: dict[str, str]) -> None:
        src = values.get("src", "")
        if src.startswith("data:") or not src:
            src = values.get("data-src") or values.get("data-lazy-src") or src
        if not src or src.startswith("data:") or src in self._seen_src:
            return
        if len(self.image_list) >= MAX_IMAGES:
            return
        self._seen_src.add(src)
        match = ATTACHMENT.search(values.get("class", ""))
        self.image_list.append(
            {
                "src": urljoin(self.base, src),
                "alt": values.get("alt", "").strip(),
                "attachment_id": int(match.group(1)) if match else None,
                "width": values.get("width", ""),
                "height": values.get("height", ""),
                "heading": self._last_heading,
                "near": self._tail.strip(),
            }
        )

    def _count_link(self, href: str) -> None:
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            return
        target = urlsplit(urljoin(self.base, href))
        if target.scheme not in {"http", "https"}:
            return
        if (target.hostname or "").lower().removeprefix("www.") == self.host:
            self.internal += 1
        else:
            self.external += 1


def normalize_html(html: str, base_url: str) -> dict[str, Any]:
    """Plain text, heading outline and counts for a fragment or a whole page."""
    reader = _Reader(base_url)
    reader.feed(html)
    reader.close()
    raw = "".join(reader.parts)
    text = BLANKS.sub("\n\n", "\n".join(SPACES.sub(" ", line).strip() for line in raw.splitlines()))
    text = text.strip()
    return {
        "text": text,
        "outline": reader.outline,
        "images": reader.image_list,
        "stats": {
            "word_count": len(text.split()),
            "internal_links": reader.internal,
            "external_links": reader.external,
            "images": reader.images,
            "images_missing_alt": reader.missing_alt,
        },
    }


def content_hash(title: str, text: str) -> str:
    return hashlib.sha256(f"{title}\n{text}".encode()).hexdigest()
