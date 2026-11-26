#!/usr/bin/env python3
"""Compile every locale/*/LC_MESSAGES/django.po into a .mo file without GNU gettext."""

import sys
from pathlib import Path

import polib

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    compiled = 0
    for po_path in sorted((REPO_ROOT / "locale").glob("*/LC_MESSAGES/*.po")):
        catalog = polib.pofile(str(po_path))
        catalog.save_as_mofile(str(po_path.with_suffix(".mo")))
        compiled += 1
        print(f"compiled {po_path.relative_to(REPO_ROOT).as_posix()}")
    if compiled == 0:
        print("no catalogs found")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
