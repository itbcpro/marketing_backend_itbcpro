"""Catch the habits that make text read as machine written.

The prompt asks the writer to avoid them, but a request is not a guarantee, so this module checks
the finished text. The list of phrases and patterns is a data file the team can edit; the owner's
own banned phrases are added on top. Findings are advice (the writer gets one chance to fix them),
except the owner's banned phrases, which block approval.
"""

import json
import re
import statistics
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from social.text_rules import EMOJI

DATA = Path(__file__).parent / "data" / "ai_tells.json"
EM_DASH = "—"
APOSTROPHES = "['" + chr(0x2019) + "]"
SENTENCES = re.compile(r"(?<=[.!?])\s+")
WORD = re.compile(r"\w+")
MAX_LIGHT_EMOJI = 2


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: str
    match: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "match": self.match}


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    return json.loads(DATA.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _phrase_regex(phrase: str) -> re.Pattern[str]:
    loose = phrase.replace("'", APOSTROPHES)  # typographic apostrophes count too
    return re.compile(rf"(?<!\w){loose}(?!\w)", re.IGNORECASE)


@lru_cache(maxsize=1)
def _compiled() -> tuple[
    tuple[tuple[str, re.Pattern[str]], ...], tuple[tuple[str, re.Pattern[str]], ...]
]:
    data = _data()
    phrases = tuple((p, _phrase_regex(p)) for p in data["phrases"])
    patterns = tuple((n, re.compile(p, re.IGNORECASE)) for n, p in data["patterns"].items())
    return phrases, patterns


def _phrase_findings(text: str) -> list[Finding]:
    phrases, patterns = _compiled()
    found = [
        Finding("ai_phrase", "warning", m.group(0)) for _, rx in phrases if (m := rx.search(text))
    ]
    found += [
        Finding(name, "warning", m.group(0)[:60]) for name, rx in patterns if (m := rx.search(text))
    ]
    return found


def _owner_findings(text: str, banned: list[str]) -> list[Finding]:
    return [
        Finding("banned_phrase", "error", phrase)
        for phrase in banned
        if phrase and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE)
    ]


def _count_findings(text: str, emoji: str) -> list[Finding]:
    limits = _data()["limits"]
    found: list[Finding] = []
    if text.count(EM_DASH) > limits["max_em_dashes"]:
        found.append(Finding("em_dash", "warning", EM_DASH))
    if text.count("!") > limits["max_exclamations"]:
        found.append(Finding("exclamations", "warning", "!"))
    emoji_count = len(EMOJI.findall(text))
    if (emoji == "none" and emoji_count) or (emoji == "light" and emoji_count > MAX_LIGHT_EMOJI):
        found.append(Finding("emoji", "warning", str(emoji_count)))
    return found


def _rhythm_findings(text: str) -> list[Finding]:
    limits = _data()["limits"]
    lengths = [len(WORD.findall(s)) for s in SENTENCES.split(text.strip()) if s.strip()]
    if (
        len(lengths) >= limits["min_sentences_for_rhythm"]
        and statistics.pstdev(lengths) < limits["min_rhythm_spread"]
    ):
        return [Finding("uniform_rhythm", "warning")]
    return []


def lint(text: str, *, banned: list[str], emoji: str = "light") -> list[Finding]:
    """Everything in ``text`` that reads as machine written or that the owner forbade."""
    body = text.strip()
    if not body:
        return []
    return [
        *_owner_findings(body, banned),
        *_phrase_findings(body),
        *_count_findings(body, emoji),
        *_rhythm_findings(body),
    ]


def lint_fragments(
    fragments: list[str], *, banned: list[str], emoji: str = "light"
) -> list[Finding]:
    """Like ``lint`` for short pieces of text (slides), where sentence rhythm means nothing."""
    text = "\n".join(f.strip() for f in fragments if f.strip())
    if not text:
        return []
    return [*_owner_findings(text, banned), *_phrase_findings(text), *_count_findings(text, emoji)]


def feedback(findings: list[Finding]) -> str:
    """A short instruction for the writer listing what to remove, in plain words."""
    items = [f"{f.code}: {f.match}" if f.match else f.code for f in findings]
    return (
        "Rewrite the post so it sounds like a person wrote it. Remove or rephrase: "
        + "; ".join(items)
        + ". Keep the facts and the structure."
    )


def blocks_approval(findings: list[Finding]) -> bool:
    return any(f.severity == "error" for f in findings)
