import re
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from audit.models import OutboxMessage
from conftest import make_user
from core.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from core.metrics import REGISTRY
from core.models import DeadLetter
from crm.channels.fake import FakeAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.inbox import services as inbox
from crm.inbox.models import Message
from crm.ops import health, metrics
from crm.sending import gateway, registry
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
RULES = Path(__file__).resolve().parents[5] / "infra" / "alerts" / "crm.rules.yml"
SYSTEM = Actor.system("test")


def sample(name, labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_the_alert_rules_only_name_metrics_that_exist():
    rules = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    exported = {m.name for m in REGISTRY.collect()}
    exported |= {f"{name}_total" for name in exported}  # counters are exposed with a suffix
    used = set()
    for group in rules["groups"]:
        for rule in group["rules"]:
            used |= set(re.findall(r"\bcrm_[a-z_]+", rule["expr"]))
    assert used, "no CRM metrics found in the rules"
    assert used <= exported, sorted(used - exported)


def test_every_rule_has_a_severity_and_a_summary():
    rules = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    for group in rules["groups"]:
        for rule in group["rules"]:
            assert rule["labels"]["severity"] in {"page", "warn", "info"}, rule["alert"]
            assert rule["annotations"]["summary"], rule["alert"]


def test_callbacks_are_counted_by_what_happened_to_them():
    def outcome(name):
        return sample("crm_webhooks_total", {"source": "test", "outcome": name})

    before = {n: outcome(n) for n in ("ok", "bad_signature", "not_found", "invalid")}
    with metrics.watch("test"):
        pass
    for error in (PermissionDeniedError("x"), NotFoundError("x"), ValidationFailedError("x")):
        with pytest.raises(type(error)), metrics.watch("test"):
            raise error
    for name, count in before.items():
        assert outcome(name) == count + 1


def test_a_sent_message_is_counted(workspace):
    adapter = FakeAdapter("email")
    registry.register("email", lambda: adapter)
    contact = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "a@x.test"}
    )
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=contact.pk,
        channel="email",
        purpose="transactional",
        status="opted_in",
        source="form",
    )
    sent = sample("crm_messages_total", {"channel": "email", "outcome": "sent"})
    gateway.send(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=contact.pk,
        channel="email",
        body="Hi",
        subject="S",
    )
    assert sample("crm_messages_total", {"channel": "email", "outcome": "sent"}) == sent + 1


def _conversation(workspace):
    contact = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "h@x.test"}
    )
    return inbox.get_or_create(workspace.pk, contact)


def test_health_flags_each_signal_past_its_limit(workspace, settings):
    settings.CRM_HEALTH_OUTBOX_LAG_SECONDS = 60
    settings.CRM_HEALTH_MIN_MESSAGES = 2
    now = timezone.now()
    calm = {s["key"]: s["status"] for s in health.snapshot(workspace.pk, now)}
    assert set(calm.values()) == {"ok"}
    OutboxMessage.objects.create(topic="x", payload={})
    OutboxMessage.objects.update(available_at=now - timedelta(minutes=10))
    DeadLetter.objects.create(
        task_name="crm.workflows.step", kwargs={"workspace_id": str(workspace.pk)}, error="x"
    )
    DeadLetter.objects.create(task_name="other", kwargs={"workspace_id": "elsewhere"}, error="x")
    conversation = _conversation(workspace)
    for _ in range(2):
        Message.objects.create(
            workspace_id=workspace.pk,
            conversation=conversation,
            channel="email",
            direction="outbound",
            status="failed",
            provider_ts=now,
        )
    flagged = {s["key"]: s for s in health.snapshot(workspace.pk, now)}
    assert flagged["outbox_lag"]["status"] == "warn" and flagged["outbox_lag"]["value"] >= 600
    assert flagged["dead_letters"]["value"] == 1  # only the ones of this workspace
    assert flagged["send_failure_percent"]["status"] == "warn"
    assert flagged["overdue_runs"]["status"] == "ok"


def test_the_health_endpoint_is_for_people_with_the_reports_permission(workspace, owner):
    url = "/api/v1/crm/health"
    admin = Client()
    admin.force_login(owner)
    res = admin.get(url, HTTP_X_WORKSPACE_ID=str(workspace.pk))
    assert res.status_code == 200
    assert {s["key"] for s in res.json()} == {
        "outbox_lag",
        "overdue_runs",
        "dead_letters",
        "send_failure_percent",
    }
    viewer = Client()
    viewer.force_login(make_user("v@example.com", workspace, Role.VIEWER))
    assert viewer.get(url, HTTP_X_WORKSPACE_ID=str(workspace.pk)).status_code == 403
    other = create_workspace(name="Other Co", owner=owner)
    DeadLetter.objects.create(
        task_name="crm.workflows.step", kwargs={"workspace_id": str(workspace.pk)}, error="x"
    )
    theirs = admin.get(url, HTTP_X_WORKSPACE_ID=str(other.pk)).json()
    assert {s["key"]: s["value"] for s in theirs}["dead_letters"] == 0


def test_the_scrape_includes_the_backlog_gauges(workspace):
    names = {m.name for m in REGISTRY.collect()}
    assert {
        "crm_outbox_oldest_pending_seconds",
        "crm_overdue_enrollments",
        "crm_dead_letters_open",
    } <= names
