"""Every request and response model needs a name of its own in the OpenAPI document.

Ninja files models by class name. Two modules that both define ``SettingsOut`` end up sharing one
entry, so the generated web types silently describe the wrong shape for one of them.
"""

import ast
from collections import defaultdict
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
SKIP = {".venv", "migrations", "tests", "__pycache__"}


def _is_schema(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(base, ast.Name) and base.id == "Schema")
        or (isinstance(base, ast.Attribute) and base.attr == "Schema")
        for base in node.bases
    )


def test_schema_class_names_are_unique() -> None:
    seen: dict[str, list[str]] = defaultdict(list)
    for path in API_ROOT.rglob("*.py"):
        relative = path.relative_to(API_ROOT)
        if SKIP.intersection(relative.parts):
            continue
        if not (path.name.startswith("api") or path.name == "schemas.py"):
            continue
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ClassDef) and _is_schema(node):
                seen[node.name].append(relative.as_posix())
    duplicates = {name: files for name, files in seen.items() if len(files) > 1}
    assert not duplicates, f"schema names used by more than one module: {duplicates}"
