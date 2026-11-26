"""Backlogs read from the database when Prometheus scrapes."""

from __future__ import annotations

from collections.abc import Iterable

from django.db import DatabaseError
from django.utils import timezone
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from core.metrics import REGISTRY
from crm.ops import health


class CrmCollector(Collector):
    """Backlogs of the outbox, automation runs and dead letters."""

    def describe(self) -> Iterable[Metric]:
        return []  # keeps registration from reading the database at import time

    def collect(self) -> Iterable[Metric]:
        try:
            totals = health.platform_totals(timezone.now())
        except DatabaseError:
            return []
        gauges = []
        for name, doc, value in (
            (
                "crm_outbox_oldest_pending_seconds",
                "Age of the oldest unprocessed event",
                totals.outbox_lag_seconds,
            ),
            (
                "crm_overdue_enrollments",
                "Automation runs past their wake time",
                totals.overdue_runs,
            ),
            ("crm_dead_letters_open", "Unresolved dead-lettered tasks", totals.dead_letters),
        ):
            gauge = GaugeMetricFamily(name, doc)
            gauge.add_metric([], float(value))
            gauges.append(gauge)
        return gauges


def register_collector() -> None:
    REGISTRY.register(CrmCollector())
