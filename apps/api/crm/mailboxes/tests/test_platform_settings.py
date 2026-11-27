"""The platform panel keeps the Google and Microsoft app keys; sign-in reads them from there."""

import json
from urllib.parse import parse_qs, urlparse

import pytest
from django.db import connection
from django.test import Client

from audit.models import AuditEvent
from conftest import PASSWORD, make_user
from crm.mailboxes import oauth, platform_config
from crm.mailboxes.models import PlatformOAuthSettings
from identity.models import User
from workspaces.models import Role

pytestmark = pytest.mark.django_db
URL = "/api/v1/platform/mailbox-oauth"
JSON = "application/json"
SECRET = "g-secret-value-123"


@pytest.fixture(autouse=True)
def mfa_done(monkeypatch):
    monkeypatch.setattr("identity.mfa.mfa_enabled", lambda user: True)


@pytest.fixture(autouse=True)
def no_environment(settings):
    settings.CRM_GOOGLE_CLIENT_ID = settings.CRM_GOOGLE_CLIENT_SECRET = ""
    settings.CRM_MICROSOFT_CLIENT_ID = settings.CRM_MICROSOFT_CLIENT_SECRET = ""
    settings.CRM_MAILBOX_REDIRECT_URI = settings.CRM_MAILBOX_RETURN_URL = ""
    settings.CRM_MICROSOFT_TENANT = "common"


@pytest.fixture
def admin(db):
    user = User.objects.create_superuser("root@example.com", PASSWORD)
    client = Client()
    client.force_login(user)
    return client


def put(client, **body):
    return client.put(URL, json.dumps(body), content_type=JSON)


FULL = {
    "google_client_id": "gid.apps.test",
    "google_client_secret": SECRET,
    "microsoft_client_id": "ms-app-id",
    "microsoft_client_secret": "ms-secret-value",
    "microsoft_tenant": "contoso.onmicrosoft.com",
    "redirect_uri": "https://app.test/bff/api/v1/crm-public/mailboxes/oauth/callback",
    "return_url": "https://app.test/crm/email",
}


def test_nothing_is_set_at_first(admin):
    body = admin.get(URL).json()
    assert body["google"] == {"client_id": "", "secret_set": False, "source": "none"}
    assert body["microsoft"]["source"] == "none" and oauth.available() == []


def test_saved_keys_switch_sign_in_on_and_secrets_are_never_returned(admin):
    response = put(admin, **FULL)
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["google"] == {
        "client_id": "gid.apps.test",
        "secret_set": True,
        "source": "database",
    }
    assert body["microsoft_tenant"] == "contoso.onmicrosoft.com"
    assert (
        SECRET not in response.content.decode()
        and "ms-secret-value" not in response.content.decode()
    )
    assert SECRET not in admin.get(URL).content.decode()
    assert oauth.available() == ["google", "microsoft"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT google_client_secret_ciphertext FROM crm_mailboxes_platformoauthsettings"
        )
        assert SECRET.encode() not in bytes(cursor.fetchone()[0])
    assert platform_config.current().google_client_secret == SECRET


def test_the_authorize_address_uses_the_saved_values(admin, workspace, owner):
    put(admin, **FULL)
    url = oauth.start(
        workspace_id=workspace.pk, user_id=owner.pk, provider="microsoft", sync=False, shared=False
    )
    assert urlparse(url).path == "/contoso.onmicrosoft.com/oauth2/v2.0/authorize"
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["ms-app-id"] and query["redirect_uri"] == [FULL["redirect_uri"]]


def test_fields_left_out_are_kept_and_a_secret_can_be_cleared(admin):
    put(admin, **FULL)
    again = put(admin, google_client_id="other-id").json()
    assert again["google"]["client_id"] == "other-id" and again["google"]["secret_set"] is True
    cleared = put(admin, google_client_secret="").json()
    assert cleared["google"]["secret_set"] is False and cleared["google"]["source"] == "none"
    assert cleared["microsoft"]["secret_set"] is True


def test_the_environment_is_the_fallback_and_the_database_wins(admin, settings):
    settings.CRM_GOOGLE_CLIENT_ID, settings.CRM_GOOGLE_CLIENT_SECRET = "env-id", "env-secret"
    settings.CRM_MAILBOX_REDIRECT_URI = "https://env.test/callback"
    body = admin.get(URL).json()
    assert (
        body["google"]["source"] == "environment"
        and body["effective_redirect_uri"] == "https://env.test/callback"
    )
    assert oauth.available() == ["google"]
    put(admin, google_client_id="db-id", google_client_secret="db-secret")
    assert admin.get(URL).json()["google"] == {
        "client_id": "db-id",
        "secret_set": True,
        "source": "database",
    }
    assert platform_config.current().google_client_secret == "db-secret"
    put(admin, google_client_id="", google_client_secret="")
    assert admin.get(URL).json()["google"]["source"] == "environment"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("redirect_uri", "javascript:alert(1)"),
        ("redirect_uri", "http://evil.test/cb"),
        ("return_url", "https://user:pw@app.test/x"),
        ("return_url", "not a url"),
        ("microsoft_tenant", "bad tenant!"),
        ("google_client_id", "x" * 300),
    ],
)
def test_bad_values_are_refused_and_nothing_is_saved(admin, field, value):
    response = put(admin, **{field: value})
    assert response.status_code == 422
    assert not PlatformOAuthSettings.objects.exists()


def test_local_http_is_allowed_for_development(admin):
    assert put(admin, redirect_uri="http://localhost:3003/bff/cb").status_code == 200


def test_only_a_platform_administrator_may_use_it(workspace, owner):
    for user in (owner, make_user("viewer@example.com", workspace, Role.VIEWER)):
        client = Client()
        client.force_login(user)
        assert client.get(URL).status_code == 404
        assert put(client, **FULL).status_code == 404
    assert Client().get(URL).status_code in {401, 403}
    assert not PlatformOAuthSettings.objects.exists()


def test_a_change_is_audited_without_any_secret(admin):
    put(admin, **FULL)
    event = AuditEvent.objects.filter(action="platform.mailbox_oauth_updated").get()
    assert "google_client_secret" in event.after["fields"]
    assert SECRET not in json.dumps(event.after)
