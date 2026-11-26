"""Smart lists, workflows with people inside them, lead ads, webhooks and notifications."""

from __future__ import annotations

from typing import Any

from crm.leads.models import (
    ConversionLog,
    ConversionRule,
    LeadCapture,
    LeadFormMapping,
)
from crm.lists import smart_lists
from crm.lists.models import SmartList
from crm.notifications import services as notifications
from crm.notifications.models import Notification
from crm.ops.demo.base import DEMO_TAG, Demo
from crm.pipelines.models import Stage
from crm.plans import services as plans
from crm.webhooks import services as webhooks
from crm.webhooks.models import WebhookEndpoint
from crm.workflows import definitions
from crm.workflows.models import Enrollment, StepLog, Workflow
from workspaces.models import Workspace

LISTS = [
    (
        "Demo Hot leads",
        "shared",
        {
            "op": "and",
            "conditions": [
                {"field": "tags", "operator": "has", "value": "hot"},
                {"field": "contact_type", "operator": "eq", "value": "lead"},
            ],
        },
        ["first_name", "last_name", "email", "source"],
    ),
    (
        "Demo Customers in Ontario",
        "shared",
        {
            "op": "and",
            "conditions": [
                {"field": "contact_type", "operator": "eq", "value": "customer"},
                {"field": "state", "operator": "eq", "value": "ON"},
            ],
        },
        ["first_name", "last_name", "city", "phone"],
    ),
    (
        "Demo My private follow-ups",
        "private",
        {
            "op": "and",
            "conditions": [{"field": "tags", "operator": "has", "value": "needs follow-up"}],
        },
        ["first_name", "last_name", "email"],
    ),
    (
        "Demo Facebook leads",
        "shared",
        {
            "op": "and",
            "conditions": [{"field": "source", "operator": "contains", "value": "facebook"}],
        },
        ["first_name", "email", "source", "created_at"],
    ),
]


def _step(sid: str, kind: str, config: dict[str, Any], nxt: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": sid, "type": kind, "config": config}
    if nxt:
        out["next"] = nxt
    return out


def _flows(demo: Demo) -> list[tuple[str, str, dict[str, Any]]]:
    stage = Stage.objects.for_workspace(demo.wid).filter(name="Proposal sent").first()
    return [
        (
            "Demo Welcome sequence",
            "active",
            {
                "trigger": {"type": "contact_created", "config": {}},
                "start": "tag",
                "steps": [
                    _step("tag", "add_tag", {"tag": "welcomed"}, "wait"),
                    _step("wait", "wait", {"seconds": 86400}, "mail"),
                    _step(
                        "mail",
                        "send_message",
                        {
                            "channel": "email",
                            "subject": "Welcome!",
                            "body": "Hi {{contact.first_name}}, welcome aboard.",
                        },
                        "task",
                    ),
                    _step("task", "create_task", {"title": "Check in", "due_in_days": 3}, "end"),
                    _step("end", "finish", {}),
                ],
            },
        ),
        (
            "Demo Quote follow-up",
            "active",
            {
                "trigger": {"type": "stage_reached", "config": {"stage_id": str(stage.pk)}},
                "start": "wait",
                "steps": [
                    _step(
                        "wait",
                        "wait_for_event",
                        {
                            "event": "reply",
                            "timeout_seconds": 172800,
                            "on_event": "end",
                            "on_timeout": "nudge",
                        },
                    ),
                    _step("nudge", "create_task", {"title": "Nudge about the quote"}, "end"),
                    _step("end", "finish", {}),
                ],
            }
            if stage
            else {},
        ),
        (
            "Demo Missed call text-back",
            "paused",
            {
                "trigger": {"type": "missed_call", "config": {}},
                "start": "mail",
                "steps": [
                    _step(
                        "mail",
                        "send_message",
                        {"channel": "sms", "body": "Sorry we missed your call, how can we help?"},
                        "end",
                    ),
                    _step("end", "finish", {}),
                ],
            },
        ),
        (
            "Demo Draft: VIP onboarding",
            "draft",
            {
                "trigger": {"type": "tag_added", "config": {"tag": "vip"}},
                "start": "task",
                "steps": [
                    _step("task", "create_task", {"title": "Send gift"}, "end"),
                    _step("end", "finish", {}),
                ],
            },
        ),
    ]


def seed_lists(demo: Demo) -> None:
    for name, visibility, spec, columns in LISTS:
        if SmartList.objects.for_workspace(demo.wid).filter(name=name).exists():
            continue
        smart_lists.create(
            membership=demo.owner,
            actor=demo.actor,
            name=name,
            spec=spec,
            columns=columns,
            visibility=visibility,
        )
        demo.count("smart lists")


def seed_workflows(demo: Demo) -> None:
    plans.set_plan(
        Workspace.objects.select_related("organization").get(pk=demo.wid).organization, "growth"
    )  # the free plan only allows two workflows
    for name, state, definition in _flows(demo):
        if (
            not definition
            or Workflow.objects.for_workspace(demo.wid)
            .filter(name=name, archived_at__isnull=True)
            .exists()
        ):
            continue
        flow = definitions.create_workflow(demo.wid, name=name)
        definitions.save_draft(demo.wid, flow.pk, definition)
        demo.count("workflows")
        if state == "draft":
            continue
        flow = definitions.publish(demo.wid, flow.pk)
        if state == "paused":
            definitions.set_paused(demo.wid, flow.pk, True)
            continue
        _enroll(demo, flow, definition)


def _enroll(demo: Demo, flow: Workflow, definition: dict[str, Any]) -> None:
    steps = [s["id"] for s in definition["steps"]]
    outcomes = [
        ("waiting", steps[1] if len(steps) > 1 else steps[0]),
        ("completed", steps[-1]),
        ("active", steps[0]),
        ("exited", steps[0]),
        ("failed", steps[-2]),
    ]
    version = flow.published_version
    assert version is not None  # noqa: S101 - published just above
    for index, (status, current) in enumerate(outcomes):
        contact = demo.contacts[10 + index]
        row = Enrollment.objects.create(
            workspace_id=demo.wid,
            workflow=flow,
            version=version,
            contact=contact,
            status=status,
            current_step=current,
            wake_at=demo.ahead(hours=6) if status == "waiting" else None,
            waiting_for="timer" if status == "waiting" else "",
            exit_reason="replied" if status == "exited" else "",
            finished_at=demo.ago(days=1) if status in {"completed", "exited", "failed"} else None,
        )
        for sid in steps[: steps.index(current) + (status == "completed")]:
            StepLog.objects.create(
                workspace_id=demo.wid,
                enrollment=row,
                step_id=sid,
                step_type=sid,
                outcome="done",
                detail="demo",
            )
        if status == "failed":
            StepLog.objects.create(
                workspace_id=demo.wid,
                enrollment=row,
                step_id=current,
                step_type="send_message",
                outcome="failed",
                detail="provider_rejected",
                attempt=3,
            )
        demo.count("enrollments")


def seed_leads(demo: Demo) -> None:
    if LeadFormMapping.objects.for_workspace(demo.wid).exists():
        return
    for form_id, name, tags in [
        ("1001", "Free quote form", ["facebook", DEMO_TAG]),
        ("1002", "Webinar signup", ["webinar", DEMO_TAG]),
    ]:
        LeadFormMapping.objects.create(
            workspace_id=demo.wid,
            form_id=form_id,
            name=name,
            mapping={"your_budget": "custom.budget"},
            tags=tags,
            consent_channels=["email", "sms"],
        )
    for index, contact in enumerate(demo.contacts[40:52]):
        LeadCapture.objects.create(
            workspace_id=demo.wid,
            leadgen_id=f"demo-lead-{index}",
            contact=contact,
            form_id="1001" if index % 2 == 0 else "1002",
            page_id="9001",
            ad_id=f"ad-{index % 3}",
            adset_id="adset-1",
            campaign_id="camp-spring",
            answers={"full_name": contact.full_name, "your_budget": "1500"},
        )
    rules = [
        ("Report new leads", "lead_captured", "Lead", False),
        ("Report won deals", "deal_won", "Purchase", True),
    ]
    for name, trigger, event, value in rules:
        rule = ConversionRule.objects.create(
            workspace_id=demo.wid, name=name, trigger=trigger, event_name=event, send_value=value
        )
        for index, outcome in enumerate(["sent", "sent", "skipped_no_consent", "failed"]):
            ConversionLog.objects.create(
                workspace_id=demo.wid,
                rule=rule,
                event_id=f"demo-{rule.pk.hex[:6]}-{index}",
                contact_id=demo.contacts[index].pk,
                outcome=outcome,
                detail="" if outcome == "sent" else "demo",
            )
    demo.count("lead captures", 12)


def seed_webhooks(demo: Demo) -> None:
    if WebhookEndpoint.objects.for_workspace(demo.wid).exists():
        return
    webhooks.create_endpoint(demo.wid, kind="inbound_email", name="Demo inbound email")
    webhooks.create_endpoint(demo.wid, kind="email_events", name="Demo bounce notices")
    demo.count("webhook endpoints", 2)


def seed_notifications(demo: Demo) -> None:
    if Notification.objects.for_workspace(demo.wid).filter(data__demo=True).exists():
        return
    kinds = [
        "task_assigned",
        "mention",
        "contact_assigned",
        "deal_assigned",
        "conversation_assigned",
        "customer_replied",
    ]
    for index in range(12):
        contact = demo.contacts[index]
        Notification.objects.create(
            workspace_id=demo.wid,
            user_id=demo.actor.id,
            kind=kinds[index % len(kinds)],
            data={"demo": True, "contact_id": str(contact.pk), "contact_name": contact.full_name},
            read_at=demo.ago(hours=2) if index % 3 == 0 else None,
        )
    notifications.preferences(demo.wid, demo.owner.user_id)
    demo.count("notifications", 12)


def run(demo: Demo) -> None:
    seed_lists(demo)
    seed_workflows(demo)
    seed_leads(demo)
    seed_webhooks(demo)
    seed_notifications(demo)
