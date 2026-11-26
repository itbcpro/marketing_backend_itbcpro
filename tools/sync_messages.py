#!/usr/bin/env python3
"""Synchronise locale/en/LC_MESSAGES/django.po with the messages used in the API source.

Adds new msgids (English source text is also the English translation), keeps existing entries and
reports unused ones. Run after adding or removing gettext calls; CI verifies the result with
check_i18n.py.
"""

import sys
from pathlib import Path

import polib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_i18n import REPO_ROOT, used_msgids  # noqa: E402

PO_PATH = REPO_ROOT / "locale" / "en" / "LC_MESSAGES" / "django.po"


def main(argv: list[str]) -> int:
    catalog = polib.pofile(str(PO_PATH))
    known = {entry.msgid for entry in catalog}
    used = used_msgids()
    added = sorted(used - known)
    unused = sorted(known - used)
    for msgid in added:
        catalog.append(polib.POEntry(msgid=msgid, msgstr=msgid))
    if "--prune" in argv:
        for entry in [e for e in catalog if e.msgid in unused]:
            catalog.remove(entry)
    catalog.save(str(PO_PATH))
    print(f"added {len(added)}, unused {len(unused)}{' (pruned)' if '--prune' in argv else ''}")
    for msgid in unused:
        print(f"  unused: {msgid!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
