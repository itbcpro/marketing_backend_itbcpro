"""Weekly reflection: turn feedback into proposed memory items with evidence."""

import json
import uuid
from typing import Literal

from django.conf import settings
from pydantic import BaseModel, Field

from audit.actors import Actor
from llm import prompts
from llm.ports import LlmRequest, Message, SystemBlock
from llm.service import LlmService, get_llm_service, workspace_api_key
from llm.structured import complete_structured
from memory import services
from memory.models import FeedbackEvent, MemoryItem, MemoryKind, MemoryScope, MemoryStatus

MIN_EVIDENCE = 2
MAX_FEEDBACK = 60
PROMPT_KEY = "memory/reflection"


class ProposedMemory(BaseModel):
    text: str = Field(min_length=8)
    kind: Literal["rule", "preference", "fact"]
    scope: Literal["workspace", "channel", "agent"] = "workspace"
    scope_ref: str = ""
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    explicit_instruction: bool = False
    contradicts: str = ""


class ReflectionOutput(BaseModel):
    items: list[ProposedMemory] = Field(default_factory=list)


def _describe(event: FeedbackEvent) -> str:
    detail = f"{event.agent} / {event.action_kind}: {event.verdict}"
    if event.reason:
        detail += f" (reason: {event.reason})"
    if event.note:
        detail += f" note: {event.note}"
    if event.changed_fields:
        detail += f" edited fields: {', '.join(event.changed_fields)}"
    return detail


def run_reflection(workspace_id: uuid.UUID, service: LlmService) -> list[MemoryItem]:
    """Create ``proposed`` memory items. Nothing becomes active without a human decision."""
    events = list(
        FeedbackEvent.objects.filter(workspace_id=workspace_id, processed=False).order_by(
            "created_at"
        )[:MAX_FEEDBACK]
    )
    if not events:
        return []
    existing = MemoryItem.objects.filter(
        workspace_id=workspace_id, status__in=[MemoryStatus.ACTIVE, MemoryStatus.PROPOSED]
    )
    prompt = prompts.get_prompt(PROMPT_KEY)
    text = prompt.render(
        feedback="\n".join(f"{i + 1}. {_describe(e)}" for i, e in enumerate(events)),
        outcomes="(no measured outcomes available yet)",
        existing_memories="\n".join(f"- {m.text}" for m in existing[:50]) or "(none)",
    )
    request = LlmRequest(
        tier=prompt.tier, system=[SystemBlock(text)], messages=[Message.user("Begin.")]
    )
    output, _ = complete_structured(
        service,
        request,
        ReflectionOutput,
        workspace_id=workspace_id,
        prompt_key=f"{PROMPT_KEY}@v{prompt.version}",
    )
    created: list[MemoryItem] = []
    for proposal in output.items:
        if len(proposal.evidence) < MIN_EVIDENCE and not proposal.explicit_instruction:
            continue  # not enough evidence to be worth the owner's attention
        created.append(
            services.remember(
                workspace_id=workspace_id,
                text=proposal.text,
                kind=MemoryKind(proposal.kind),
                scope=MemoryScope(proposal.scope),
                scope_ref=proposal.scope_ref,
                source="reflection",
                evidence=proposal.evidence,
                confidence=proposal.confidence,
                actor=Actor.agent("reflection"),
            )
        )
    FeedbackEvent.objects.filter(pk__in=[e.pk for e in events]).update(processed=True)
    return created


def reflect_all_workspaces(service: LlmService | None = None) -> dict[str, int]:
    """Weekly job: one reflection per workspace that has unprocessed feedback."""
    from workspaces.models import Workspace

    counts: dict[str, int] = {}
    pending = FeedbackEvent.objects.filter(processed=False).values_list("workspace_id", flat=True)
    for workspace in Workspace.objects.filter(pk__in=set(pending), is_active=True):
        if service is None and not (settings.ANTHROPIC_API_KEY or workspace_api_key(workspace.pk)):
            continue  # no model credentials for this workspace yet
        counts[str(workspace.pk)] = len(
            run_reflection(workspace.pk, service or get_llm_service(workspace.pk))
        )
    return counts


def evidence_summary(item: MemoryItem) -> str:
    return json.dumps(item.evidence)
