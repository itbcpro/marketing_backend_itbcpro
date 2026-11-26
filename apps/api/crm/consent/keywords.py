"""Keyword catalog for inbound messages, per language. Data only; matching is exact."""

import re
from enum import StrEnum


class KeywordAction(StrEnum):
    OPT_OUT = "opt_out"
    OPT_IN = "opt_in"
    HELP = "help"


CATALOG: dict[str, dict[KeywordAction, frozenset[str]]] = {
    "en": {
        KeywordAction.OPT_OUT: frozenset(
            {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
        ),
        KeywordAction.OPT_IN: frozenset({"START", "UNSTOP", "YES", "SUBSCRIBE"}),
        KeywordAction.HELP: frozenset({"HELP", "INFO"}),
    },
    "fr": {
        KeywordAction.OPT_OUT: frozenset({"ARRET", "ARRETER", "STOP", "DESABONNER"}),
        KeywordAction.OPT_IN: frozenset({"DEBUT", "OUI", "ABONNER"}),
        KeywordAction.HELP: frozenset({"AIDE"}),
    },
}
_CLEAN = re.compile(r"[^A-Z]")


def _normalise(text: str) -> str:
    folded = text.strip().upper().replace("É", "E").replace("È", "E").replace("Ê", "E")
    return _CLEAN.sub("", folded)


def classify(text: str, languages: tuple[str, ...] = ("en", "fr")) -> KeywordAction | None:
    """Return the action for a message that is exactly one keyword, else ``None``."""
    word = _normalise(text)
    if not word or len(text.split()) > 1:
        return None
    for language in languages:
        for action, words in CATALOG.get(language, {}).items():
            if word in words:
                return action
    return None
