#!/usr/bin/env python3
"""Validate translation catalogs.

Backend: every msgid used through gettext helpers in apps/api must exist in each locale catalog,
must be translated, and must not be marked fuzzy. Unused catalog entries are reported as errors.
Frontend: message files for all locales must expose exactly the same set of keys.
"""

import json
import re
import sys
from pathlib import Path

import polib

REPO_ROOT = Path(__file__).resolve().parent.parent
API_ROOT = REPO_ROOT / "apps" / "api"
MESSAGES_DIR = REPO_ROOT / "apps" / "web" / "messages"

USAGE = re.compile(
    r"""\b(?:gettext_lazy|gettext|ngettext|pgettext)\(\s*(?:_\()?\s*(?P<q>["'])(?P<text>(?:(?!(?P=q)).)+)(?P=q)"""
    r"""|\b_\(\s*(?P<q2>["'])(?P<text2>(?:(?!(?P=q2)).)+)(?P=q2)""",
    re.DOTALL,
)
SKIP = {".venv", "migrations", "__pycache__", "tests"}


def used_msgids() -> set[str]:
    found: set[str] = set()
    for path in API_ROOT.rglob("*.py"):
        if SKIP.intersection(path.parts):
            continue
        source = path.read_text(encoding="utf-8")
        for match in USAGE.finditer(source):
            found.add(match.group("text") or match.group("text2"))
    return found


def check_backend() -> list[str]:
    errors: list[str] = []
    used = used_msgids()
    for po_path in sorted((REPO_ROOT / "locale").glob("*/LC_MESSAGES/django.po")):
        locale = po_path.parent.parent.name
        catalog = polib.pofile(str(po_path))
        entries = {e.msgid: e for e in catalog}
        for msgid in sorted(used - entries.keys()):
            errors.append(f"[{locale}] missing msgid: {msgid!r}")
        for msgid, entry in entries.items():
            if msgid not in used:
                errors.append(f"[{locale}] unused msgid: {msgid!r}")
            elif locale != "en" and not entry.msgstr.strip():
                errors.append(f"[{locale}] untranslated: {msgid!r}")
            elif "fuzzy" in entry.flags:
                errors.append(f"[{locale}] fuzzy entry: {msgid!r}")
    return errors


def flatten(node: object, prefix: str = "") -> set[str]:
    if isinstance(node, dict):
        keys: set[str] = set()
        for key, value in node.items():
            keys |= flatten(value, f"{prefix}.{key}" if prefix else key)
        return keys
    return {prefix}


def check_frontend() -> list[str]:
    if not MESSAGES_DIR.is_dir():
        return []
    catalogs = {
        p.stem: flatten(json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(MESSAGES_DIR.glob("*.json"))
    }
    if not catalogs:
        return []
    reference_locale = "en" if "en" in catalogs else next(iter(catalogs))
    reference = catalogs[reference_locale]
    errors: list[str] = []
    for locale, keys in catalogs.items():
        errors.extend(f"[{locale}] missing key: {k}" for k in sorted(reference - keys))
        errors.extend(f"[{locale}] extra key: {k}" for k in sorted(keys - reference))
    return errors


def main() -> int:
    errors = check_backend() + check_frontend()
    for error in errors:
        print(error)
    if errors:
        print(f"\n{len(errors)} i18n problem(s) found.")
        return 1
    print("i18n catalogs are consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
