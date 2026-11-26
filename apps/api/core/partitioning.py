"""Monthly range-partition maintenance for time-series tables (for example metric snapshots).

A parent table must be declared ``PARTITION BY RANGE (<date column>)``. This module creates the
upcoming monthly partitions and drops those older than the retention window.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from django.db import connection

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")


@dataclass(frozen=True, slots=True)
class PartitionedTable:
    name: str
    keep_months: int = 25
    months_ahead: int = 3

    def __post_init__(self) -> None:
        if not _IDENTIFIER.match(self.name):
            raise ValueError(f"unsafe table name: {self.name!r}")


_registry: dict[str, PartitionedTable] = {}


def register_partitioned_table(table: PartitionedTable) -> None:
    _registry[table.name] = table


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _add_months(value: date, months: int) -> date:
    index = value.year * 12 + (value.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def partition_name(table: str, month: date) -> str:
    return f"{table}_p{month:%Y%m}"


def ensure_partitions(table: PartitionedTable, *, today: date | None = None) -> list[str]:
    """Create the current and upcoming monthly partitions. Returns the names created."""
    start = _month_start(today or datetime.now(UTC).date())
    created: list[str] = []
    with connection.cursor() as cursor:
        for offset in range(table.months_ahead + 1):
            month = _add_months(start, offset)
            name = partition_name(table.name, month)
            cursor.execute("SELECT to_regclass(%s)", [name])
            row = cursor.fetchone()
            if row and row[0] is not None:
                continue
            upper = _add_months(month, 1)
            cursor.execute(
                f"CREATE TABLE {name} PARTITION OF {table.name} "
                f"FOR VALUES FROM ('{month:%Y-%m-%d}') TO ('{upper:%Y-%m-%d}')"
            )
            created.append(name)
    return created


def drop_expired_partitions(table: PartitionedTable, *, today: date | None = None) -> list[str]:
    """Drop monthly partitions that end before the retention window. Returns the names dropped."""
    cutoff = _add_months(_month_start(today or datetime.now(UTC).date()), -table.keep_months)
    dropped: list[str] = []
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "WHERE p.relname = %s",
            [table.name],
        )
        pattern = re.compile(rf"^{re.escape(table.name)}_p(\d{{4}})(\d{{2}})$")
        for (relname,) in cursor.fetchall():
            match = pattern.match(relname)
            if match and date(int(match[1]), int(match[2]), 1) < cutoff:
                cursor.execute(f"DROP TABLE {relname}")
                dropped.append(relname)
    return sorted(dropped)


def maintain_all(*, today: date | None = None) -> dict[str, dict[str, list[str]]]:
    return {
        name: {
            "created": ensure_partitions(table, today=today),
            "dropped": drop_expired_partitions(table, today=today),
        }
        for name, table in _registry.items()
    }
