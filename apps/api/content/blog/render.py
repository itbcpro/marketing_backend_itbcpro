"""Turn a finished article into WordPress HTML and an Elementor layout.

The Markdown converter escapes everything first and then recognises a small set of patterns, so
model output can never inject HTML or scripts. Only http and https links survive.
"""

import html
import json
import re
import uuid
from pathlib import Path
from typing import Any

from content.models import Article

FALLBACK_FAQ_HEADING = "Frequently asked questions"
INLINE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)|\*\*([^*]+)\*\*|\*([^*\s][^*]*)\*")
HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
LIST_ITEM = re.compile(r"^\s*([-*]|\d+\.)\s+(.*)$")
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}")
SECTION_HEADING = re.compile(r"^##\s+(.*)$")
MIN_TABLE_ROWS = 2
ARTICLE_CLASS = "itbcpro-article"
READING_WIDTH = 820
ARTICLE_CSS = (Path(__file__).parent / "article.css").read_text(encoding="utf-8").strip()
LD_SCRIPT = '<script type="application/ld+json">{}</script>'


def _inline(text: str) -> str:
    out: list[str] = []
    last = 0
    for match in INLINE.finditer(text):
        out.append(html.escape(text[last : match.start()]))
        label, url, bold, italic = match.groups()
        if url:
            out.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>')
        elif bold:
            out.append(f"<strong>{html.escape(bold)}</strong>")
        else:
            out.append(f"<em>{html.escape(italic or '')}</em>")
        last = match.end()
    out.append(html.escape(text[last:]))
    return "".join(out)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table(rows: list[str]) -> str:
    head, *body = [r for i, r in enumerate(rows) if i != 1]
    th = "".join(f"<th>{_inline(c)}</th>" for c in _cells(head))
    trs = "".join(
        "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in _cells(row)) + "</tr>" for row in body
    )
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def markdown_to_html(markdown: str) -> str:
    lines = markdown.split("\n")
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        heading = HEADING.match(line)
        if not line.strip():
            i += 1
        elif heading:
            level = min(max(len(heading.group(1)), 2), 4)
            blocks.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
        elif "|" in line and i + 1 < len(lines) and TABLE_RULE.match(lines[i + 1]):
            rows = []
            while i < len(lines) and "|" in lines[i]:
                rows.append(lines[i])
                i += 1
            blocks.append(_table(rows) if len(rows) >= MIN_TABLE_ROWS else "")
        elif LIST_ITEM.match(line):
            ordered = line.lstrip()[0].isdigit()
            items = []
            while i < len(lines) and (m := LIST_ITEM.match(lines[i])):
                items.append(f"<li>{_inline(m.group(2))}</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            blocks.append(f"<{tag}>{''.join(items)}</{tag}>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not HEADING.match(lines[i]):
                para.append(lines[i].strip())
                i += 1
            blocks.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(b for b in blocks if b)


def split_section(markdown: str) -> tuple[str, str]:
    """The H2 text and the rest of a section's Markdown."""
    lines = markdown.strip().split("\n")
    match = SECTION_HEADING.match(lines[0]) if lines else None
    if not match:
        return "", markdown
    return match.group(1).strip(), "\n".join(lines[1:]).strip()


def faq_heading(article: Article) -> str:
    return str(article.plan.get("faq_heading") or FALLBACK_FAQ_HEADING)


def faq_jsonld(article: Article) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": q["question"],
                "acceptedAnswer": {"@type": "Answer", "text": q["answer"]},
            }
            for q in article.faq
        ],
    }
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def _inline_images(article: Article) -> dict[str, list[dict[str, Any]]]:
    """Uploaded images by lower-cased section heading."""
    found: dict[str, list[dict[str, Any]]] = {}
    sections = [split_section(s["markdown"])[0] for s in article.sections]
    for entry in article.images:
        if entry["status"] != "ready" or not entry.get("wp_url"):
            continue
        place = str(entry["placement"]).strip().lower()
        if place == "featured":
            continue
        for heading in sections:
            if heading and (place == heading.lower() or place in heading.lower()):
                found.setdefault(heading.lower(), []).append(entry)
                break
    return found


def featured_image(article: Article) -> dict[str, Any] | None:
    for entry in article.images:
        ready = entry["status"] == "ready" and entry.get("wp_media_id")
        if ready and str(entry["placement"]).strip().lower() == "featured":
            return dict(entry)
    return None


def _figure(entry: dict[str, Any]) -> str:
    caption = entry.get("caption") or ""
    cap = f"<figcaption>{html.escape(caption)}</figcaption>" if caption else ""
    src = html.escape(entry["wp_url"], quote=True)
    alt = html.escape(entry.get("alt", ""), quote=True)
    return f'<figure><img src="{src}" alt="{alt}" loading="lazy">{cap}</figure>'


def build_html(article: Article) -> str:
    """The post body as plain HTML (also the fallback when Elementor is not used)."""
    placed = _inline_images(article)
    parts: list[str] = []
    for section in article.sections:
        heading, body = split_section(section["markdown"])
        parts.append(f"<h2>{html.escape(heading)}</h2>" if heading else "")
        parts.extend(_figure(e) for e in placed.get(heading.lower(), []))
        parts.append(markdown_to_html(body))
    if article.faq:
        parts.append(f"<h2>{html.escape(faq_heading(article))}</h2>")
        parts.extend(
            f"<h3>{html.escape(q['question'])}</h3>\n<p>{html.escape(q['answer'])}</p>"
            for q in article.faq
        )
    return "\n".join(p for p in parts if p)


# ---- Elementor -------------------------------------------------------------------------------


def _id() -> str:
    return uuid.uuid4().hex[:7]


def _widget(kind: str, settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _id(),
        "elType": "widget",
        "settings": settings,
        "elements": [],
        "widgetType": kind,
    }


def _row(widgets: list[dict[str, Any]]) -> dict[str, Any]:
    """One boxed section at reading width, tagged so ``ARTICLE_CSS`` applies to it."""
    column = {
        "id": _id(),
        "elType": "column",
        "settings": {"_column_size": 100},
        "elements": widgets,
        "isInner": False,
    }
    settings = {
        "content_width": {"unit": "px", "size": READING_WIDTH, "sizes": []},
        "css_classes": ARTICLE_CLASS,
    }
    return {
        "id": _id(),
        "elType": "section",
        "settings": settings,
        "elements": [column],
        "isInner": False,
    }


def _image_widget(entry: dict[str, Any]) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "image": {"url": entry["wp_url"], "id": entry["wp_media_id"], "alt": entry.get("alt", "")},
        "image_size": "large",
    }
    if entry.get("caption"):
        settings.update(caption_source="custom", caption=entry["caption"])
    return _widget("image", settings)


def build_elementor(article: Article) -> list[dict[str, Any]]:
    """Sections with a heading, optional images and rich text, then the FAQ accordion."""
    placed = _inline_images(article)
    rows: list[dict[str, Any]] = []
    style = _widget("html", {"html": f"<style>{ARTICLE_CSS}</style>"})
    for section in article.sections:
        heading, body = split_section(section["markdown"])
        widgets = [_widget("heading", {"title": heading, "header_size": "h2"})] if heading else []
        if not rows:
            widgets.insert(0, style)
        widgets.extend(_image_widget(e) for e in placed.get(heading.lower(), []))
        widgets.append(_widget("text-editor", {"editor": markdown_to_html(body)}))
        rows.append(_row(widgets))
    if article.faq:
        tabs = [
            {"_id": _id(), "tab_title": q["question"], "tab_content": html.escape(q["answer"])}
            for q in article.faq
        ]
        rows.append(
            _row(
                [
                    _widget("heading", {"title": faq_heading(article), "header_size": "h2"}),
                    _widget("accordion", {"tabs": tabs, "title_html_tag": "h3"}),
                    _widget(
                        "html",
                        {"html": LD_SCRIPT.format(faq_jsonld(article))},
                    ),
                ]
            )
        )
    return rows
