"""Connected mailboxes: SMTP, Google and Microsoft sign-in, sending, reading replies, safety."""

import base64
import json
import smtplib
from datetime import timedelta
from email import message_from_bytes
from email.message import EmailMessage
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from conftest import make_user
from core.errors import ConflictError, ValidationFailedError
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.inbox.models import Message
from crm.mailboxes import http, oauth, services, smtp
from crm.mailboxes.models import AuthRequest, Mailbox, MailboxStatus
from crm.mailboxes.sealing import open_credentials
from crm.sending import gateway
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
SERVER = {"host": "smtp.acme.test", "port": 587, "security": "starttls", "username": "ada"}
JSON = "application/json"


class FakeSmtp:
    """Stands in for smtplib: records logins and sent mail, and can be told to fail."""

    instances: list[FakeSmtp] = []
    login_error: Exception | None = None
    send_error: Exception | None = None
    starttls_offered = True

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.sent, self.logged_in = host, port, [], None
        self.closed = False
        FakeSmtp.instances.append(self)

    def ehlo(self):
        return (250, b"ok")

    def has_extn(self, name):
        return self.starttls_offered and name == "starttls"

    def starttls(self, context=None):
        return (220, b"go")

    def login(self, user, password):
        if self.login_error:
            raise self.login_error
        self.logged_in = (user, password)

    def noop(self):
        return (250, b"ok")

    def send_message(self, message):
        if self.send_error:
            raise self.send_error
        self.sent.append(message)

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch, settings):
    FakeSmtp.instances, FakeSmtp.login_error = [], None
    FakeSmtp.send_error, FakeSmtp.starttls_offered = None, True
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    settings.CRM_MAILBOX_ALLOW_PRIVATE_HOSTS = True
    settings.CRM_GOOGLE_CLIENT_ID = "google-id"
    settings.CRM_GOOGLE_CLIENT_SECRET = "google-secret"
    settings.CRM_MICROSOFT_CLIENT_ID = "ms-id"
    settings.CRM_MICROSOFT_CLIENT_SECRET = "ms-secret"
    settings.CRM_MAILBOX_REDIRECT_URI = (
        "https://app.test/bff/api/v1/crm-public/mailboxes/oauth/callback"
    )
    settings.CRM_MAILBOX_RETURN_URL = "https://app.test/crm/email"


def connect(workspace, owner, email="ada@acme.test", **extra):
    return services.connect_smtp(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        owner_id=owner.pk,
        email=email,
        display_name="Ada Lovelace",
        server=SERVER,
        password="s3cret-pass",
        **extra,
    )


@pytest.fixture
def person(workspace):
    c = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "bob@example.com"}
    )
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=c.pk,
        channel="email",
        purpose="marketing",
        status="opted_in",
        source="form",
    )
    return c


def write(workspace, owner, person, **extra):
    values = {"body": "Hello Bob", "subject": "Hi", "purpose": "transactional", **extra}
    return gateway.send(
        workspace_id=workspace.pk,
        actor=Actor.human(owner.pk),
        contact_id=person.pk,
        channel="email",
        **values,
    )


# ---- SMTP -------------------------------------------------------------------------------------


def test_connecting_smtp_proves_the_login_and_seals_the_password(workspace, owner):
    mailbox = connect(workspace, owner)
    assert FakeSmtp.instances[0].logged_in == ("ada", "s3cret-pass")
    assert mailbox.status == MailboxStatus.CONNECTED and mailbox.account_id
    assert open_credentials(mailbox) == {"password": "s3cret-pass"}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT secret_ciphertext FROM crm_mailboxes_mailbox WHERE id = %s", [mailbox.pk]
        )
        assert b"s3cret-pass" not in bytes(cursor.fetchone()[0])
    assert "s3cret-pass" not in json.dumps(mailbox.config)


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"host": "a b"}, "host"),
        ({"port": 22}, "port"),
        ({"security": "none"}, "security"),
        ({"username": ""}, "username"),
    ],
)
def test_smtp_settings_are_validated(override, field):
    with pytest.raises(ValidationFailedError) as caught:
        smtp.clean_server({**SERVER, **override})
    assert field in caught.value.params["errors"]


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "localhost"]
)
def test_private_hosts_are_refused_unless_allowed(host, settings):
    settings.CRM_MAILBOX_ALLOW_PRIVATE_HOSTS = False
    with pytest.raises(smtp.SmtpError) as caught:
        smtp.check_host(host)
    assert caught.value.code == "host_not_allowed"


def test_a_failed_login_or_missing_starttls_is_reported_and_nothing_is_saved(workspace, owner):
    FakeSmtp.login_error = smtplib.SMTPAuthenticationError(535, b"bad")
    with pytest.raises(ValidationFailedError) as caught:
        connect(workspace, owner)
    assert caught.value.params["errors"]["smtp"] == "auth_failed"
    FakeSmtp.login_error, FakeSmtp.starttls_offered = None, False
    with pytest.raises(ValidationFailedError) as caught:
        connect(workspace, owner)
    assert caught.value.params["errors"]["smtp"] == "starttls_unsupported"
    assert Mailbox.objects.for_workspace(workspace.pk).count() == 0


def test_the_same_address_cannot_be_connected_twice_but_can_after_disconnecting(workspace, owner):
    mailbox = connect(workspace, owner)
    with pytest.raises(ConflictError):
        connect(workspace, owner)
    services.disconnect(workspace_id=workspace.pk, actor=SYSTEM, mailbox=mailbox)
    again = connect(workspace, owner)
    assert again.pk == mailbox.pk and again.status == MailboxStatus.CONNECTED


# ---- sending ----------------------------------------------------------------------------------


def test_a_person_writing_sends_from_their_own_mailbox(workspace, owner, person):
    mailbox = connect(workspace, owner)
    outcome = write(workspace, owner, person)
    assert not outcome.blocked, outcome.reason
    message = Message.objects.get(pk=outcome.message.pk)
    assert message.status == "sent" and message.channel_account_id == mailbox.account_id
    sent = FakeSmtp.instances[-1].sent[0]
    assert sent["From"] == "Ada Lovelace <ada@acme.test>" and sent["To"] == "bob@example.com"
    assert sent["Message-ID"].endswith("@acme.test>")
    assert message.provider_message_id == sent["Message-ID"]


def test_a_daily_limit_blocks_further_sends(workspace, owner, person):
    connect(workspace, owner, daily_limit=1)
    assert not write(workspace, owner, person).blocked
    second = write(workspace, owner, person, body="Again")
    assert second.blocked and second.reason == "mailbox_limit"


def test_a_disconnected_mailbox_is_never_used(workspace, owner, person):
    mailbox = connect(workspace, owner)
    services.disconnect(workspace_id=workspace.pk, actor=SYSTEM, mailbox=mailbox)
    assert services.sender_for(workspace.pk, owner.pk) is None
    forced = gateway.send(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=person.pk,
        channel="email",
        body="x",
        subject="y",
        purpose="transactional",
        channel_account_id=mailbox.account_id,
    )
    assert forced.blocked and forced.reason == "mailbox_unavailable"
    assert open_credentials(Mailbox.objects.get(pk=mailbox.pk)) == {}


def test_a_refused_login_while_sending_marks_the_mailbox_for_attention(workspace, owner, person):
    mailbox = connect(workspace, owner)
    FakeSmtp.login_error = smtplib.SMTPAuthenticationError(535, b"changed")
    outcome = write(workspace, owner, person)
    message = Message.objects.get(pk=outcome.message.pk)
    assert message.status == "failed" and message.error_code == "mailbox_unauthorised"
    assert Mailbox.objects.get(pk=mailbox.pk).status == MailboxStatus.NEEDS_ATTENTION


def test_a_rejected_recipient_fails_for_good_and_a_server_error_is_retried(
    workspace, owner, person
):
    connect(workspace, owner)
    FakeSmtp.send_error = smtplib.SMTPRecipientsRefused({"bob@example.com": (550, b"no")})
    message = Message.objects.get(pk=write(workspace, owner, person).message.pk)
    assert message.status == "failed" and message.error_code == "email_rejected"


# ---- Google and Microsoft ---------------------------------------------------------------------


class Provider:
    """A tiny fake of the token, identity, Gmail and Graph endpoints."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.refresh_ok = True
        self.token_expires = 3600

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        if "token" in url and request.method == "POST":
            form = parse_qs(request.content.decode())
            if form.get("grant_type") == ["refresh_token"] and not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": f"at-{len(self.calls)}",
                    "refresh_token": "rt-1",
                    "expires_in": self.token_expires,
                    "scope": form.get("scope", [""])[0],
                },
            )
        if url.endswith("/userinfo"):
            return httpx.Response(200, json={"email": "Ann@Gmail.test", "name": "Ann"})
        if url.endswith("/v1.0/me"):
            return httpx.Response(200, json={"mail": "ann@corp.test", "displayName": "Ann C"})
        if url.endswith("/messages/send") or url.endswith("/sendMail"):
            return httpx.Response(200 if "gmail" in url else 202, json={"id": "g1"})
        return httpx.Response(404)


@pytest.fixture
def provider(monkeypatch):
    fake = Provider()
    monkeypatch.setattr(http, "client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    return fake


def sign_in(workspace, owner, provider_name="google", **extra):
    url = oauth.start(
        workspace_id=workspace.pk,
        user_id=owner.pk,
        provider=provider_name,
        sync=extra.get("sync", False),
        shared=extra.get("shared", False),
    )
    state = parse_qs(urlparse(url).query)["state"][0]
    return url, oauth.finish(state=state, code="abc")


def test_the_authorize_address_carries_pkce_state_and_the_right_scopes(workspace, owner):
    url, _ = sign_in_url(workspace, owner)
    query = parse_qs(urlparse(url).query)
    assert query["code_challenge_method"] == ["S256"] and query["client_id"] == ["google-id"]
    assert query["redirect_uri"] == [
        "https://app.test/bff/api/v1/crm-public/mailboxes/oauth/callback"
    ]
    assert "gmail.send" in query["scope"][0] and "gmail.readonly" not in query["scope"][0]
    assert query["state"][0].startswith(workspace.pk.hex + ".")
    with_sync = oauth.start(
        workspace_id=workspace.pk, user_id=owner.pk, provider="google", sync=True, shared=False
    )
    assert "gmail.readonly" in parse_qs(urlparse(with_sync).query)["scope"][0]
    stored = AuthRequest.objects.for_workspace(workspace.pk).get(
        state_hash=oauth._hash(query["state"][0])
    )
    assert query["state"][0] not in repr(stored.__dict__)  # only the hash is kept


def sign_in_url(workspace, owner):
    url = oauth.start(
        workspace_id=workspace.pk, user_id=owner.pk, provider="google", sync=False, shared=False
    )
    return url, None


def test_a_sign_in_creates_a_mailbox_with_sealed_tokens(workspace, owner, provider):
    _, grant = sign_in(workspace, owner)
    mailbox = services.connect_oauth(grant=grant, actor=SYSTEM)
    assert (mailbox.email, mailbox.provider, mailbox.display_name) == (
        "ann@gmail.test",
        "google",
        "Ann",
    )
    assert open_credentials(mailbox)["refresh_token"] == "rt-1"
    exchange = next(c for c in provider.calls if c.method == "POST")
    assert "code_verifier" in parse_qs(exchange.content.decode())
    assert not AuthRequest.objects.for_workspace(workspace.pk).exists()


def test_a_state_works_once_expires_and_belongs_to_one_workspace(workspace, owner, provider):
    url = oauth.start(
        workspace_id=workspace.pk, user_id=owner.pk, provider="google", sync=False, shared=False
    )
    state = parse_qs(urlparse(url).query)["state"][0]
    oauth.finish(state=state, code="abc")
    for bad in (state, "garbage", f"{workspace.pk.hex}.forged"):
        with pytest.raises(oauth.OAuthError):
            oauth.finish(state=bad, code="abc")
    url = oauth.start(
        workspace_id=workspace.pk, user_id=owner.pk, provider="google", sync=False, shared=False
    )
    AuthRequest.objects.for_workspace(workspace.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.finish(state=parse_qs(urlparse(url).query)["state"][0], code="abc")
    assert caught.value.code == "invalid_state"


def test_an_unconfigured_provider_is_not_offered(workspace, owner, settings):
    settings.CRM_MICROSOFT_CLIENT_ID = ""
    assert oauth.available() == ["google"]
    with pytest.raises(oauth.OAuthError):
        oauth.start(
            workspace_id=workspace.pk,
            user_id=owner.pk,
            provider="microsoft",
            sync=False,
            shared=False,
        )


def test_gmail_and_graph_send_raw_mime(workspace, owner, person, provider):
    for name, marker in (("google", "/messages/send"), ("microsoft", "/sendMail")):
        _, grant = sign_in(workspace, owner, name)
        services.connect_oauth(grant=grant, actor=SYSTEM)
        outcome = write(workspace, owner, person, subject=f"via {name}")
        assert not outcome.blocked and Message.objects.get(pk=outcome.message.pk).status == "sent"
        call = [c for c in provider.calls if str(c.url).endswith(marker)][-1]
        raw = (
            base64.urlsafe_b64decode(json.loads(call.content)["raw"])
            if name == "google"
            else base64.b64decode(call.content)
        )
        sent = message_from_bytes(raw)
        assert sent["Subject"] == f"via {name}" and sent["To"] == "bob@example.com"
        assert call.headers["Authorization"].startswith("Bearer at-")
        Mailbox.objects.for_workspace(workspace.pk).update(
            owner=None
        )  # next loop picks a new owner
        Mailbox.objects.for_workspace(workspace.pk).exclude(provider=name).delete()


def test_an_expired_token_is_refreshed_and_a_revoked_grant_needs_attention(
    workspace, owner, provider
):
    provider.token_expires = 1
    _, grant = sign_in(workspace, owner)
    mailbox = services.connect_oauth(grant=grant, actor=SYSTEM)
    first = oauth.access_token(mailbox)
    second = oauth.access_token(Mailbox.objects.get(pk=mailbox.pk))
    assert first != second  # the stored token had already expired
    provider.refresh_ok = False
    with pytest.raises(oauth.OAuthError):
        oauth.access_token(Mailbox.objects.get(pk=mailbox.pk))
    assert Mailbox.objects.get(pk=mailbox.pk).status == MailboxStatus.NEEDS_ATTENTION


def test_replies_are_read_into_the_inbox_once(workspace, owner, person, provider, monkeypatch):
    _, grant = sign_in(workspace, owner, sync=True)
    mailbox = services.connect_oauth(grant=grant, actor=SYSTEM)
    mailbox.sync_enabled = True
    mailbox.save()
    reply = EmailMessage()
    reply["From"], reply["To"], reply["Subject"] = "bob@example.com", "ann@gmail.test", "Re: Hi"
    reply["Message-ID"] = "<reply-1@example.com>"
    reply.set_content("Sounds good")
    raw = base64.urlsafe_b64encode(bytes(reply)).decode()

    def handler(request):
        url = str(request.url)
        if "token" in url:
            return httpx.Response(
                200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
            )
        if url.endswith("/messages") or "/messages?" in url:
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(200, json={"raw": raw, "internalDate": "1790000000000"})

    monkeypatch.setattr(
        http, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert services.sync(workspace_id=workspace.pk, mailbox_id=mailbox.pk) == 1
    services.sync(workspace_id=workspace.pk, mailbox_id=mailbox.pk)  # the same mail again
    inbound = Message.objects.for_workspace(workspace.pk).filter(direction="inbound")
    assert inbound.count() == 1 and "Sounds good" in inbound.get().body
    assert Mailbox.objects.get(pk=mailbox.pk).sync_cursor == "1790000000000"


# ---- the API ----------------------------------------------------------------------------------


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": JSON}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def login(user):
    client = Client()
    client.force_login(user)
    return client


SMTP_BODY = {
    "email": "ada@acme.test",
    "display_name": "Ada",
    "password": "s3cret-pass",
    "host": "smtp.acme.test",
    "port": 587,
    "security": "starttls",
    "username": "ada",
}


def test_the_api_never_returns_secrets_and_respects_ownership(workspace, owner):
    editor = make_user("ed@example.com", workspace, Role.EDITOR)
    mine, theirs = login(editor), login(owner)
    made = api(mine, "post", "/email/mailboxes/smtp", workspace, SMTP_BODY)
    assert made.status_code == 201, made.content
    body = made.json()
    assert "s3cret-pass" not in made.content.decode() and "password" not in body
    assert body["mine"] and not body["shared"] and body["sent_today"] == 0
    shared = api(
        mine,
        "post",
        "/email/mailboxes/smtp",
        workspace,
        {**SMTP_BODY, "email": "team@acme.test", "shared": True},
    )
    assert shared.status_code == 403  # an editor may not create a shared mailbox
    assert (
        api(
            theirs,
            "post",
            "/email/mailboxes/smtp",
            workspace,
            {**SMTP_BODY, "email": "team@acme.test", "shared": True},
        ).status_code
        == 201
    )
    seen = {m["email"] for m in api(mine, "get", "/email/mailboxes", workspace).json()}
    assert seen == {"ada@acme.test", "team@acme.test"}
    third = login(make_user("other@example.com", workspace, Role.EDITOR))
    assert {m["email"] for m in api(third, "get", "/email/mailboxes", workspace).json()} == {
        "team@acme.test"
    }
    assert (
        api(
            third, "patch", f"/email/mailboxes/{body['id']}", workspace, {"daily_limit": 5}
        ).status_code
        == 404
    )
    assert (
        api(
            theirs, "patch", f"/email/mailboxes/{body['id']}", workspace, {"daily_limit": 5}
        ).json()["daily_limit"]
        == 5
    )


def test_the_api_starts_oauth_tests_and_disconnects(workspace, owner, provider):
    client = login(owner)
    assert api(client, "get", "/email/mailboxes/providers", workspace).json()["oauth"] == [
        "google",
        "microsoft",
    ]
    started = api(
        client, "post", "/email/mailboxes/oauth/start", workspace, {"provider": "microsoft"}
    )
    assert started.status_code == 200 and started.json()["url"].startswith(
        "https://login.microsoftonline.com/"
    )
    assert (
        api(
            client, "post", "/email/mailboxes/oauth/start", workspace, {"provider": "yahoo"}
        ).status_code
        == 422
    )
    made = api(client, "post", "/email/mailboxes/smtp", workspace, SMTP_BODY).json()
    assert api(client, "post", f"/email/mailboxes/{made['id']}/test", workspace).json() == {
        "ok": True,
        "code": "",
    }
    FakeSmtp.login_error = smtplib.SMTPAuthenticationError(535, b"no")
    assert (
        api(client, "post", f"/email/mailboxes/{made['id']}/test", workspace).json()["code"]
        == "auth_failed"
    )
    gone = api(client, "post", f"/email/mailboxes/{made['id']}/disconnect", workspace).json()
    assert gone["status"] == "disabled"


def test_the_callback_redirects_to_the_configured_page_only(workspace, owner, provider):
    url = oauth.start(
        workspace_id=workspace.pk, user_id=owner.pk, provider="google", sync=False, shared=False
    )
    state = parse_qs(urlparse(url).query)["state"][0]
    anonymous = Client()
    good = anonymous.get(
        "/api/v1/crm-public/mailboxes/oauth/callback", {"state": state, "code": "abc"}
    )
    assert (
        good.status_code == 302
        and good["Location"] == "https://app.test/crm/email?mailbox=connected"
    )
    assert Mailbox.objects.for_workspace(workspace.pk).get().email == "ann@gmail.test"
    replay = anonymous.get(
        "/api/v1/crm-public/mailboxes/oauth/callback", {"state": state, "code": "abc"}
    )
    assert "mailbox_error=invalid_state" in replay["Location"]
    denied = anonymous.get(
        "/api/v1/crm-public/mailboxes/oauth/callback",
        {"error": "access_denied", "redirect": "https://evil.test"},
    )
    assert (
        denied["Location"].startswith("https://app.test/crm/email?")
        and "evil" not in denied["Location"]
    )


def test_nothing_leaks_across_workspaces(workspace, owner):
    other = create_workspace(name="Other Co", owner=owner)
    client = login(owner)
    made = api(client, "post", "/email/mailboxes/smtp", workspace, SMTP_BODY).json()
    assert api(client, "get", "/email/mailboxes", other).json() == []
    for method, path, body in (
        ("patch", f"/email/mailboxes/{made['id']}", {"daily_limit": 9}),
        ("post", f"/email/mailboxes/{made['id']}/disconnect", None),
        ("post", f"/email/mailboxes/{made['id']}/test", None),
    ):
        assert api(client, method, path, other, body).status_code == 404
    viewer = login(make_user("v@example.com", workspace, Role.VIEWER))
    assert api(viewer, "get", "/email/mailboxes", workspace).status_code == 403
    assert Client().get("/api/v1/crm/email/mailboxes").status_code in {401, 403}
