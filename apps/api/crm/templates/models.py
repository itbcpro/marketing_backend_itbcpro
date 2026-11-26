"""Reusable message templates and short snippets, with merge fields."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class TemplateKind(models.TextChoices):
    TEMPLATE = "template"
    SNIPPET = "snippet"


class MessageTemplate(TenantModel):
    """A template (a full message) or a snippet (a short canned reply) for one channel or all."""

    name = models.CharField(max_length=120)
    kind = models.CharField(
        max_length=10, choices=TemplateKind.choices, default=TemplateKind.TEMPLATE
    )
    channel = models.CharField(max_length=20, blank=True, default="")  # empty means any channel
    subject = models.CharField(max_length=300, blank=True, default="")
    body = models.TextField()
    archived_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = TenantManager["MessageTemplate"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "name"],
                condition=Q(archived_at__isnull=True),
                name="uniq_template_name",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=[k.value for k in TemplateKind]), name="template_kind_valid"
            ),
        ]
        ordering = ["name"]
