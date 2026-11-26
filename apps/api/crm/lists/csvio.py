"""CSV reading and writing helpers shared by import and export."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator

from crm.common.csvwrite import FORMULA_PREFIXES, safe_cell, write

__all__ = ["FORMULA_PREFIXES", "decode", "detect_delimiter", "read_rows", "safe_cell", "write"]

DELIMITERS = ",;\t"


def decode(raw: bytes) -> str:
    """UTF-8 (with or without BOM) only; anything else is rejected so no data is mangled."""
    return raw.decode("utf-8-sig")


def detect_delimiter(sample: str) -> str:
    first = sample.splitlines()[0] if sample.splitlines() else ""
    counts = {d: first.count(d) for d in DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def read_rows(text: str) -> Iterator[list[str]]:
    delimiter = detect_delimiter(text[:4096])
    yield from csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
