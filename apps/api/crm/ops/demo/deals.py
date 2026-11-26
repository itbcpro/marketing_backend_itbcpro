"""Pipelines, deals in every status, notes, tasks and the activity they leave behind."""

from __future__ import annotations

from crm.activity import services as activity
from crm.ops.demo.base import Demo
from crm.pipelines import services as pipelines
from crm.pipelines.models import Opportunity, Pipeline

MICROS = 1_000_000
SALES = (
    "Demo Sales",
    [("New lead", 10), ("Contacted", 25), ("Proposal sent", 50), ("Negotiation", 75)],
)
ONBOARDING = ("Demo Onboarding", [("Kickoff", 20), ("Setup", 50), ("Live", 90)])
LOST_REASONS = ["Price too high", "Chose a competitor", "No response", "Bad timing"]
DEAL_NAMES = [
    "Website redesign",
    "SEO retainer",
    "Google Ads setup",
    "Social media package",
    "Email automation",
    "CRM migration",
    "Brand refresh",
    "Local SEO audit",
]
PRIORITIES = ["low", "normal", "normal", "high"]
NOTES = [
    "Called, left a voicemail. Try again Thursday.",
    "Wants a quote before the end of the month.",
    "Asked about payment terms. Prefers monthly billing.",
    "Met at the trade show, very interested in local SEO.",
    "Decision maker is the owner; budget approved.",
]
TASKS = ["Call back", "Send proposal", "Prepare quote", "Follow up by email", "Book a demo"]
DEAL_COUNT = 40
LOST_AFTER = 28  # deals from this index on are closed
STALE_EVERY = 6


def _pipeline(demo: Demo, name: str, stages: list[tuple[str, int]]) -> Pipeline:
    found = pipelines.list_pipelines(demo.wid).filter(name=name).first()
    if found:
        return found
    demo.count("pipelines")
    return pipelines.create_pipeline(demo.wid, name=name, stages=stages)


def seed_deals(demo: Demo) -> list[Opportunity]:
    if Opportunity.objects.for_workspace(demo.wid).filter(name__startswith="Demo: ").exists():
        return list(Opportunity.objects.for_workspace(demo.wid).filter(name__startswith="Demo: "))
    sales = _pipeline(demo, *SALES)
    onboarding = _pipeline(demo, *ONBOARDING)
    for name in LOST_REASONS:
        if not pipelines.list_lost_reasons(demo.wid).filter(name=name).exists():
            pipelines.create_lost_reason(demo.wid, name)
    reasons = list(pipelines.list_lost_reasons(demo.wid).filter(name__in=LOST_REASONS))
    deals: list[Opportunity] = []
    rng = demo.rng
    for index in range(DEAL_COUNT):
        pipeline = onboarding if index % 5 == 4 else sales
        stages = list(pipeline.stages.order_by("position"))
        deal = pipelines.create_opportunity(
            demo.wid,
            demo.actor,
            contact_id=demo.contacts[index % len(demo.contacts)].pk,
            pipeline_id=pipeline.pk,
            name=f"Demo: {DEAL_NAMES[index % len(DEAL_NAMES)]} #{index + 1}",
            value_micros=rng.choice([800, 1500, 3200, 6000, 12000]) * MICROS,
            currency="CAD",
            assigned_to=rng.choice(demo.user_ids()),
            source=rng.choice(["web form", "referral", "facebook lead ad"]),
            expected_close_date=demo.ahead(days=rng.randint(-10, 60)).date(),
            priority=rng.choice(PRIORITIES),
            tags=rng.sample(["upsell", "renewal", "inbound", "partner"], k=rng.randint(0, 2)),
            custom={"contract_term": rng.choice(["6 months", "12 months", "monthly"])},
        )
        for stage in stages[1 : rng.randint(1, len(stages))]:
            deal = pipelines.move_opportunity(demo.wid, demo.actor, deal, stage.pk)
        if index >= LOST_AFTER:
            kind = index % 3
            if kind == 0:
                deal = pipelines.win(demo.wid, demo.actor, deal)
            elif kind == 1:
                deal = pipelines.lose(
                    demo.wid, demo.actor, deal, lost_reason_id=rng.choice(reasons).pk
                )
            else:
                deal = pipelines.abandon(demo.wid, demo.actor, deal)
        elif index % STALE_EVERY == 0:  # an old open deal shows up as "stale" on the board
            Opportunity.objects.filter(pk=deal.pk).update(last_stage_change_at=demo.ago(days=45))
        deals.append(deal)
        demo.count("deals")
    return deals


def seed_notes_and_tasks(demo: Demo, deals: list[Opportunity]) -> None:
    rng = demo.rng
    for index, contact in enumerate(demo.contacts[:40]):
        note = activity.add_note(
            workspace_id=demo.wid,
            actor=demo.actor,
            contact_id=contact.pk,
            body=rng.choice(NOTES),
            pinned=index % 8 == 0,
        )
        demo.count("notes")
        if index % 4 == 0:
            activity.add_note(
                workspace_id=demo.wid,
                actor=demo.actor,
                contact_id=contact.pk,
                body=f"@team please check this account. ({note.pk.hex[:4]})",
                mentions=[demo.members[-1].user_id],
            )
    for index in range(30):
        contact = demo.contacts[index]
        deal = next((d for d in deals if d.contact_id == contact.pk), None)
        due = demo.ahead(days=rng.randint(-5, 10), hours=rng.randint(0, 8))
        task = activity.create_task(
            workspace_id=demo.wid,
            actor=demo.actor,
            contact_id=contact.pk,
            title=rng.choice(TASKS),
            description="Demo task.",
            due_at=due,
            assigned_to=rng.choice(demo.user_ids()),
            opportunity_id=deal.pk if deal and index % 2 == 0 else None,
        )
        demo.count("tasks")
        if index % 4 == 0:
            activity.complete_task(workspace_id=demo.wid, actor=demo.actor, task_id=task.pk)


def run(demo: Demo) -> None:
    deals = seed_deals(demo)
    if deals and demo.counts.get("deals"):
        seed_notes_and_tasks(demo, deals)
