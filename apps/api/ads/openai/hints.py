"""AI cleanup of an ad group's context hints; applying it edits the group via actions."""

import json
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from ninja import Schema
from pydantic import Field, field_validator

from ads.models import AdEntity, AdHintProposal, AdLevel
from ads.openai import changes
from ads.openai.checks import FRAGMENT_STARTS
from ads.openai.review import AGENT, container_snapshot
from connections.models import Connection
from core.errors import ConflictError, DomainError, ErrorCode, NotFoundError, ValidationFailedError
from core.logging import get_logger
from identity.models import User
from llm import prompts
from llm.ports import LlmRequest, Message, SystemBlock
from llm.service import get_llm_service
from llm.structured import complete_structured
from memory import services as memory

log = get_logger(__name__)

PROMPT_KEY = "ads/openai-hints"
MIN_HINTS = 8
MAX_HINTS = 20
HINT_MIN_CHARS = 3
HINT_MAX_CHARS = 200
MAX_INSTRUCTION_CHARS = 1000
STUCK_AFTER = timedelta(minutes=10)


class HintResult(Schema):
    hints: list[str] = Field(min_length=MIN_HINTS, max_length=MAX_HINTS)
    summary: str
    notes: str

    @field_validator("hints")
    @classmethod
    def clean(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for hint in value:
            text = hint.strip()
            if not HINT_MIN_CHARS <= len(text) <= HINT_MAX_CHARS:
                raise ValueError(
                    f"each hint must be {HINT_MIN_CHARS} to {HINT_MAX_CHARS} characters"
                )
            if text.lower().startswith(FRAGMENT_STARTS):
                raise ValueError(f"a hint must not start with a conjunction: {text[:40]!r}")
            if text.lower() in seen:
                raise ValueError(f"duplicate hint: {text[:40]!r}")
            seen.add(text.lower())
        return [h.strip() for h in value]


def _enqueue(proposal: AdHintProposal) -> None:
    from ads.tasks import ad_hints_task

    transaction.on_commit(lambda: ad_hints_task.delay(str(proposal.pk)))


def start(
    connection: Connection, user_id: uuid.UUID, entity: AdEntity, instruction: str
) -> AdHintProposal:
    if entity.level != AdLevel.AD_GROUP:
        raise ValidationFailedError("hints belong to an ad group")
    text = instruction.strip()
    if len(text) > MAX_INSTRUCTION_CHARS:
        raise ValidationFailedError("the request is too long")
    busy = AdHintProposal.objects.filter(entity=entity, status="thinking").first()
    if busy and busy.thinking_since and timezone.now() - busy.thinking_since < STUCK_AFTER:
        return busy
    proposal = AdHintProposal.objects.create(
        workspace_id=connection.workspace_id,
        connection=connection,
        entity=entity,
        created_by_id=user_id,
        instruction=text,
        before=list(entity.data.get("context_hints") or []),
        thinking_since=timezone.now(),
    )
    _enqueue(proposal)
    return proposal


def get(workspace_id: uuid.UUID, proposal_id: uuid.UUID) -> AdHintProposal:
    found = AdHintProposal.objects.filter(pk=proposal_id, workspace_id=workspace_id).first()
    if found is None:
        raise NotFoundError("hint proposal not found")
    if (
        found.status == "thinking"
        and found.thinking_since
        and timezone.now() - found.thinking_since > STUCK_AFTER
    ):
        found.status = "failed"
        found.error_code = ErrorCode.PROVIDER_UNAVAILABLE.value
        found.save(update_fields=["status", "error_code", "updated_at"])
    return found


def run(proposal_id: uuid.UUID) -> str:
    proposal = (
        AdHintProposal.objects.select_related("workspace", "entity__parent")
        .filter(pk=proposal_id)
        .first()
    )
    if proposal is None or proposal.status != "thinking":
        return "skipped"
    entity = proposal.entity
    try:
        prompt = prompts.get_prompt(PROMPT_KEY)
        workspace = proposal.workspace
        system = prompt.render(
            workspace_name=workspace.name,
            brand_profile=memory.render_brand_profile(memory.current_brand_profile(workspace.pk)),
            memories=memory.render_memories(
                memory.retrieve(
                    workspace_id=workspace.pk,
                    query=f"context hints {entity.name}",
                    agent=AGENT,
                    limit=6,
                )
            ),
            snapshot=json.dumps(container_snapshot(entity), indent=1, default=str),
            current_hints=json.dumps(proposal.before, indent=1),
            instruction=proposal.instruction,
        )
        request = LlmRequest(
            tier=prompt.tier,
            system=[SystemBlock(system)],
            messages=[Message.user("Answer with the cleaned hints.")],
        )
        result, _ = complete_structured(
            get_llm_service(workspace.pk),
            request,
            HintResult,
            workspace_id=workspace.pk,
            prompt_key=f"{PROMPT_KEY}@v{prompt.version}",
        )
    except DomainError as exc:
        return _fail(proposal, exc.code.value)
    except Exception:
        log.exception("ad_hints_crashed")
        return _fail(proposal, ErrorCode.UNKNOWN.value)
    proposal.after = result.hints
    proposal.summary = result.summary
    proposal.notes = result.notes
    proposal.status = "done"
    proposal.thinking_since = None
    proposal.save()
    return "done"


def _fail(proposal: AdHintProposal, code: str) -> str:
    proposal.status = "failed"
    proposal.error_code = code
    proposal.thinking_since = None
    proposal.save(update_fields=["status", "error_code", "thinking_since", "updated_at"])
    return "failed"


def apply(proposal: AdHintProposal, user: User) -> tuple[AdHintProposal, bool]:
    """Replace the group's hints with the cleaned list. Applied now when the user may approve."""
    if proposal.status != "done" or not proposal.after:
        raise ValidationFailedError("there is no cleaned list to apply")
    if proposal.decided:
        raise ConflictError("this proposal was already applied")
    action, applied = changes.submit(
        proposal.connection,
        user,
        "ad_group.update",
        {"ad_group_id": proposal.entity.external_id, "context_hints": proposal.after},
        summary="Replace the context hints with the AI-cleaned list",
        apply_now=True,
    )
    proposal.decided = True
    proposal.action_id = action.pk
    proposal.applied = applied
    proposal.save()
    return proposal, applied
