#!/usr/bin/env python3
"""Export JSON Schemas of the CRM event envelope and every registered event into contracts/events/.

Usage (from apps/api):  python ../../tools/export_event_schemas.py [--check]
With --check the script fails when the committed files differ from the code (CI drift guard).
"""

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
API_ROOT = REPO_ROOT / "apps" / "api"
TARGET = REPO_ROOT / "contracts" / "events"


def build() -> dict[str, str]:
    sys.path.insert(0, str(API_ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
    import django

    django.setup()
    from crm.events.envelope import EventEnvelope
    from crm.events.registry import all_events

    files = {"envelope.schema.json": EventEnvelope.model_json_schema()}
    for (event_type, version), model in all_events().items():
        files[f"{event_type}.v{version}.schema.json"] = model.model_json_schema()
    return {name: json.dumps(schema, indent=2, sort_keys=True) + "\n" for name, schema in files.items()}


def main(argv: list[str]) -> int:
    rendered = build()
    if "--check" in argv:
        existing = {p.name: p.read_text(encoding="utf-8") for p in TARGET.glob("*.schema.json")}
        normalize = lambda text: text.replace("\r\n", "\n")  # noqa: E731
        stale = sorted(
            name
            for name in rendered.keys() | existing.keys()
            if normalize(rendered.get(name, "")) != normalize(existing.get(name, ""))
        )
        if stale:
            print("event schemas are out of date: " + ", ".join(stale))
            return 1
        return 0
    TARGET.mkdir(parents=True, exist_ok=True)
    for stale in TARGET.glob("*.schema.json"):
        if stale.name not in rendered:
            stale.unlink()
    for name, text in rendered.items():
        (TARGET / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(rendered)} schema files to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
