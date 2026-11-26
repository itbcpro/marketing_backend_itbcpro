#!/usr/bin/env python3
"""Export the Django Ninja OpenAPI schema so the TypeScript client can be generated from it.

Usage (from apps/api):  python ../../tools/export_openapi.py [--check]
With --check the script fails if the committed schema differs from the code (CI drift guard).
"""

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
API_ROOT = REPO_ROOT / "apps" / "api"
TARGET = REPO_ROOT / "packages" / "api-client" / "openapi.json"


def build_schema() -> str:
    sys.path.insert(0, str(API_ROOT))
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
    import django

    django.setup()
    from config.api import api

    schema = api.get_openapi_schema()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    rendered = build_schema()
    if "--check" in argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != rendered:
            print("OpenAPI schema is out of date. Run: python tools/export_openapi.py")
            return 1
        print("OpenAPI schema is up to date.")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
