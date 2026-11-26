"""LLM facade: tier routing, budget enforcement and the usage ledger around a gateway."""

import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from core.errors import DomainError, ErrorCode
from core.logging import get_logger
from llm.models import LlmCall
from llm.ports import LlmGateway, LlmRequest, LlmResponse
from llm.pricing import cost_micros

log = get_logger(__name__)


class BudgetExceededError(DomainError):
    code = ErrorCode.BUDGET_EXCEEDED
    http_status = 402


def model_for(tier: str) -> str:
    try:
        return str(settings.MODEL_ROUTING[tier])
    except KeyError as exc:
        raise ValueError(f"unknown model tier: {tier}") from exc


def effort_for(tier: str) -> str | None:
    return settings.EFFORT_BY_TIER.get(tier)


def spent_today(workspace_id: uuid.UUID) -> int:
    since = timezone.now() - timedelta(hours=24)
    total = LlmCall.objects.filter(workspace_id=workspace_id, created_at__gte=since).aggregate(
        total=Sum("cost_micros")
    )["total"]
    return int(total or 0)


class LlmService:
    def __init__(self, gateway: LlmGateway) -> None:
        self._gateway = gateway

    def _check_budget(self, workspace_id: uuid.UUID | None) -> None:
        limit = settings.LLM_DAILY_BUDGET_MICROS
        if workspace_id and limit > 0 and spent_today(workspace_id) >= limit:
            raise BudgetExceededError("daily LLM budget reached")

    def complete(
        self,
        request: LlmRequest,
        *,
        workspace_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        prompt_key: str = "",
    ) -> LlmResponse:
        self._check_budget(workspace_id)
        model = model_for(request.tier)
        if request.effort is None:
            request.effort = effort_for(request.tier)
        started = time.perf_counter()
        try:
            response = self._gateway.complete(request, model=model)
        except DomainError as exc:
            log.warning(
                "llm_call_failed",
                code=exc.code.value,
                model=model,
                prompt_key=prompt_key,
                detail=str(exc)[:500],
            )
            self._record(
                request, model, None, started, workspace_id, run_id, prompt_key, exc.code.value
            )
            raise
        self._record(request, model, response, started, workspace_id, run_id, prompt_key, "")
        return response

    @staticmethod
    def _record(
        request: LlmRequest,
        model: str,
        response: LlmResponse | None,
        started: float,
        workspace_id: uuid.UUID | None,
        run_id: uuid.UUID | None,
        prompt_key: str,
        error_code: str,
    ) -> None:
        cost, known = (0, True)
        fields: dict[str, object] = {}
        if response is not None:
            cost, known = cost_micros(response.model or model, response.usage)
            fields = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_tokens": response.usage.cache_read_tokens,
                "cache_write_tokens": response.usage.cache_write_tokens,
                "stop_reason": response.stop_reason,
                "request_id": response.request_id,
            }
        LlmCall.objects.create(
            workspace_id=workspace_id,
            run_id=run_id,
            tier=request.tier,
            model=(response.model if response else model),
            prompt_key=prompt_key,
            cost_micros=cost,
            cost_known=known,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_code=error_code,
            **fields,
        )


_service: LlmService | None = None
_overridden = False


def workspace_api_credentials(workspace_id: uuid.UUID) -> tuple[str, str] | None:
    """``(api key, Anthropic workspace id)`` from the workspace's live Claude connection."""
    from connections.models import Connection, ConnectionStatus, CredentialKind
    from connections.services import read_secret
    from core.errors import NotFoundError

    connection = (
        Connection.objects.filter(workspace_id=workspace_id, provider="anthropic")
        .exclude(status__in=[ConnectionStatus.REVOKED, ConnectionStatus.PENDING])
        .order_by("created_at")
        .first()
    )
    if connection is None:
        return None
    from llm.provider import workspace_of

    try:
        return read_secret(connection, CredentialKind.API_KEY), workspace_of(connection)
    except NotFoundError:
        return None


def workspace_api_key(workspace_id: uuid.UUID) -> str | None:
    """The workspace's own Anthropic key from its Claude connection, when it has a live one."""
    credentials = workspace_api_credentials(workspace_id)
    return credentials[0] if credentials else None


def get_llm_service(workspace_id: uuid.UUID | None = None) -> LlmService:
    """Service for a workspace: its own Claude connection first, then the platform key."""
    global _service  # noqa: PLW0603
    if _overridden and _service is not None:
        return _service
    from llm.anthropic_adapter import AnthropicGateway

    if workspace_id is not None and (credentials := workspace_api_credentials(workspace_id)):
        return LlmService(AnthropicGateway(api_key=credentials[0], workspace_id=credentials[1]))
    if _service is None:
        _service = LlmService(AnthropicGateway(api_key=settings.ANTHROPIC_API_KEY or None))
    return _service


def set_llm_service(service: LlmService | None) -> None:
    """Override the service (tests, alternative gateways)."""
    global _service, _overridden  # noqa: PLW0603
    _service = service
    _overridden = service is not None
