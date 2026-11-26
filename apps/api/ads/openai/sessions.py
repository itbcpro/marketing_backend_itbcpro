"""Saved assistant conversations: every turn is stored, and drafting runs as a background job."""

import uuid
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from ads.models import AssistSession, AssistStatus
from ads.openai import assist
from connections.models import Connection
from core.errors import ConflictError, DomainError, ErrorCode, NotFoundError, ValidationFailedError
from core.logging import get_logger
from llm.service import get_llm_service
from workspaces.models import Workspace

log = get_logger(__name__)

TITLE_CHARS = 120
MAX_TEXT_CHARS = 4000
MAX_ANSWERS = 10
STUCK_AFTER = timedelta(minutes=15)
LIST_LIMIT = 30


def _event(kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, "at": timezone.now().isoformat(), **fields}


def _clip(text: str) -> str:
    cleaned = text.strip()
    if not cleaned or len(cleaned) > MAX_TEXT_CHARS:
        raise ValidationFailedError(f"text must be 1 to {MAX_TEXT_CHARS} characters")
    return cleaned


def _enqueue(session: AssistSession) -> None:
    from ads.tasks import assist_turn_task

    transaction.on_commit(lambda: assist_turn_task.delay(str(session.pk)))


def _begin_turn(session: AssistSession, event: dict[str, Any]) -> AssistSession:
    session.history = [*session.history, event]
    session.status = AssistStatus.THINKING
    session.error_code = ""
    session.thinking_since = timezone.now()
    session.save()
    _enqueue(session)
    return session


def create(
    workspace: Workspace, connection: Connection, user_id: uuid.UUID, *, goal: str, image_url: str
) -> AssistSession:
    goal = _clip(goal)
    session = AssistSession.objects.create(
        workspace=workspace,
        connection=connection,
        created_by_id=user_id,
        title=goal.splitlines()[0][:TITLE_CHARS],
        goal=goal,
        image_url=image_url,
        history=[],
    )
    return _begin_turn(session, _event("goal", text=goal))


def get(workspace_id: uuid.UUID, session_id: uuid.UUID) -> AssistSession:
    session = AssistSession.objects.filter(pk=session_id, workspace_id=workspace_id).first()
    if session is None or session.status == AssistStatus.ARCHIVED:
        raise NotFoundError("conversation not found")
    if (
        session.status == AssistStatus.THINKING
        and session.thinking_since
        and timezone.now() - session.thinking_since > STUCK_AFTER
    ):
        _fail(session, ErrorCode.PROVIDER_UNAVAILABLE.value)
    return session


def recent(workspace_id: uuid.UUID, connection_id: uuid.UUID) -> list[AssistSession]:
    rows = AssistSession.objects.filter(
        workspace_id=workspace_id, connection_id=connection_id
    ).exclude(status=AssistStatus.ARCHIVED)
    return list(rows[:LIST_LIMIT])


def answer(session: AssistSession, answers: list[tuple[str, str]]) -> AssistSession:
    if session.status != AssistStatus.NEEDS_INPUT:
        raise ConflictError("this conversation is not waiting for answers")
    items = [(q, _clip(a)) for q, a in answers[:MAX_ANSWERS] if a.strip()]
    if not items:
        raise ValidationFailedError("answer at least one question")
    event = _event("answers", items=[{"question": q, "answer": a} for q, a in items])
    session.questions = []
    return _begin_turn(session, event)


def refine(session: AssistSession, feedback: str) -> AssistSession:
    if session.status != AssistStatus.DRAFT:
        raise ConflictError("there is no draft to change yet")
    return _begin_turn(session, _event("feedback", text=_clip(feedback)))


def retry(session: AssistSession) -> AssistSession:
    if session.status != AssistStatus.FAILED:
        raise ConflictError("only a failed turn can be retried")
    session.history = [*session.history, _event("retry")]
    session.status = AssistStatus.THINKING
    session.error_code = ""
    session.thinking_since = timezone.now()
    session.save()
    _enqueue(session)
    return session


def archive(session: AssistSession) -> None:
    session.status = AssistStatus.ARCHIVED
    session.save(update_fields=["status", "updated_at"])


def launch(session: AssistSession, user_id: uuid.UUID, image_url: str) -> AssistSession:
    if session.status != AssistStatus.DRAFT or not session.draft:
        raise ConflictError("there is no draft to launch")
    draft = assist.Draft.model_validate(session.draft)
    run = assist.launch(session.workspace, session.connection, user_id, draft, image_url)
    session.status = AssistStatus.LAUNCHED
    session.image_url = image_url
    session.run_id = run.pk
    session.history = [*session.history, _event("launched", run_id=str(run.pk))]
    session.save()
    return session


# --- background turn -------------------------------------------------------------------------


def compose_goal(session: AssistSession) -> str:
    """The original goal plus every answer given so far, in the order they were given."""
    lines = [session.goal]
    answered = [e for e in session.history if e["type"] == "answers"]
    if answered:
        lines.append("\nAnswers to your questions:")
        lines.extend(
            f"- {item['question']} {item['answer']}" for e in answered for item in e["items"]
        )
    return "\n".join(lines)


def _fail(session: AssistSession, code: str) -> None:
    session.status = AssistStatus.FAILED
    session.error_code = code
    session.thinking_since = None
    session.history = [*session.history, _event("error", code=code)]
    session.save()


def run_turn(session_id: uuid.UUID) -> str:
    """Produce the assistant's next message. Safe to call twice: only a thinking turn runs."""
    session = (
        AssistSession.objects.select_related("workspace", "connection")
        .filter(pk=session_id)
        .first()
    )
    if session is None or session.status != AssistStatus.THINKING:
        return "skipped"
    last = session.history[-1] if session.history else {}
    feedback = str(last.get("text", "")) if last.get("type") == "feedback" else ""
    previous = assist.Draft.model_validate(session.draft) if session.draft and feedback else None
    try:
        result = assist.suggest(
            session.workspace,
            session.connection,
            get_llm_service(session.workspace_id),
            goal=compose_goal(session),
            previous=previous,
            feedback=feedback,
        )
    except DomainError as exc:
        log.warning("assist_turn_failed", code=exc.code.value)
        _fail(session, exc.code.value)
        return "failed"
    except Exception:
        log.exception("assist_turn_crashed")
        _fail(session, ErrorCode.UNKNOWN.value)
        return "failed"

    session.thinking_since = None
    if result.status == "draft" and result.draft is not None:
        session.status = AssistStatus.DRAFT
        session.draft = result.draft.model_dump(mode="json")
        session.questions = []
        session.history = [*session.history, _event("draft", draft=session.draft)]
    else:
        session.status = AssistStatus.NEEDS_INPUT
        session.questions = result.questions
        session.history = [*session.history, _event("questions", items=result.questions)]
    session.save()
    return str(session.status)
