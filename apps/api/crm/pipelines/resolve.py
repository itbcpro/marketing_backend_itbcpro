"""Find pipelines, stages and lost reasons by name, for imports from other systems."""

from __future__ import annotations

import uuid

from core.errors import ValidationFailedError
from crm.pipelines import clean
from crm.pipelines.models import Opportunity, OpportunityStatus, Stage


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def resolve_stage(
    workspace_id: uuid.UUID, *, pipeline: str, stage: str, create_missing: bool
) -> Stage:
    """The stage called ``stage`` in the pipeline called ``pipeline``.

    An empty stage name means the first stage. With ``create_missing`` an unknown pipeline or
    stage is created (a new stage goes last); otherwise it is an error.
    """
    from crm.pipelines import services  # noqa: PLC0415 - services imports this module

    found = next(
        (p for p in services.list_pipelines(workspace_id) if _same(p.name, pipeline)), None
    )
    if found is None:
        if not create_missing:
            raise ValidationFailedError("unknown pipeline", errors={"pipeline": "unknown"})
        found = services.create_pipeline(
            workspace_id, name=clean.name(pipeline), stages=[(stage or "New", 0)]
        )
    stages = list(found.stages.all())
    if not stage:
        return stages[0]
    match = next((s for s in stages if _same(s.name, stage)), None)
    if match is not None:
        return match
    if not create_missing:
        raise ValidationFailedError("unknown stage", errors={"stage": "unknown"})
    return services.add_stage(workspace_id, found.pk, name=stage)


def resolve_lost_reason(workspace_id: uuid.UUID, name: str, *, create_missing: bool) -> uuid.UUID:
    from crm.pipelines import services  # noqa: PLC0415 - services imports this module

    found = next((r for r in services.list_lost_reasons(workspace_id) if _same(r.name, name)), None)
    if found is None:
        if not create_missing:
            raise ValidationFailedError("unknown lost reason", errors={"lost_reason": "unknown"})
        found = services.create_lost_reason(workspace_id, name)
    return found.pk


def find_deal(
    workspace_id: uuid.UUID, *, contact_id: uuid.UUID, pipeline_id: uuid.UUID, name: str
) -> Opportunity | None:
    """An earlier deal of the contact with this name in the pipeline; makes imports repeatable."""
    rows = Opportunity.objects.for_workspace(workspace_id).filter(
        contact_id=contact_id, pipeline_id=pipeline_id
    )
    return next((d for d in rows if _same(d.name, name)), None)


STATUSES = tuple(OpportunityStatus.values)
