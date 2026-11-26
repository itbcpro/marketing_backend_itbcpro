"""Agents as data: definitions, runs and the step log that makes every run replayable."""

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class AgentDefinition(TenantModel):
    name = models.SlugField(max_length=60)
    title = models.CharField(max_length=120)
    prompt_key = models.CharField(max_length=200)
    tier = models.CharField(max_length=40, default="planner")
    tools = models.JSONField(default=list)
    channel = models.CharField(max_length=40, blank=True, default="")
    max_steps = models.PositiveSmallIntegerField(default=12)
    max_cost_micros = models.BigIntegerField(default=2_000_000)
    max_wall_seconds = models.PositiveIntegerField(default=600)
    enabled = models.BooleanField(default=True)

    objects = TenantManager["AgentDefinition"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "name"], name="uniq_agent_name"),
        ]

    def __str__(self) -> str:
        return self.name


class RunStatus(models.TextChoices):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"


TERMINAL_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.BUDGET_EXHAUSTED}
)


class AgentRun(TenantModel):
    agent = models.ForeignKey(AgentDefinition, on_delete=models.PROTECT, related_name="runs")
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    depth = models.PositiveSmallIntegerField(default=0)
    goal = models.TextField()
    requested_by = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=RunStatus.choices, default=RunStatus.QUEUED)
    tainted = models.BooleanField(default=False)
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    steps_count = models.PositiveSmallIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost_micros = models.BigIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["AgentRun"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in RunStatus]), name="run_status_valid"
            ),
        ]
        indexes = [models.Index(fields=["workspace", "status", "created_at"], name="run_list_idx")]

    def __str__(self) -> str:
        return f"{self.agent_id}:{self.status}"


class AgentStep(TenantModel):
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="steps")
    index = models.PositiveSmallIntegerField()
    kind = models.CharField(max_length=10)  # goal | llm | tool
    payload = models.JSONField()
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost_micros = models.BigIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)

    objects = TenantManager["AgentStep"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "index"], name="uniq_step_index"),
        ]
        ordering = ["index"]


class CommandSession(TenantModel):
    """One request typed into the command box, from interpretation to the run it started."""

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    text = models.TextField()
    brief = models.JSONField(default=dict)
    answers = models.JSONField(default=list)
    status = models.CharField(max_length=20, default="needs_input")
    run = models.ForeignKey(
        AgentRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = TenantManager["CommandSession"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["needs_input", "ready", "running", "cancelled"]),
                name="command_status_valid",
            ),
        ]
