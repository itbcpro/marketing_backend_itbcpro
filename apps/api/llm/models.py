"""LLM usage ledger: one row per model call, used for cost dashboards and budgets."""

from django.db import models

from core.models import BaseModel


class LlmCall(BaseModel):
    workspace_id = models.UUIDField(null=True, blank=True, db_index=True)
    run_id = models.UUIDField(null=True, blank=True, db_index=True)
    tier = models.CharField(max_length=40)
    model = models.CharField(max_length=100)
    prompt_key = models.CharField(max_length=200, blank=True, default="")
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_read_tokens = models.PositiveIntegerField(default=0)
    cache_write_tokens = models.PositiveIntegerField(default=0)
    cost_micros = models.BigIntegerField(default=0)
    cost_known = models.BooleanField(default=True)
    latency_ms = models.PositiveIntegerField(default=0)
    stop_reason = models.CharField(max_length=40, blank=True, default="")
    request_id = models.CharField(max_length=100, blank=True, default="")
    error_code = models.CharField(max_length=60, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["workspace_id", "created_at"], name="llmcall_ws_time_idx")]

    def __str__(self) -> str:
        return f"{self.model} {self.cost_micros}µ$"
