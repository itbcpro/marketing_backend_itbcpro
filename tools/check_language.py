#!/usr/bin/env python3
"""Fail when any tracked file contains Arabic-script text.

Rule: the application, its code, comments, docs, configuration and message catalogs are English only.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_RANGES = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
    (0x200F, 0x200F),  # right-to-left mark
    (0x202B, 0x202E),  # bidi embedding / override controls
    (0x2067, 0x2067),  # right-to-left isolate
)

ALLOWED_PREFIXES: tuple[str, ...] = ()

SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf",
    ".pdf", ".mo", ".lock", ".zip", ".gz", ".sqlite3",
}
SKIP_DIRS = {".git", "node_modules", ".venv", ".next", "__pycache__", ".mypy_cache", ".ruff_cache"}


def is_forbidden(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in FORBIDDEN_RANGES)


def tracked_files() -> list[Path]:
    """Files git knows about (tracked or untracked but not ignored); falls back to a tree walk."""
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [REPO_ROOT / line for line in out.splitlines() if line]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [
            p
            for p in REPO_ROOT.rglob("*")
            if p.is_file() and not SKIP_DIRS.intersection(p.parts)
        ]


def is_allowed(path: Path) -> bool:
    relative = path.relative_to(REPO_ROOT).as_posix()
    return relative.startswith(ALLOWED_PREFIXES)


def scan(path: Path) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return findings
    for number, line in enumerate(text.splitlines(), start=1):
        if any(is_forbidden(ch) for ch in line):
            codes = sorted({f"U+{ord(ch):04X}" for ch in line if is_forbidden(ch)})
            findings.append((number, ",".join(codes[:5])))
    return findings


def main(argv: list[str]) -> int:
    paths = [Path(a).resolve() for a in argv] if argv else tracked_files()
    failures = 0
    for path in paths:
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if SKIP_DIRS.intersection(path.parts) or is_allowed(path):
            continue
        for number, snippet in scan(path):
            failures += 1
            print(f"{path.relative_to(REPO_ROOT).as_posix()}:{number}: Arabic script ({snippet})")
    if failures:
        print(f"\n{failures} line(s) contain Arabic/Persian script in the repository.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
