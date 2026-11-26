import json

import pytest
from django.test import Client, override_settings

from audit.actors import Actor
from conftest import make_user
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.channels.fake import FakeAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.inbox.models import ChannelAccount
from crm.sending import domains, gateway, registry
from crm.sending.dns import ResolverUnavailableError
from crm.sending.models import DomainStatus, RecordStatus
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
PROFILE = {
    "spf_include": "_spf.mailer.test",
    "dkim": {"selector": "itb1", "cname_target": "itb1.dkim.mailer.test"},
    "return_path": {"host": "bounce", "target": "bounce.mailer.test"},
    "tracking": {"host": "link", "target": "track.mailer.test"},
    "mx": {"host": "inbound", "target": "mx.mailer.test", "priority": 10},
    "dmarc": {"policy": "none", "report_to": "dmarc@acme.test"},
}


class FakeResolver:
    def __init__(self, txt=None, cname=None, mx=None, down=False):
        self._txt, self._cname, self._mx, self.down = txt or {}, cname or {}, mx or {}, down

    def _get(self, table, name):
        if self.down:
            raise ResolverUnavailableError("timeout")
        return table.get(name, [])

    def txt(self, name):
        return self._get(self._txt, name)

    def cname(self, name):
        return self._get(self._cname, name)

    def mx(self, name):
        return self._get(self._mx, name)


def published(domain="mail.acme.test"):
    return FakeResolver(
        txt={
            domain: ["v=spf1 include:_spf.mailer.test include:other.test ~all"],
            f"_dmarc.{domain}": ["v=DMARC1; p=reject"],
        },
        cname={
            f"itb1._domainkey.{domain}": ["itb1.dkim.mailer.test."],
            f"bounce.{domain}": ["bounce.mailer.test"],
            f"link.{domain}": ["TRACK.mailer.test"],
        },
        mx={f"inbound.{domain}": ["mx.mailer.test."]},
    )


@pytest.fixture(autouse=True)
def profile(settings):
    settings.CRM_EMAIL_DNS_PROFILE = PROFILE


def add(workspace, domain="mail.acme.test"):
    return domains.add_domain(workspace_id=workspace.pk, actor=SYSTEM, domain=domain)


# ---- the plan ---------------------------------------------------------------------------------


def test_the_plan_lists_every_record_for_the_domain(workspace):
    row = add(workspace)
    by_purpose = {r.purpose: r for r in row.records.all()}
    assert set(by_purpose) == {"spf", "dkim", "return_path", "tracking", "mx", "dmarc"}
    assert by_purpose["spf"].value == "v=spf1 include:_spf.mailer.test ~all"
    assert by_purpose["dkim"].name == "itb1._domainkey.mail.acme.test"
    assert (by_purpose["mx"].priority, by_purpose["mx"].value) == (10, "mx.mailer.test")
    assert by_purpose["dmarc"].value == "v=DMARC1; p=none; rua=mailto:dmarc@acme.test"
    assert by_purpose["dmarc"].required is False and by_purpose["spf"].required is True


def test_records_follow_the_profile_and_an_empty_profile_has_only_dmarc(workspace, settings):
    settings.CRM_EMAIL_DNS_PROFILE = {}
    row = add(workspace)
    assert [r.purpose for r in row.records.all()] == ["dmarc"]
    verified = domains.verify(workspace_id=workspace.pk, domain_id=row.pk, resolver=FakeResolver())
    assert verified.status == DomainStatus.PENDING  # nothing required, so nothing to prove


@pytest.mark.parametrize(
    "bad", ["", "nodot", "http://x.test", "a b.test", "-x.test", "x..test", "a" * 70 + ".test"]
)
def test_invalid_domains_are_rejected(workspace, bad):
    with pytest.raises(ValidationFailedError):
        add(workspace, bad)


def test_domains_are_normalised_and_cannot_be_claimed_twice(workspace, owner):
    assert add(workspace, " Mail.Acme.TEST. ").domain == "mail.acme.test"
    with pytest.raises(ConflictError):
        add(workspace, "mail.acme.test")
    other = create_workspace(name="Other Co", owner=owner)
    with pytest.raises(ConflictError):
        add(other, "mail.acme.test")  # another tenant cannot take it over


# ---- verification -----------------------------------------------------------------------------


def test_published_records_verify_the_domain(workspace):
    row = add(workspace)
    done = domains.verify(workspace_id=workspace.pk, domain_id=row.pk, resolver=published())
    assert done.status == DomainStatus.VERIFIED and done.verified_at and done.last_checked_at
    assert {r.status for r in done.records.all()} == {RecordStatus.OK}
    assert domains.is_verified(workspace.pk, "MAIL.acme.test")


def test_missing_and_wrong_records_are_reported_one_by_one(workspace):
    row = add(workspace)
    resolver = published()
    resolver._cname.pop("itb1._domainkey.mail.acme.test")
    resolver._cname["bounce.mail.acme.test"] = ["somewhere.else.test"]
    resolver._txt["mail.acme.test"] = ["v=spf1 include:other.test ~all"]
    done = domains.verify(workspace_id=workspace.pk, domain_id=row.pk, resolver=resolver)
    status = {r.purpose: (r.status, r.observed) for r in done.records.all()}
    assert status["dkim"] == (RecordStatus.MISSING, "")
    assert status["return_path"] == (RecordStatus.MISMATCH, "somewhere.else.test")
    assert status["spf"][0] == RecordStatus.MISMATCH
    assert done.status == DomainStatus.PENDING and not domains.is_verified(
        workspace.pk, "mail.acme.test"
    )


def test_dmarc_is_advice_only(workspace):
    row = add(workspace)
    resolver = published()
    resolver._txt.pop("_dmarc.mail.acme.test")
    assert (
        domains.verify(workspace_id=workspace.pk, domain_id=row.pk, resolver=resolver).status
        == DomainStatus.VERIFIED
    )


def test_a_verified_domain_fails_when_records_disappear_and_recovers(workspace):
    row = add(workspace)
    domains.verify(workspace_id=workspace.pk, domain_id=row.pk, resolver=published())
    broken = domains.verify(workspace_id=workspace.pk, domain_id=row.pk, resolver=FakeResolver())
    assert broken.status == DomainStatus.FAILED
    healed = domains.verify(workspace_id=workspace.pk, domain_id=row.pk, resolver=published())
    assert healed.status == DomainStatus.VERIFIED


def test_a_dns_outage_never_unverifies_a_domain(workspace):
    row = add(workspace)
    domains.verify(workspace_id=workspace.pk, domain_id=row.pk, resolver=published())
    during = domains.verify(
        workspace_id=workspace.pk, domain_id=row.pk, resolver=FakeResolver(down=True)
    )
    assert during.status == DomainStatus.VERIFIED
    assert {r.status for r in during.records.all()} == {RecordStatus.UNKNOWN}


def test_the_recheck_job_covers_every_domain(workspace, monkeypatch):
    from crm.sending import tasks

    row = add(workspace)
    monkeypatch.setattr("crm.sending.domains.SystemResolver", published)
    assert tasks.recheck_domains_all.apply().get() == 1
    row.refresh_from_db()
    assert row.status == DomainStatus.VERIFIED


# ---- the gateway uses it ----------------------------------------------------------------------


def test_mail_from_an_unverified_domain_is_blocked_until_it_verifies(workspace):
    registry.register("email", lambda: FakeAdapter("email"))
    person = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "ada@example.com"}
    )
    account = ChannelAccount.objects.create(
        workspace_id=workspace.pk,
        channel="email",
        provider="smtp",
        external_id="hello@mail.acme.test",
    )
    send = lambda: gateway.send(  # noqa: E731
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=person.pk,
        channel="email",
        body="Hi",
        channel_account_id=account.pk,
    )
    assert send().reason == "domain_unverified"
    row = add(workspace)
    assert send().reason == "domain_unverified"  # added but not yet proven
    domains.verify(workspace_id=workspace.pk, domain_id=row.pk, resolver=published())
    assert send().blocked is False
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=person.pk,
        channel="email",
        purpose="marketing",
        status="opted_in",
        source="form",
    )
    plain = gateway.send(
        workspace_id=workspace.pk, actor=SYSTEM, contact_id=person.pk, channel="email", body="Hi"
    )
    assert plain.blocked is False  # no explicit sender account: the platform default is used


# ---- API --------------------------------------------------------------------------------------


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm/email/domains{path}", **kwargs)


def test_api_flow_permissions_and_tenants(workspace, owner, monkeypatch):
    monkeypatch.setattr("crm.sending.domains.SystemResolver", published)
    admin, viewer = Client(), Client()
    admin.force_login(owner)
    viewer.force_login(make_user("v@example.com", workspace, Role.VIEWER))
    created = api(admin, "post", "", workspace, {"domain": "mail.acme.test"})
    assert created.status_code == 201 and len(created.json()["records"]) == 6
    assert api(admin, "post", "", workspace, {"domain": "mail.acme.test"}).status_code == 409
    assert api(admin, "post", "", workspace, {"domain": "nope"}).status_code == 422
    domain_id = created.json()["id"]
    assert api(viewer, "post", "", workspace, {"domain": "x.acme.test"}).status_code == 403
    assert api(viewer, "get", "", workspace).json()[0]["domain"] == "mail.acme.test"
    verified = api(admin, "post", f"/{domain_id}/verify", workspace)
    assert verified.json()["status"] == "verified"
    other = create_workspace(name="Other Co", owner=owner)
    assert api(admin, "get", "", other).json() == []
    assert api(admin, "post", f"/{domain_id}/verify", other).status_code == 404
    assert api(admin, "delete", f"/{domain_id}", other).status_code == 404
    assert api(admin, "delete", f"/{domain_id}", workspace).status_code == 204
    with pytest.raises(NotFoundError):
        domains.get_domain(workspace.pk, __import__("uuid").UUID(domain_id))


def test_settings_override_is_isolated():
    with override_settings(CRM_EMAIL_DNS_PROFILE={}):
        assert domains.plan("x.acme.test", {})[0].purpose == "dmarc"
