import json
from datetime import UTC, datetime, timedelta

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from audit.models import OutboxMessage
from audit.outbox import dispatch_batch
from conftest import make_user
from core.errors import ValidationFailedError
from crm.channels.fake import FakeAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.sending import gateway, registry
from crm.usage import charging, rating, wallet
from crm.usage.models import RateCard, TransactionKind, UsageRecord, Wallet, WalletTransaction
from crm.usage.units import sms_segments, units_for
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _commit_hooks(monkeypatch):
    monkeypatch.setattr(transaction, "on_commit", lambda func, *a, **k: func())


@pytest.fixture
def adapters():
    fakes = {c: FakeAdapter(c) for c in ("sms", "email")}
    for channel, fake in fakes.items():
        registry.register(channel, lambda fake=fake: fake)
    yield fakes
    from crm.channels.email_smtp import SmtpEmailAdapter

    registry.unregister("sms")
    registry.register("email", SmtpEmailAdapter)


def rate(
    channel="sms", price=10_000, cost=6_000, country="", category="", at=LONG_AGO, currency="CAD"
):
    return RateCard.objects.create(
        channel=channel,
        country=country,
        category=category,
        currency=currency,
        cost_micros=cost,
        price_micros=price,
        effective_from=at,
    )


@pytest.fixture
def person(workspace):
    c = contacts.create_contact(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        data={"email": "ada@example.com", "phone": "416-555-0199", "time_zone": "America/Toronto"},
    )
    for channel in ("sms", "email"):
        consent.record(
            workspace_id=workspace.pk,
            actor=SYSTEM,
            contact_id=c.pk,
            channel=channel,
            purpose="marketing",
            status="opted_in",
            source="form",
        )
    return c


def send(workspace, person, channel="sms", body="Hello", **extra):
    return gateway.send(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=person.pk,
        channel=channel,
        body=body,
        **extra,
    )


def settle(workspace):
    dispatch_batch()


# ---- units ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "segments"),
    [
        ("", 1),
        ("a" * 160, 1),
        ("a" * 161, 2),
        ("a" * 306, 2),
        ("a" * 307, 3),
        ("{" * 80, 1),
        ("{" * 81, 2),  # extension characters count twice
        ("é" * 160, 1),  # accented letters are in the GSM alphabet
        ("你" * 70, 1),
        ("你" * 71, 2),
        ("x" * 69 + "你", 1),  # one wide letter switches to UCS-2
        ("\U0001f600" * 35, 1),
        ("\U0001f600" * 36, 2),  # an emoji takes two UTF-16 units
    ],
)
def test_sms_segments(text, segments):
    assert sms_segments(text) == segments


def test_only_sms_is_counted_in_segments():
    assert units_for("email", "x" * 5000) == 1 and units_for("sms", "x" * 161) == 2


# ---- rating -----------------------------------------------------------------------------------


def test_the_most_specific_and_newest_rate_wins():
    general = rate(price=10_000)
    canada = rate(price=8_000, country="CA")
    newer_general = rate(price=12_000, at=LONG_AGO + timedelta(days=30))
    at = LONG_AGO + timedelta(days=60)
    assert rating.find_rate(channel="sms", currency="CAD", at=at, country="CA").pk == canada.pk
    assert (
        rating.find_rate(channel="sms", currency="CAD", at=at, country="US").pk == newer_general.pk
    )
    assert (
        rating.find_rate(
            channel="sms", currency="CAD", at=LONG_AGO + timedelta(days=1), country="US"
        ).pk
        == general.pk
    )
    assert rating.find_rate(channel="sms", currency="USD", at=at) is None
    assert rating.find_rate(channel="sms", currency="CAD", at=LONG_AGO - timedelta(days=1)) is None
    assert rating.find_rate(channel="voice", currency="CAD", at=at) is None


def test_country_comes_from_the_number_then_the_contact():
    assert rating.country_of("+14165550199") == "CA"
    assert rating.country_of("+442079460958", "ca") == "GB"
    assert rating.country_of("", "fr") == "FR"
    assert rating.country_of("", "") == ""


def test_markup_rounds_up_to_the_next_micro():
    assert rating.with_markup(10_000, 0) == 10_000
    assert rating.with_markup(10_000, 25) == 12_500
    assert rating.with_markup(3, 50) == 5  # 4.5 -> 5


# ---- wallet -----------------------------------------------------------------------------------


def test_topups_and_debits_are_each_applied_once(workspace):
    org = workspace.organization
    first = wallet.top_up(org, amount_micros=5_000_000, reference="pay-1")
    again = wallet.top_up(org, amount_micros=5_000_000, reference="pay-1")
    assert first.pk == again.pk and wallet.get_wallet(org).balance_micros == 5_000_000
    entry, applied = wallet.debit(
        org, amount_micros=12_000, reference="usage:x", workspace_id=workspace.pk
    )
    replay, applied_again = wallet.debit(
        org, amount_micros=12_000, reference="usage:x", workspace_id=workspace.pk
    )
    assert applied and not applied_again and entry.pk == replay.pk
    current = wallet.get_wallet(org)
    assert current.balance_micros == 4_988_000
    assert [t.balance_after_micros for t in current.transactions.order_by("created_at")] == [
        5_000_000,
        4_988_000,
    ]


@pytest.mark.parametrize(("amount", "reference"), [(0, "r"), (-5, "r"), (10, "")])
def test_invalid_topups_are_rejected(workspace, amount, reference):
    with pytest.raises(ValidationFailedError):
        wallet.top_up(workspace.organization, amount_micros=amount, reference=reference)


def test_the_ledger_cannot_repeat_a_reference(workspace):
    wallet.top_up(workspace.organization, amount_micros=1, reference="same")
    w = wallet.get_wallet(workspace.organization)
    with pytest.raises(IntegrityError), transaction.atomic():
        WalletTransaction.objects.create(
            wallet=w,
            kind=TransactionKind.TOP_UP,
            amount_micros=1,
            balance_after_micros=2,
            reference="same",
        )


def test_each_organization_has_its_own_wallet(workspace, owner):
    other = create_workspace(name="Other Co", owner=owner)
    wallet.top_up(workspace.organization, amount_micros=100, reference="a")
    assert wallet.get_wallet(other.organization).balance_micros == 0
    assert Wallet.objects.count() == 2


# ---- charging ---------------------------------------------------------------------------------


def test_a_sent_message_is_charged_once_with_the_rate_markup_and_segments(
    workspace, person, adapters
):
    rate(price=10_000, cost=6_000)
    workspace.organization.limits = {"markup_percent": 50}
    workspace.organization.save()
    wallet.top_up(workspace.organization, amount_micros=1_000_000, reference="seed")
    outcome = send(workspace, person, body="x" * 200)  # two segments
    settle(workspace)
    settle(workspace)  # a redelivered event must not charge again
    record = UsageRecord.objects.get(message=outcome.message)
    assert (record.units, record.cost_micros, record.price_micros, record.rated) == (
        2,
        12_000,
        30_000,
        True,
    )
    assert wallet.get_wallet(workspace.organization).balance_micros == 970_000
    assert UsageRecord.objects.count() == 1


def test_unrated_usage_is_recorded_at_zero_so_it_can_be_priced_later(workspace, person, adapters):
    outcome = send(workspace, person)
    settle(workspace)
    record = UsageRecord.objects.get(message=outcome.message)
    assert (record.rated, record.price_micros, record.rate_card_id) == (False, 0, None)
    assert wallet.get_wallet(workspace.organization).balance_micros == 0


def test_blocked_and_failed_messages_cost_nothing(workspace, person, adapters):
    rate()
    consent.handle_keyword(
        workspace_id=workspace.pk, contact_id=person.pk, channel="sms", text="STOP"
    )
    send(workspace, person)  # blocked: opted out
    from crm.channels.fake import permanent

    adapters["email"].fail_next(permanent("bad"))
    send(workspace, person, channel="email")  # rejected by the provider
    settle(workspace)
    assert UsageRecord.objects.count() == 0


def test_the_recipients_country_picks_the_rate(workspace, adapters):
    rate(price=10_000)
    rate(price=7_000, country="GB")
    uk = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"phone": "+442079460958"}
    )
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=uk.pk,
        channel="sms",
        purpose="marketing",
        status="opted_in",
        source="form",
    )
    outcome = send(workspace, uk)
    settle(workspace)
    assert UsageRecord.objects.get(message=outcome.message).price_micros == 7_000


def test_usage_survives_the_contact_being_erased(workspace, person, adapters):
    rate()
    outcome = send(workspace, person)
    message_id = outcome.message.pk
    UsageRecord.objects.filter(message_id=message_id).delete()
    contacts.erase_contact(workspace_id=workspace.pk, actor=SYSTEM, contact_id=person.pk)
    assert (
        charging.charge_message(workspace_id=workspace.pk, message_id=message_id) is None
    )  # message gone with it


def test_a_low_balance_raises_one_alert_until_topped_up(workspace, person, adapters):
    rate(price=40_000)
    w = wallet.get_wallet(workspace.organization)
    w.low_balance_micros = 50_000
    w.balance_micros = 100_000
    w.save()
    send(workspace, person)
    send(workspace, person)
    send(workspace, person)
    settle(workspace)
    assert OutboxMessage.objects.filter(topic="crm.wallet.low").count() == 1
    wallet.top_up(workspace.organization, amount_micros=1_000_000, reference="refill")
    assert Wallet.objects.get().low_alerted is False
    send(workspace, person, body="y")
    settle(workspace)


# ---- the gateway uses the money ---------------------------------------------------------------


def test_prepaid_organizations_cannot_send_without_funds(workspace, person, adapters):
    rate(price=10_000)
    workspace.organization.limits = {"billing_mode": "prepaid"}
    workspace.organization.save()
    assert send(workspace, person).reason == "insufficient_funds" and adapters["sms"].sent == []
    wallet.top_up(workspace.organization, amount_micros=10_000, reference="p")
    assert send(workspace, person).blocked is False
    settle(workspace)
    assert (
        send(workspace, person, body="again").reason == "insufficient_funds"
    )  # balance is now zero


def test_postpaid_organizations_are_never_blocked_for_money(workspace, person, adapters):
    rate(price=10_000)
    assert send(workspace, person).blocked is False
    settle(workspace)
    assert wallet.get_wallet(workspace.organization).balance_micros == -10_000


def test_the_daily_spend_cap_stops_further_sends(workspace, person, adapters):
    rate(price=10_000)
    workspace.organization.limits = {"daily_spend_cap_micros": 25_000}
    workspace.organization.save()
    for i in range(2):
        assert send(workspace, person, body=f"m{i}").blocked is False
        settle(workspace)
    assert (
        send(workspace, person, body="third").reason == "spend_cap_reached"
    )  # 30_000 would pass the cap


# ---- API and commands -------------------------------------------------------------------------


def api(client, path, workspace):
    return client.get(f"/api/v1/crm/usage{path}", HTTP_X_WORKSPACE_ID=str(workspace.pk))


def test_api_shows_the_wallet_summary_and_records_without_our_cost(
    workspace, owner, person, adapters
):
    rate(price=10_000, cost=1)
    wallet.top_up(workspace.organization, amount_micros=100_000, reference="seed")
    send(workspace, person)
    send(workspace, person, channel="email")
    settle(workspace)
    admin = Client()
    admin.force_login(owner)
    info = api(admin, "/wallet", workspace).json()
    assert (
        info["balance_micros"] == 90_000
        and info["billing_mode"] == "postpaid"
        and info["spent_today_micros"] == 10_000
    )
    summary = api(admin, "/summary", workspace).json()
    assert {s["channel"]: (s["messages"], s["units"], s["price_micros"]) for s in summary} == {
        "sms": (1, 1, 10_000),
        "email": (1, 1, 0),
    }
    assert (
        api(
            admin,
            "/summary?since="
            + (timezone.now() + timedelta(days=1)).isoformat().replace("+", "%2B"),
            workspace,
        ).json()
        == []
    )
    page = api(admin, "/records?limit=1", workspace).json()
    assert len(page["items"]) == 1 and page["next_cursor"] and "cost_micros" not in json.dumps(page)


def test_api_permissions_and_tenants(workspace, owner, person, adapters):
    rate()
    send(workspace, person)
    settle(workspace)
    viewer = Client()
    viewer.force_login(make_user("v@example.com", workspace, Role.VIEWER))
    assert api(viewer, "/wallet", workspace).status_code == 403
    other = create_workspace(name="Other Co", owner=owner)
    admin = Client()
    admin.force_login(owner)
    assert api(admin, "/summary", other).json() == []
    assert api(admin, "/records", other).json()["items"] == []
    assert api(admin, "/wallet", other).json()["balance_micros"] == 0


def test_commands_add_rates_and_credit_wallets_once(workspace, capsys):
    call_command(
        "crm_set_rate",
        channel="sms",
        currency="cad",
        cost_micros=6000,
        price_micros=9000,
        country="ca",
    )
    row = RateCard.objects.get()
    assert (row.country, row.currency, row.price_micros) == ("CA", "CAD", 9000)
    slug = workspace.organization.slug
    call_command("crm_wallet_topup", organization=slug, amount_micros=2_000_000, reference="inv-1")
    call_command("crm_wallet_topup", organization=slug, amount_micros=2_000_000, reference="inv-1")
    assert wallet.get_wallet(workspace.organization).balance_micros == 2_000_000
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("crm_wallet_topup", organization="nope", amount_micros=1, reference="x")
    with pytest.raises(CommandError):
        call_command(
            "crm_set_rate",
            channel="sms",
            currency="cad",
            cost_micros=6000,
            price_micros=9000,
            country="ca",
            effective_from=row.effective_from.isoformat(),
        )
    _ = capsys
