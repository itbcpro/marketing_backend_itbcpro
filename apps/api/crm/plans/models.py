"""Plans: what each tier costs and allows. Rows are data, so prices change without a deploy."""

from __future__ import annotations

from django.db import models

from core.models import BaseModel


class Plan(BaseModel):
    key = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    price_micros = models.BigIntegerField(
        default=0, help_text="Per month, in the billing currency."
    )
    # {limit key: number}. A key that is missing means no limit.
    limits = models.JSONField(default=dict)
    is_public = models.BooleanField(default=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "key"]
        constraints = [
            models.CheckConstraint(condition=models.Q(price_micros__gte=0), name="plan_price_ok")
        ]

    def __str__(self) -> str:
        return self.name
