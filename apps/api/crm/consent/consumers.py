"""Reactions of the consent module to events of other modules."""

from audit.actors import Actor
from audit.outbox import register_handler
from crm.consent import services
from crm.consent.models import ConsentState, Source, Status
from crm.events.consumers import idempotent
from crm.events.envelope import EventEnvelope


@idempotent("consent.on_contact_merged")
def on_contact_merged(envelope: EventEnvelope) -> None:
    """Carry the loser's consent to the winner, never loosening what the winner already has.

    An opt-out always wins; an opt-in is copied only where the winner has no answer yet.
    """
    workspace_id = envelope.tenant_id
    winner_id, loser_id = envelope.data["winner_id"], envelope.data["loser_id"]
    have = {
        (s.channel, s.purpose): s
        for s in ConsentState.objects.for_workspace(workspace_id).filter(contact_id=winner_id)
    }
    for state in ConsentState.objects.for_workspace(workspace_id).filter(contact_id=loser_id):
        mine = have.get((state.channel, state.purpose))
        carry = (
            state.status == Status.OPTED_OUT and (mine is None or mine.status != Status.OPTED_OUT)
        ) or (state.status == Status.OPTED_IN and mine is None)
        if carry:
            services.record(
                workspace_id=workspace_id,
                actor=Actor.system("merge"),
                contact_id=winner_id,
                channel=state.channel,
                purpose=state.purpose,
                status=state.status,
                source=Source.MERGE,
                evidence={"merged_from": str(loser_id), "original_event": str(state.last_event_id)},
                expires_at=state.expires_at,
            )


def register() -> None:
    register_handler("crm.contact.merged", on_contact_merged)
