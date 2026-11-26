"""Proposed actions: every external change passes through this table."""

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class ActionStatus(models.TextChoices):
    POLICY_BLOCKED = "policy_blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    VERIFIED = "verified"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"
    EXPIRED = "expired"


class RiskClass(models.TextChoices):
    """R0 read-only, R1 reversible and free, R2 reversible with cost or visibility, R3 financial
    or irreversible."""

    R0 = "r0"
    R1 = "r1"
    R2 = "r2"
    R3 = "r3"


class ProposedAction(TenantModel):
    kind = models.CharField(max_length=100, db_index=True)
    summary = models.CharField(max_length=300, blank=True, default="")
    payload = models.JSONField(default=dict)
    risk = models.CharField(max_length=2, choices=RiskClass.choices)
    status = models.CharField(
        max_length=20, choices=ActionStatus.choices, default=ActionStatus.AWAITING_APPROVAL
    )
    requested_by = models.CharField(max_length=100)
    run_id = models.UUIDField(null=True, blank=True, db_index=True)
    tainted = models.BooleanField(default=False)
    auto_approved = models.BooleanField(default=False)
    idempotency_key = models.CharField(max_length=200)
    spend_micros = models.BigIntegerField(default=0)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    violations = models.JSONField(default=list)
    expires_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.CharField(max_length=40, blank=True, default="")
    decision_note = models.TextField(blank=True, default="")
    edited_payload = models.JSONField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    rollback_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="rollbacks"
    )

    objects = TenantManager["ProposedAction"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "idempotency_key"], name="uniq_action_idempotency"
            ),
            models.CheckConstraint(
                condition=Q(risk__in=["r0", "r1", "r2", "r3"]), name="action_risk_valid"
            ),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in ActionStatus]), name="action_status_valid"
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "status", "created_at"], name="action_inbox_idx"),
        ]

    @property
    def effective_payload(self) -> dict[str, object]:
        """What executors must use: the human's edited version when there is one."""
        payload: dict[str, object] = (
            self.edited_payload if self.edited_payload is not None else self.payload
        )
        return payload

    def __str__(self) -> str:
        return f"{self.kind} [{self.status}]"


class ExecutionAttempt(TenantModel):
    action = models.ForeignKey(ProposedAction, on_delete=models.CASCADE, related_name="attempt_log")
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    ok = models.BooleanField(default=False)
    error_class = models.CharField(max_length=100, blank=True, default="")
    detail = models.JSONField(default=dict)

    objects = TenantManager["ExecutionAttempt"]()


class AutonomyGrant(TenantModel):
    """How much an agent may do alone for one action kind. Levels: 1 suggest, 2 prepare,
    3 execute low risk, 4 execute within guardrails. Absence means level 2."""

    agent = models.CharField(max_length=100)
    action_kind = models.CharField(max_length=100)
    level = models.PositiveSmallIntegerField(default=2)
    limits = models.JSONField(default=dict)
    expires_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["AutonomyGrant"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "agent", "action_kind"], name="uniq_autonomy_grant"
            ),
            models.CheckConstraint(condition=Q(level__gte=1, level__lte=4), name="autonomy_level"),
        ]


class Guardrail(TenantModel):
    """Workspace policy value: blocked terms, spend caps and similar limits."""

    key = models.CharField(max_length=60)
    config = models.JSONField(default=dict)
    enabled = models.BooleanField(default=True)

    objects = TenantManager["Guardrail"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "key"], name="uniq_guardrail_key"),
        ]
