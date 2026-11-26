"""OpenAI Ads screens: account tree, insights, billing and proposed changes."""

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Schema, Status

from actions.registry import UnknownActionKindError, get_action_kind
from ads import providers
from ads.models import AssistSession
from ads.openai import actions, assist, changes, launcher, sessions
from ads.openai.client import PROVIDER, OpenAiAdsClient
from connections.models import Connection, ConnectionStatus
from core.errors import DomainError, NotFoundError, ValidationFailedError
from identity.authentication import api_auth
from identity.models import User
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["ads-openai"], auth=api_auth)

LEVELS = {"account", "campaign", "ad_group", "ad"}
SEGMENTS = {"country", "device", "platform"}
GRANULARITIES = {"hourly", "daily", "monthly", "none"}
MAX_RANGE_DAYS = 365


class OverviewOut(Schema):
    connection_id: uuid.UUID
    account: dict[str, Any]
    campaigns: list[dict[str, Any]]


class InsightsOut(Schema):
    currency: str
    rows: list[dict[str, Any]]


class BillingOut(Schema):
    account: dict[str, Any]
    spend_limits: dict[str, Any] | None
    spend_limits_available: bool
    month_to_date: float
    last_30_days: float
    today: float
    currency: str


class InsightsQuery(Schema):
    connection_id: uuid.UUID
    level: str = "account"
    entity_id: str | None = None
    date_from: date
    date_to: date
    granularity: str = "daily"
    segment: str | None = None


class ChangeIn(Schema):
    connection_id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    summary: str = ""
    apply_now: bool = False
    wait: bool = False


class ChangeOut(Schema):
    action_id: uuid.UUID
    status: str
    risk: str
    summary: str
    applied: bool
    result: dict[str, Any] | None = None
    error: str = ""


def _connection(
    request: HttpRequest, connection_id: uuid.UUID, permission: Permission
) -> Connection:
    membership = resolve_workspace(request, permission)
    connection = (
        Connection.objects.filter(
            pk=connection_id, workspace_id=membership.workspace_id, provider=PROVIDER
        )
        .exclude(status=ConnectionStatus.REVOKED)
        .first()
    )
    if connection is None:
        raise NotFoundError("OpenAI Ads connection not found")
    return connection


def _client(connection: Connection) -> OpenAiAdsClient:
    return actions.client_factory(connection)


@router.get("/overview", response=OverviewOut, operation_id="openai_ads_overview")
def overview(request: HttpRequest, connection_id: uuid.UUID) -> OverviewOut:
    connection = _connection(request, connection_id, Permission.VIEW)
    with _client(connection) as client:
        account = client.get_account()
        campaigns = client.list_resource("campaigns")
        for campaign in campaigns:
            groups = client.list_resource("ad_groups", parent=("campaign_id", campaign["id"]))
            for group in groups:
                group["ads"] = client.list_resource("ads", parent=("ad_group_id", group["id"]))
            campaign["ad_groups"] = groups
    return OverviewOut(connection_id=connection.pk, account=account, campaigns=campaigns)


@router.get("/insights", response=InsightsOut, operation_id="openai_ads_insights_report")
def insights(request: HttpRequest, query: Query[InsightsQuery]) -> InsightsOut:
    connection = _connection(request, query.connection_id, Permission.VIEW)
    if query.level not in LEVELS or query.granularity not in GRANULARITIES:
        raise ValidationFailedError("unsupported level or granularity")
    if query.segment is not None and query.segment not in SEGMENTS:
        raise ValidationFailedError("unsupported segment")
    if query.level != "account" and not query.entity_id:
        raise ValidationFailedError("entity_id is required below account level")
    span = (query.date_to - query.date_from).days
    if span < 0 or span > MAX_RANGE_DAYS:
        raise ValidationFailedError("date range must be between 0 and 365 days")
    with _client(connection) as client:
        currency = str(client.get_account().get("currency_code", ""))
        rows = client.insights(
            query.level,
            query.entity_id,
            date_from=query.date_from.isoformat(),
            date_to=query.date_to.isoformat(),
            granularity=query.granularity,
            segment=query.segment,
        )
    return InsightsOut(currency=currency, rows=rows)


def _spend(rows: list[dict[str, Any]]) -> float:
    return round(sum(float(row.get("spend") or 0) for row in rows), 2)


@router.get("/billing", response=BillingOut, operation_id="openai_ads_billing")
def billing(request: HttpRequest, connection_id: uuid.UUID) -> BillingOut:
    """Spend and spending limits. The API exposes no invoices or payment methods."""
    connection = _connection(request, connection_id, Permission.VIEW)
    today = timezone.now().date()
    with _client(connection) as client:
        account = client.get_account()
        try:
            limits: dict[str, Any] | None = client.spend_limits()
        except DomainError:
            limits = None  # needs billing permission on the key, or a prepaid account

        def spend(since: date, until: date) -> float:
            rows = client.insights(
                "account",
                None,
                date_from=since.isoformat(),
                date_to=until.isoformat(),
                granularity="none",
            )
            return _spend(rows)

        month_to_date = spend(today.replace(day=1), today)
        last_30 = spend(today - timedelta(days=30), today)
        today_spend = spend(today, today)
    return BillingOut(
        account=account,
        spend_limits=limits,
        spend_limits_available=limits is not None,
        month_to_date=month_to_date,
        last_30_days=last_30,
        today=today_spend,
        currency=str(account.get("currency_code", "")),
    )


@router.post("/changes", response=ChangeOut, operation_id="openai_ads_propose_change")
def propose_change(request: HttpRequest, body: ChangeIn) -> ChangeOut:
    """Turn an edit into a proposed action; approvers may apply it immediately."""
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    found = (
        Connection.objects.filter(
            pk=body.connection_id,
            workspace_id=membership.workspace_id,
            provider__in=list(providers.PROVIDERS),
        )
        .exclude(status=ConnectionStatus.REVOKED)
        .first()
    )
    if found is None:
        raise NotFoundError("ad account connection not found")
    connection = found
    user: User = request.auth  # type: ignore[attr-defined]
    kind = providers.for_connection(connection).action_prefix + body.kind
    try:
        get_action_kind(kind)
    except UnknownActionKindError as exc:
        raise ValidationFailedError("unknown change") from exc
    action, applied = changes.submit(
        connection,
        user,
        body.kind,
        body.payload,
        summary=body.summary,
        apply_now=body.apply_now,
        wait=body.wait,
    )
    return ChangeOut(
        action_id=action.pk,
        status=action.status,
        risk=action.risk,
        summary=action.summary,
        applied=applied,
        result=action.result,
        error=str(getattr(action, "error", "") or "")[:400],
    )


class AssistAnswerIn(Schema):
    question: str
    answer: str


class AssistSessionCreateIn(Schema):
    connection_id: uuid.UUID
    goal: str
    image_url: str = ""


class AssistAnswersIn(Schema):
    answers: list[AssistAnswerIn]


class AssistFeedbackIn(Schema):
    feedback: str


class AssistLaunchIn(Schema):
    image_url: str = ""


class AssistSessionSummaryOut(Schema):
    id: uuid.UUID
    title: str
    status: str
    updated_at: datetime


class AssistSessionOut(Schema):
    id: uuid.UUID
    connection_id: uuid.UUID
    title: str
    goal: str
    image_url: str
    status: str
    history: list[dict[str, Any]]
    questions: list[str]
    draft: assist.Draft | None
    error_code: str
    thinking_since: datetime | None
    run_id: uuid.UUID | None
    updated_at: datetime


def _session_out(session: AssistSession) -> AssistSessionOut:
    return AssistSessionOut(
        id=session.pk,
        connection_id=session.connection_id,
        title=session.title,
        goal=session.goal,
        image_url=session.image_url,
        status=session.status,
        history=session.history,
        questions=list(session.questions),
        draft=assist.Draft.model_validate(session.draft) if session.draft else None,
        error_code=session.error_code,
        thinking_since=session.thinking_since,
        run_id=session.run_id,
        updated_at=session.updated_at,
    )


def _session(request: HttpRequest, session_id: uuid.UUID) -> AssistSession:
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    return sessions.get(membership.workspace_id, session_id)


@router.get(
    "/assist/sessions",
    response=list[AssistSessionSummaryOut],
    operation_id="openai_ads_assist_sessions",
)
def assist_sessions(
    request: HttpRequest, connection_id: uuid.UUID
) -> list[AssistSessionSummaryOut]:
    """Saved conversations for this ad account, newest first."""
    connection = _connection(request, connection_id, Permission.EDIT_CONTENT)
    rows = sessions.recent(connection.workspace_id, connection.pk)
    return [
        AssistSessionSummaryOut(id=r.pk, title=r.title, status=r.status, updated_at=r.updated_at)
        for r in rows
    ]


@router.post("/assist/sessions", response=AssistSessionOut, operation_id="openai_ads_assist_start")
def assist_start(request: HttpRequest, body: AssistSessionCreateIn) -> AssistSessionOut:
    """Start a saved conversation; Claude drafts in the background."""
    connection = _connection(request, body.connection_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    image_url = body.image_url.strip()
    if image_url and not image_url.startswith("https://"):
        raise ValidationFailedError("the image link must start with https://")
    session = sessions.create(
        connection.workspace, connection, user.pk, goal=body.goal, image_url=image_url
    )
    return _session_out(session)


@router.get(
    "/assist/sessions/{session_id}", response=AssistSessionOut, operation_id="openai_ads_assist_get"
)
def assist_get(request: HttpRequest, session_id: uuid.UUID) -> AssistSessionOut:
    return _session_out(_session(request, session_id))


@router.post(
    "/assist/sessions/{session_id}/answers",
    response=AssistSessionOut,
    operation_id="openai_ads_assist_answer",
)
def assist_answer(
    request: HttpRequest, session_id: uuid.UUID, body: AssistAnswersIn
) -> AssistSessionOut:
    session = _session(request, session_id)
    pairs = [(a.question, a.answer) for a in body.answers]
    return _session_out(sessions.answer(session, pairs))


@router.post(
    "/assist/sessions/{session_id}/feedback",
    response=AssistSessionOut,
    operation_id="openai_ads_assist_feedback",
)
def assist_feedback(
    request: HttpRequest, session_id: uuid.UUID, body: AssistFeedbackIn
) -> AssistSessionOut:
    return _session_out(sessions.refine(_session(request, session_id), body.feedback))


@router.post(
    "/assist/sessions/{session_id}/retry",
    response=AssistSessionOut,
    operation_id="openai_ads_assist_retry",
)
def assist_retry(request: HttpRequest, session_id: uuid.UUID) -> AssistSessionOut:
    return _session_out(sessions.retry(_session(request, session_id)))


@router.post(
    "/assist/sessions/{session_id}/launch",
    response=AssistSessionOut,
    operation_id="openai_ads_assist_launch",
)
def assist_launch(
    request: HttpRequest, session_id: uuid.UUID, body: AssistLaunchIn
) -> AssistSessionOut:
    """Create the approved draft step by step in the background (all paused); resumes if stopped."""
    session = _session(request, session_id)
    user: User = request.auth  # type: ignore[attr-defined]
    image = body.image_url.strip() or session.image_url
    return _session_out(launcher.begin(session, user, image))


@router.delete(
    "/assist/sessions/{session_id}", response={204: None}, operation_id="openai_ads_assist_delete"
)
def assist_delete(request: HttpRequest, session_id: uuid.UUID) -> Status[None]:
    sessions.archive(_session(request, session_id))
    return Status(204, None)
