"""CSV writing shared by every CRM export: formula-safe cells, streamed line by line."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: object) -> str:
    """Neutralise spreadsheet formulas so an exported value cannot run code when opened."""
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(FORMULA_PREFIXES) else text


def write(rows: Iterable[Iterable[object]]) -> Iterator[str]:
    """Yield CSV text line by line."""
    for row in rows:
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="\r\n").writerow([safe_cell(c) for c in row])
        yield buffer.getvalue()
