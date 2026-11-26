"""Page dashboard: one page's health, AI review, suggestions, editing and change history."""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema
from pydantic import ValidationError as PydanticError

from actions import services as action_services
from actions.models import ActionStatus, ProposedAction
from content import alt, checks, review, seo_meta, sync
from content.actions import KIND, MEDIA_KIND, UpdateMediaAlt, UpdatePage
from content.body_action import KIND as BODY_KIND
from content.brief import Brief
from content.models import (
    AnalysisStatus,
    ContentItem,
    ItemSource,
    PageSuggestion,
    SuggestionStatus,
    WebsiteKind,
)
from content.tasks import analyze_page
from core.errors import DomainError, NotFoundError, ValidationFailedError
from identity.authentication import api_auth
from identity.models import User
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["websites"], auth=api_auth)


def _first_error(exc: PydanticError) -> str:
    errors = exc.errors()
    return str(errors[0].get("msg", "invalid value"))[:200] if errors else "invalid value"


HISTORY = 20
TEXT_PREVIEW = 3000
DONE = {ActionStatus.SUCCEEDED, ActionStatus.VERIFIED}


class PgItem(Schema):
    id: uuid.UUID
    kind: str
    title: str
    url: str
    slug: str
    status: str
    language: str
    author: str
    terms: dict[str, Any]
    published_at: datetime | None
    modified_at: datetime | None
    seo: dict[str, Any]
    stats: dict[str, Any]
    outline: list[dict[str, Any]]
    images: list[dict[str, Any]]
    in_sitemap: bool
    http_status: int | None
    text_preview: str


class PgCheck(Schema):
    key: str
    status: str
    weight: int
    values: dict[str, Any]


class PgEditable(Schema):
    fields: list[str]
    reason: str
    seo_plugin: str
    seo_writable: bool | None
    helper_snippet: str


class PgAnalysis(Schema):
    id: uuid.UUID
    status: str
    stale: bool
    instruction: str
    result: dict[str, Any]
    error: str
    finished_at: datetime | None


class PgSuggestion(Schema):
    id: uuid.UUID
    field: str
    current: str
    proposed: str
    reason: str
    impact: str


class PgChange(Schema):
    id: uuid.UUID
    status: str
    summary: str
    requested_by: str
    created_at: datetime
    executed_at: datetime | None
    error: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    can_rollback: bool
    is_rollback: bool


class PgBrief(Schema):
    primary_keyword: str = ""
    secondary_keywords: list[str] = []
    country: str = ""
    region: str = ""
    audience: str = ""
    goal: str = ""


class PgDashboard(Schema):
    item: PgItem
    brief: PgBrief
    site_brief: PgBrief
    score: int
    checks: list[PgCheck]
    editable: PgEditable
    analysis: PgAnalysis | None
    suggestions: list[PgSuggestion]
    changes: list[PgChange]


class PgAnalyzeIn(Schema):
    instruction: str = ""


class PgRewriteIn(Schema):
    field: str
    instruction: str = ""
    current: str | None = None


class PgRewriteOut(Schema):
    options: list[str]
    note: str


class PgApplyIn(Schema):
    changes: dict[str, str]
    suggestion_fields: list[str] = []


class PgAdviceIn(Schema):
    check: str


class PgAdviceOut(Schema):
    why_it_matters: str
    steps: list[str]
    example: str


class PgAltSuggestIn(Schema):
    srcs: list[str] = []


class PgAltSuggestion(Schema):
    src: str
    attachment_id: int | None
    suggested: str
    decorative: bool
    note: str
    seen: bool


class PgAltApplyIn(Schema):
    alts: dict[str, str]


class PgApplied(Schema):
    action_id: uuid.UUID
    status: str


def _item(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, permission: Permission
) -> ContentItem:
    membership = resolve_workspace(request, permission)
    item = (
        ContentItem.objects.select_related("website__connection")
        .filter(pk=item_id, website_id=website_id, workspace_id=membership.workspace_id)
        .first()
    )
    if item is None:
        raise NotFoundError("page not found")
    return item


def _editable(item: ContentItem) -> PgEditable:
    site = item.website
    state = (site.profile or {}).get("seo_meta") or {}
    plugin = str(state.get("plugin", ""))
    if site.kind != WebsiteKind.WORDPRESS or item.source != ItemSource.WORDPRESS:
        reason = "git_site" if site.kind == WebsiteKind.GIT_SITE else "not_a_wordpress_item"
        return PgEditable(
            fields=[], reason=reason, seo_plugin=plugin, seo_writable=None, helper_snippet=""
        )
    if item.removed_at is not None or site.connection is None:
        return PgEditable(
            fields=[], reason="removed", seo_plugin=plugin, seo_writable=None, helper_snippet=""
        )
    writable = state.get("writable") if state else None
    fields = list(seo_meta.CORE_FIELDS)
    if writable:
        fields += list(seo_meta.SEO_FIELDS)
    return PgEditable(
        fields=fields,
        reason="",
        seo_plugin=plugin,
        seo_writable=writable,
        helper_snippet="" if writable else seo_meta.HELPER_SNIPPET,
    )


def _change(action: ProposedAction, rolled_back: set[Any]) -> PgChange:
    return PgChange(
        id=action.pk,
        status=action.status,
        summary=action.summary,
        requested_by=action.requested_by,
        created_at=action.created_at,
        executed_at=action.executed_at,
        error=action.error,
        before=action.before,
        after=action.after,
        can_rollback=(
            action.status in DONE
            and action.rollback_of_id is None
            and action.pk not in rolled_back
            and bool((action.result or {}).get("before"))
        ),
        is_rollback=action.rollback_of_id is not None,
    )


def _supersede(item: ContentItem, kind: str) -> None:
    """A newer change replaces older ones of the same kind that have not started yet."""
    ProposedAction.objects.filter(
        workspace_id=item.workspace_id,
        kind=kind,
        payload__item_id=str(item.pk),
        status__in=[ActionStatus.AWAITING_APPROVAL, ActionStatus.APPROVED],
        attempts=0,
    ).update(status=ActionStatus.EXPIRED, error="superseded")


def _history(item: ContentItem) -> list[PgChange]:
    rows = list(
        ProposedAction.objects.filter(
            workspace_id=item.workspace_id,
            kind__in=[KIND, MEDIA_KIND, BODY_KIND],
            payload__item_id=str(item.pk),
        ).order_by("-created_at")[:HISTORY]
    )
    rolled = {
        r.rollback_of_id
        for r in rows
        if r.rollback_of_id and r.status not in {ActionStatus.REJECTED, ActionStatus.EXPIRED}
    }
    return [_change(a, rolled) for a in rows]


def _dashboard(item: ContentItem) -> PgDashboard:
    results = checks.run_checks(item)
    latest = review.latest_analysis(item)
    suggestions = PageSuggestion.objects.filter(item=item, status=SuggestionStatus.PROPOSED)
    return PgDashboard(
        brief=PgBrief(**(item.brief or {})),
        site_brief=PgBrief(**(item.website.brief or {})),
        item=PgItem(
            id=item.pk,
            kind=item.kind,
            title=item.title,
            url=item.url,
            slug=item.slug,
            status=item.status,
            language=item.language,
            author=item.author,
            terms=item.terms,
            published_at=item.published_at,
            modified_at=item.modified_at,
            seo=item.seo,
            stats=item.stats,
            outline=item.outline,
            images=item.images,
            in_sitemap=item.in_sitemap,
            http_status=item.http_status,
            text_preview=item.text[:TEXT_PREVIEW],
        ),
        score=checks.score_of(results),
        checks=[PgCheck(**row) for row in checks.as_dicts(results)],
        editable=_editable(item),
        analysis=(
            PgAnalysis(
                id=latest.pk,
                status=latest.status,
                stale=bool(latest.content_hash) and latest.content_hash != item.content_hash,
                instruction=latest.instruction,
                result=latest.result,
                error=latest.error,
                finished_at=latest.finished_at,
            )
            if latest
            else None
        ),
        suggestions=[
            PgSuggestion(
                id=s.pk,
                field=s.field,
                current=s.current,
                proposed=s.proposed,
                reason=s.reason,
                impact=s.impact,
            )
            for s in suggestions
        ],
        changes=_history(item),
    )


@router.get(
    "/{website_id}/pages/{item_id}", response=PgDashboard, operation_id="website_page_dashboard"
)
def page_dashboard(request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID) -> PgDashboard:
    return _dashboard(_item(request, website_id, item_id, Permission.VIEW))


@router.post(
    "/{website_id}/pages/{item_id}/analyze",
    response=PgDashboard,
    operation_id="website_page_analyze",
)
def analyze(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, payload: PgAnalyzeIn
) -> PgDashboard:
    item = _item(request, website_id, item_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    running = item.analyses.filter(
        status__in=[AnalysisStatus.QUEUED, AnalysisStatus.RUNNING]
    ).exists()
    if not running:
        analysis = review.start_analysis(
            item, requested_by=f"user:{user.pk}", instruction=payload.instruction
        )
        analyze_page.delay(str(analysis.pk))
    return _dashboard(item)


@router.put(
    "/{website_id}/pages/{item_id}/brief", response=PgDashboard, operation_id="website_page_brief"
)
def save_brief(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, payload: PgBrief
) -> PgDashboard:
    item = _item(request, website_id, item_id, Permission.EDIT_CONTENT)
    try:
        item.brief = Brief(**payload.dict()).cleaned()
    except ValueError as exc:
        raise ValidationFailedError(str(exc)[:300]) from exc
    item.save(update_fields=["brief", "updated_at"])
    return _dashboard(item)


@router.post(
    "/{website_id}/pages/{item_id}/rewrite",
    response=PgRewriteOut,
    operation_id="website_page_rewrite",
)
def rewrite(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, payload: PgRewriteIn
) -> PgRewriteOut:
    item = _item(request, website_id, item_id, Permission.EDIT_CONTENT)
    result = review.rewrite_field(
        item, payload.field, instruction=payload.instruction, current=payload.current
    )
    return PgRewriteOut(options=result.options, note=result.note)


@router.post(
    "/{website_id}/pages/{item_id}/advice",
    response=PgAdviceOut,
    operation_id="website_page_advice",
)
def advice(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, payload: PgAdviceIn
) -> PgAdviceOut:
    item = _item(request, website_id, item_id, Permission.EDIT_CONTENT)
    result = review.check_advice(item, payload.check)
    return PgAdviceOut(
        why_it_matters=result.why_it_matters, steps=result.steps, example=result.example
    )


@router.post(
    "/{website_id}/pages/{item_id}/apply", response=PgApplied, operation_id="website_page_apply"
)
def apply_changes(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, payload: PgApplyIn
) -> PgApplied:
    item = _item(request, website_id, item_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    return propose_changes(item, payload.changes, list(payload.suggestion_fields), user)


def propose_changes(
    item: ContentItem, requested: dict[str, str], suggestion_fields: list[str], user: User
) -> PgApplied:
    """Propose a page change through the action layer and approve it for the signed-in user."""
    editable = _editable(item)
    changes = {k: v for k, v in requested.items() if v != review.current_value(item, k)}
    if not changes or any(field not in editable.fields for field in changes):
        raise ValidationFailedError("these fields cannot be changed on this page")
    try:
        spec = UpdatePage(
            website_id=item.website_id,
            item_id=item.pk,
            changes=changes,
            base_modified_at=(
                item.modified_at.strftime("%Y-%m-%dT%H:%M:%S") if item.modified_at else ""
            ),
            suggestion_fields=suggestion_fields,
        )
    except PydanticError as exc:
        raise ValidationFailedError(_first_error(exc)) from exc
    labels = ", ".join(sorted(changes))
    _supersede(item, KIND)
    action = action_services.propose(
        workspace_id=item.workspace_id,
        kind=KIND,
        payload=spec.model_dump(mode="json"),
        requested_by=f"user:{user.pk}",
        summary=f"Change {labels} of {item.title or item.url}"[:300],
    )
    if action.status == ActionStatus.AWAITING_APPROVAL:
        action = action_services.approve(
            workspace_id=item.workspace_id, action_id=action.pk, user=user
        )
    return PgApplied(action_id=action.pk, status=action.status)


@router.post(
    "/{website_id}/pages/{item_id}/suggestions/{suggestion_id}/dismiss",
    response=PgDashboard,
    operation_id="website_page_dismiss",
)
def dismiss(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, suggestion_id: uuid.UUID
) -> PgDashboard:
    item = _item(request, website_id, item_id, Permission.EDIT_CONTENT)
    suggestion = PageSuggestion.objects.filter(pk=suggestion_id, item=item).first()
    if suggestion is None:
        raise NotFoundError("suggestion not found")
    review.dismiss(suggestion)
    return _dashboard(item)


@router.post(
    "/{website_id}/pages/{item_id}/changes/{action_id}/rollback",
    response=PgApplied,
    operation_id="website_page_rollback",
)
def rollback(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, action_id: uuid.UUID
) -> PgApplied:
    item = _item(request, website_id, item_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    original = ProposedAction.objects.filter(
        pk=action_id,
        workspace_id=item.workspace_id,
        kind__in=[KIND, MEDIA_KIND, BODY_KIND],
        payload__item_id=str(item.pk),
    ).first()
    if original is None:
        raise NotFoundError("change not found")
    action = action_services.request_rollback(
        workspace_id=item.workspace_id, action_id=original.pk, user=user
    )
    if action.status == ActionStatus.AWAITING_APPROVAL:
        action = action_services.approve(
            workspace_id=item.workspace_id, action_id=action.pk, user=user
        )
    return PgApplied(action_id=action.pk, status=action.status)


@router.post(
    "/{website_id}/pages/{item_id}/refresh",
    response=PgDashboard,
    operation_id="website_page_refresh",
)
def refresh(request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID) -> PgDashboard:
    """Read this one page from WordPress again (content, headings, images)."""
    item = _item(request, website_id, item_id, Permission.EDIT_CONTENT)
    try:
        sync.refresh_item(item)
    except DomainError as exc:
        raise ValidationFailedError(str(exc)) from exc
    return _dashboard(item)


@router.post(
    "/{website_id}/pages/{item_id}/alt/suggest",
    response=list[PgAltSuggestion],
    operation_id="website_page_alt_suggest",
)
def alt_suggest(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, payload: PgAltSuggestIn
) -> list[PgAltSuggestion]:
    item = _item(request, website_id, item_id, Permission.EDIT_CONTENT)
    found = alt.suggest(item, payload.srcs or None)
    return [PgAltSuggestion(**row) for row in found]


@router.post(
    "/{website_id}/pages/{item_id}/alt/apply",
    response=PgApplied,
    operation_id="website_page_alt_apply",
)
def alt_apply(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, payload: PgAltApplyIn
) -> PgApplied:
    item = _item(request, website_id, item_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    if _editable(item).reason:
        raise ValidationFailedError("alt text cannot be changed on this page")
    known = {str(i.get("attachment_id")) for i in item.images if i.get("attachment_id")}
    alts = {k: v for k, v in payload.alts.items() if k in known and v.strip()}
    if not alts:
        raise ValidationFailedError("no image of this page can be changed")
    try:
        spec = UpdateMediaAlt(website_id=website_id, item_id=item.pk, alts=alts)
    except PydanticError as exc:
        raise ValidationFailedError(_first_error(exc)) from exc
    _supersede(item, MEDIA_KIND)
    action = action_services.propose(
        workspace_id=item.workspace_id,
        kind=MEDIA_KIND,
        payload=spec.model_dump(mode="json"),
        requested_by=f"user:{user.pk}",
        summary=f"Alt text of {len(alts)} image(s) on {item.title or item.url}"[:300],
    )
    if action.status == ActionStatus.AWAITING_APPROVAL:
        action = action_services.approve(
            workspace_id=item.workspace_id, action_id=action.pk, user=user
        )
    return PgApplied(action_id=action.pk, status=action.status)
