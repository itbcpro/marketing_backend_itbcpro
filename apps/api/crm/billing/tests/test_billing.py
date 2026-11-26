from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from conftest import make_user
from crm.billing import services
from crm.billing.models import Invoice
from crm.channels.fake import FakeAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.inbox.models import Message
from crm.plans import services as plans
from crm.sending import gateway, registry
from crm.usage import charging
from crm.usage.models import RateCard, UsageRecord
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
TODAY = date(2026, 12, 3)  # invoices are for November 2026
FIRST, LAST = date(2026, 11, 1), date(2026, 11, 30)


def sent_message(workspace, key):
    """One rated email in November: price 2 units of 1000 micros."""
    adapter = FakeAdapter("email")
    registry.register("email", lambda: adapter)
    person = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": f"{key}@example.com"}
    )
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=person.pk,
        channel="email",
        purpose="transactional",
        status="opted_in",
        source="form",
    )
    outcome = gateway.send(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=person.pk,
        channel="email",
        body="Hi",
        subject="S",
    )
    Message.objects.filter(pk=outcome.message.pk).update(status="sent")
    record = charging.charge_message(workspace_id=workspace.pk, message_id=outcome.message.pk)
    UsageRecord.objects.filter(pk=record.pk).update(
        created_at=timezone.make_aware(timezone.datetime(2026, 11, 15, 12, 0))
    )
    return record


@pytest.fixture
def rated(workspace):
    RateCard.objects.create(
        channel="email",
        currency="CAD",
        cost_micros=400,
        price_micros=1000,
        effective_from=timezone.now() - timedelta(days=400),
    )
    return workspace


def test_a_paid_plan_with_usage_gets_one_invoice_with_lines(rated):
    plans.set_plan(rated.organization, "starter")
    sent_message(rated, "a")
    sent_message(rated, "b")
    (invoice,) = [services.generate(rated.organization, FIRST, LAST)]
    assert invoice.number.startswith("INV-202611-")
    assert (invoice.plan_fee_micros, invoice.usage_micros) == (49_000_000, 2000)
    assert invoice.amount_due_micros == 49_002_000 and invoice.status == "issued"
    assert [(line["kind"], line["amount_micros"]) for line in invoice.lines] == [
        ("plan", 49_000_000),
        ("usage", 2000),
    ]
    assert invoice.lines[1]["channel"] == "email" and invoice.lines[1]["units"] == 2


def test_prepaid_usage_is_not_billed_again(rated):
    plans.set_plan(rated.organization, "starter")
    rated.organization.limits = {"billing_mode": "prepaid"}
    rated.organization.save()
    sent_message(rated, "a")
    invoice = services.generate(rated.organization, FIRST, LAST)
    assert invoice.usage_micros == 1000 and invoice.amount_due_micros == 49_000_000


def test_a_free_plan_with_no_usage_has_nothing_to_invoice(workspace):
    assert services.generate(workspace.organization, FIRST, LAST) is None
    assert Invoice.objects.count() == 0


def test_postpaid_usage_on_the_free_plan_is_invoiced(rated):
    sent_message(rated, "a")
    invoice = services.generate(rated.organization, FIRST, LAST)
    assert invoice.plan_fee_micros == 0 and invoice.amount_due_micros == 1000


def test_running_it_again_changes_nothing(rated):
    plans.set_plan(rated.organization, "starter")
    first = services.generate(rated.organization, FIRST, LAST)
    sent_message(rated, "late")  # arrives after the invoice was made: the invoice stays as issued
    again = services.generate(rated.organization, FIRST, LAST)
    assert again.pk == first.pk and again.usage_micros == 0
    assert services.generate_all(TODAY) == 0
    assert Invoice.objects.count() == 1


def test_the_monthly_run_covers_the_month_that_just_ended(rated):
    plans.set_plan(rated.organization, "starter")
    assert services.previous_month(date(2027, 1, 9)) == (date(2026, 12, 1), date(2026, 12, 31))
    assert services.generate_all(TODAY) == 1
    assert Invoice.objects.get().period_start == FIRST


def test_usage_of_other_organizations_never_leaks_in(rated, owner):
    other = create_workspace(name="Other Co", owner=owner)
    sent_message(other, "theirs")
    mine = services.generate(rated.organization, FIRST, LAST)
    assert mine is None  # free plan, and the only usage belongs to someone else


def test_the_operator_command_marks_an_invoice_paid(rated):
    plans.set_plan(rated.organization, "starter")
    invoice = services.generate(rated.organization, FIRST, LAST)
    out = StringIO()
    call_command("crm_invoice_paid", invoice.number, stdout=out)
    invoice.refresh_from_db()
    assert invoice.status == "paid" and invoice.paid_at and "paid" in out.getvalue()
    with pytest.raises(CommandError):
        call_command("crm_invoice_paid", "INV-NOPE")


def api(client, path, workspace):
    return client.get(f"/api/v1/crm/{path}", HTTP_X_WORKSPACE_ID=str(workspace.pk))


def test_only_billing_managers_see_invoices_and_only_their_own(rated, owner):
    plans.set_plan(rated.organization, "starter")
    invoice = services.generate(rated.organization, FIRST, LAST)
    admin = Client()
    admin.force_login(owner)
    listed = api(admin, "invoices", rated).json()
    assert [i["number"] for i in listed] == [invoice.number]
    assert api(admin, f"invoices/{invoice.pk}", rated).json()["lines"][0]["kind"] == "plan"
    for role in (Role.VIEWER, Role.EDITOR, Role.APPROVER):
        client = Client()
        client.force_login(make_user(f"{role}@example.com", rated, role))
        assert api(client, "invoices", rated).status_code == 403
    other = create_workspace(name="Other Co", owner=owner)
    assert api(admin, "invoices", other).json() == []
    assert api(admin, f"invoices/{invoice.pk}", other).status_code == 404
