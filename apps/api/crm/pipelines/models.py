"""Pipelines of stages, and the deals (opportunities) that move through them."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class Pipeline(TenantModel):
    name = models.CharField(max_length=100)
    archived_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["Pipeline"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "name"],
                condition=Q(archived_at__isnull=True),
                name="uniq_pipeline_name",
            )
        ]
        ordering = ["created_at"]


class Stage(TenantModel):
    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name="stages")
    name = models.CharField(max_length=100)
    position = models.PositiveIntegerField(default=0)
    probability = models.PositiveSmallIntegerField(
        default=0, help_text="Percent chance of winning."
    )
    rotting_days = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Days without movement before a deal counts as stale; empty uses the default.",
    )
    required_fields = models.JSONField(
        default=list,
        help_text="Deal requirements (see crm.pipelines.rules) that must hold to enter the stage.",
    )

    objects = TenantManager["Stage"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(probability__lte=100), name="stage_probability_percent"
            ),
            models.UniqueConstraint(fields=["pipeline", "name"], name="uniq_stage_name"),
        ]
        ordering = ["position", "created_at"]


class LostReason(TenantModel):
    name = models.CharField(max_length=100)
    archived_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["LostReason"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "name"],
                condition=Q(archived_at__isnull=True),
                name="uniq_lost_reason_name",
            )
        ]
        ordering = ["name"]


class OpportunityStatus(models.TextChoices):
    OPEN = "open"
    WON = "won"
    LOST = "lost"
    ABANDONED = "abandoned"


class DealPriority(models.TextChoices):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class Opportunity(TenantModel):
    """A deal. Its status is independent of its stage: a lost deal keeps the stage it died in."""

    contact = models.ForeignKey(
        "crm_contacts.Contact", on_delete=models.CASCADE, related_name="opportunities"
    )
    pipeline = models.ForeignKey(Pipeline, on_delete=models.PROTECT, related_name="opportunities")
    stage = models.ForeignKey(Stage, on_delete=models.PROTECT, related_name="opportunities")
    name = models.CharField(max_length=200)
    status = models.CharField(
        max_length=10, choices=OpportunityStatus.choices, default=OpportunityStatus.OPEN
    )
    value_micros = models.BigIntegerField(default=0)
    currency = models.CharField(max_length=3)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    source = models.CharField(max_length=100, blank=True, default="")
    lost_reason = models.ForeignKey(
        LostReason, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    expected_close_date = models.DateField(null=True, blank=True)
    priority = models.CharField(
        max_length=10, choices=DealPriority.choices, default=DealPriority.NORMAL
    )
    tags = models.JSONField(default=list)
    probability_override = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Percent chance for this deal; empty uses its stage."
    )
    last_stage_change_at = models.DateTimeField()
    last_status_change_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["Opportunity"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in OpportunityStatus]),
                name="opportunity_status_valid",
            ),
            models.CheckConstraint(condition=Q(value_micros__gte=0), name="opportunity_value_ok"),
            models.CheckConstraint(
                condition=Q(priority__in=[p.value for p in DealPriority]),
                name="opportunity_priority_valid",
            ),
            models.CheckConstraint(
                condition=Q(probability_override__isnull=True) | Q(probability_override__lte=100),
                name="opportunity_probability_percent",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "pipeline", "stage", "status"], name="opp_board_idx"),
            models.Index(fields=["workspace", "assigned_to", "status"], name="opp_owner_idx"),
            models.Index(fields=["workspace", "last_stage_change_at"], name="opp_stale_idx"),
        ]


class StageChange(TenantModel):
    """Every move of a deal between stages, kept for conversion and time-in-stage reports."""

    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name="history")
    from_stage = models.ForeignKey(Stage, null=True, on_delete=models.SET_NULL, related_name="+")
    to_stage = models.ForeignKey(Stage, null=True, on_delete=models.SET_NULL, related_name="+")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = TenantManager["StageChange"]()

    class Meta:
        indexes = [models.Index(fields=["opportunity", "created_at"], name="stage_change_idx")]
        ordering = ["created_at", "id"]


class PaymentKind(models.TextChoices):
    PAYMENT = "payment"
    REFUND = "refund"


class PaymentStatus(models.TextChoices):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"


class PaymentMethod(models.TextChoices):
    CASH = "cash"
    BANK_TRANSFER = "bank_transfer"
    CARD = "card"
    CHEQUE = "cheque"
    OTHER = "other"


class Payment(TenantModel):
    """Money received for a deal, or given back; recorded by hand, never charged here."""

    contact = models.ForeignKey(
        "crm_contacts.Contact", on_delete=models.CASCADE, related_name="payments"
    )
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name="payments")
    kind = models.CharField(max_length=10, choices=PaymentKind.choices)
    status = models.CharField(
        max_length=10, choices=PaymentStatus.choices, default=PaymentStatus.PAID
    )
    method = models.CharField(
        max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.OTHER
    )
    amount_micros = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    reference = models.CharField(max_length=100, blank=True, default="")
    note = models.TextField(blank=True, default="")
    occurred_at = models.DateTimeField()
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = TenantManager["Payment"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=[k.value for k in PaymentKind]), name="payment_kind_valid"
            ),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in PaymentStatus]),
                name="payment_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(method__in=[m.value for m in PaymentMethod]),
                name="payment_method_valid",
            ),
            models.CheckConstraint(
                condition=Q(amount_micros__gt=0), name="payment_amount_positive"
            ),
        ]
        indexes = [
            models.Index(fields=["opportunity", "-occurred_at"], name="payment_deal_idx"),
            models.Index(fields=["contact", "-occurred_at"], name="payment_contact_idx"),
        ]
