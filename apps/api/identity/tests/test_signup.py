import json
from urllib.parse import parse_qs, urlparse

import pytest
from django.core import mail
from django.test import Client

from audit.models import AuditEvent
from conftest import PASSWORD
from identity.models import User
from workspaces.models import Membership, Role, Workspace

pytestmark = pytest.mark.django_db
BASE = "/api/v1"
STRONG = "a-long-unusual-password-9137"
BODY = {
    "email": "New.Owner@Example.com",
    "password": STRONG,
    "full_name": "New Owner",
    "organization_name": "Acme Roofing",
}


@pytest.fixture(autouse=True)
def open_signup(settings):
    settings.SIGNUP_ENABLED = True


def post(client: Client, path: str, body: dict | None = None):
    if "csrftoken" not in client.cookies:
        client.get(f"{BASE}/auth/csrf")
    return client.post(
        f"{BASE}{path}", data=json.dumps(body or {}), content_type="application/json"
    )


def link_params() -> dict[str, str]:
    link = next(line for line in mail.outbox[-1].body.splitlines() if "verify-email" in line)
    query = parse_qs(urlparse(link).query)
    return {"uid": query["uid"][0], "token": query["token"][0]}


def test_signing_up_makes_an_inactive_account_and_sends_the_link():
    res = post(Client(), "/auth/signup", BODY)
    assert res.status_code == 202
    user = User.objects.get(email="new.owner@example.com")
    assert not user.is_active and user.full_name == "New Owner"
    workspace = Workspace.objects.get(name="Acme Roofing")
    assert Membership.objects.get(workspace=workspace, user=user).role == Role.OWNER
    assert workspace.organization.plan_key == "free"
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["new.owner@example.com"]
    assert AuditEvent.objects.filter(action="auth.signed_up").exists()


def test_the_account_cannot_sign_in_until_the_address_is_confirmed():
    client = Client()
    post(client, "/auth/signup", BODY)
    refused = post(client, "/auth/login", {"email": "new.owner@example.com", "password": STRONG})
    assert refused.status_code in (401, 403)
    assert post(client, "/auth/verify-email", link_params()).status_code == 204
    assert User.objects.get(email="new.owner@example.com").is_active
    assert (
        post(
            client, "/auth/login", {"email": "new.owner@example.com", "password": STRONG}
        ).status_code
        == 200
    )


def test_the_link_works_once_for_activation_and_never_for_a_wrong_token():
    client = Client()
    post(client, "/auth/signup", BODY)
    params = link_params()
    assert post(client, "/auth/verify-email", {**params, "token": "nope"}).status_code == 422
    assert post(client, "/auth/verify-email", {"uid": "garbage", "token": "x"}).status_code == 422
    assert post(client, "/auth/verify-email", params).status_code == 204
    assert post(client, "/auth/verify-email", params).status_code == 204  # harmless to repeat
    assert AuditEvent.objects.filter(action="auth.email_verified").count() == 1


def test_an_address_in_use_gets_the_same_answer_and_an_explanation_by_email(owner):
    res = post(Client(), "/auth/signup", {**BODY, "email": owner.email})
    assert res.status_code == 202
    assert User.objects.filter(email=owner.email).count() == 1
    assert mail.outbox[-1].to == [owner.email]
    assert "already" in mail.outbox[-1].body
    assert not Workspace.objects.filter(name="Acme Roofing").exists()


def test_signing_up_again_before_confirming_sends_a_fresh_link():
    client = Client()
    post(client, "/auth/signup", BODY)
    post(client, "/auth/signup", BODY)
    assert User.objects.filter(email="new.owner@example.com").count() == 1
    assert Workspace.objects.filter(name="Acme Roofing").count() == 1
    assert len(mail.outbox) == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"password": "short"},
        {"password": PASSWORD.replace("a", "") and "password1234"},
        {"email": "not-an-email"},
        {"organization_name": "   "},
    ],
)
def test_bad_input_is_refused_and_nothing_is_created(bad):
    res = post(Client(), "/auth/signup", {**BODY, **bad})
    assert res.status_code == 422
    assert not User.objects.filter(email="new.owner@example.com").exists()
    assert mail.outbox == []


def test_two_companies_with_the_same_name_both_get_a_workspace():
    post(Client(), "/auth/signup", BODY)
    post(Client(), "/auth/signup", {**BODY, "email": "second@example.com"})
    assert Workspace.objects.filter(name="Acme Roofing").count() == 2


def test_it_is_off_unless_switched_on(settings):
    settings.SIGNUP_ENABLED = False
    assert post(Client(), "/auth/signup", BODY).status_code == 404
    assert not User.objects.filter(email="new.owner@example.com").exists()


def test_it_has_its_own_rate_limit():
    client = Client()
    statuses = [
        post(client, "/auth/signup", {**BODY, "email": f"p{i}@example.com"}).status_code
        for i in range(7)
    ]
    assert statuses[:5] == [202] * 5 and 429 in statuses[5:]
