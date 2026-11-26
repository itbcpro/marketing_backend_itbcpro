import json
import time
from datetime import timedelta
from unittest import mock

import httpx
import pyotp
import pytest
from django.core import mail
from django.test import Client, override_settings
from django.utils import timezone

from audit.models import AuditEvent
from conftest import PASSWORD, make_user
from identity import mfa
from identity.models import ApiToken, PasskeyCredential, RecoveryCode, SessionRecord, User
from identity.validators import is_breached
from notifications.email import TEMPLATES, render
from workspaces.models import Role, Workspace

pytestmark = pytest.mark.django_db
BASE = "/api/v1"


def post(client: Client, path: str, body: dict | None = None, **extra):
    if "csrftoken" not in client.cookies:
        client.get(f"{BASE}/auth/csrf")  # endpoints without a session need the CSRF cookie
    return client.post(
        f"{BASE}{path}", data=json.dumps(body or {}), content_type="application/json", **extra
    )


def login(client: Client, email: str, password: str = PASSWORD):
    return post(client, "/auth/login", {"email": email, "password": password})


def enroll_totp(client: Client) -> tuple[str, list[str]]:
    secret = post(client, "/auth/mfa/totp/begin").json()["secret"]
    code = pyotp.TOTP(secret).now()
    codes = post(client, "/auth/mfa/totp/confirm", {"code": code}).json()["recovery_codes"]
    return secret, codes


def next_code(secret: str) -> str:
    """A valid code from the next time step (accepted by the ±1 window, never used before)."""
    return pyotp.TOTP(secret).at(time.time() + 30)


# ---- lockout ---------------------------------------------------------------------------------


@override_settings(LOGIN_MAX_FAILURES=3, LOGIN_LOCK_MINUTES=10)
def test_repeated_failures_lock_the_account_without_revealing_it(owner: User) -> None:
    client = Client()
    for _ in range(3):
        assert login(client, owner.email, "wrong-password-value").status_code == 401
    owner.refresh_from_db()
    assert owner.is_locked
    assert AuditEvent.objects.filter(action="auth.account_locked").exists()
    assert any("temporarily locked" in m.subject for m in mail.outbox)

    # A wrong password while locked looks like any other failure...
    assert (
        login(client, owner.email, "still-wrong-password").json()["code"] == "invalid_credentials"
    )
    # ...only the right password reveals the lock.
    locked = login(client, owner.email)
    assert locked.status_code == 423 and locked.json()["code"] == "account_locked"


def test_success_resets_the_failure_counter(owner: User) -> None:
    client = Client()
    login(client, owner.email, "wrong-password-value")
    owner.refresh_from_db()
    assert owner.failed_login_count == 1
    assert login(client, owner.email).json()["status"] == "ok"
    owner.refresh_from_db()
    assert owner.failed_login_count == 0


def test_unknown_accounts_get_the_same_generic_error() -> None:
    response = login(Client(), "nobody@example.com", "whatever-password-1")
    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"


# ---- TOTP and recovery codes ------------------------------------------------------------------


def test_totp_enrolment_then_two_step_login(logged_in: Client, owner: User) -> None:
    secret, codes = enroll_totp(logged_in)
    assert len(codes) == 10 and mfa.mfa_enabled(owner)
    assert RecoveryCode.objects.filter(user=owner).count() == 10
    stored = bytes(owner.totp_device.ciphertext)
    assert secret.encode() not in stored

    client = Client()
    first = login(client, owner.email).json()
    assert first == {"status": "mfa_required", "me": None}
    assert client.get(f"{BASE}/auth/me").status_code == 401  # not signed in yet

    bad = post(client, "/auth/login/mfa", {"code": "000000"})
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_mfa_code"

    code = next_code(secret)
    done = post(client, "/auth/login/mfa", {"code": code})
    assert done.status_code == 200 and done.json()["me"]["email"] == owner.email
    assert client.get(f"{BASE}/auth/me").status_code == 200

    replay = Client()
    login(replay, owner.email)
    assert post(replay, "/auth/login/mfa", {"code": code}).status_code == 422


def test_recovery_codes_work_once(logged_in: Client, owner: User) -> None:
    _, codes = enroll_totp(logged_in)
    client = Client()
    login(client, owner.email)
    assert post(client, "/auth/login/mfa", {"code": codes[0].lower()}).status_code == 200

    again = Client()
    login(again, owner.email)
    assert post(again, "/auth/login/mfa", {"code": codes[0]}).status_code == 422
    assert mfa.remaining_recovery_codes(owner) == 9


def test_mfa_step_needs_a_pending_password_step() -> None:
    assert post(Client(), "/auth/login/mfa", {"code": "123456"}).status_code == 401


def test_pending_mfa_expires(logged_in: Client, owner: User) -> None:
    secret, _ = enroll_totp(logged_in)
    client = Client()
    login(client, owner.email)
    with mock.patch("identity.services.time.time", return_value=time.time() + 1000):
        assert post(client, "/auth/login/mfa", {"code": next_code(secret)}).status_code == 401


def test_disabling_totp_requires_the_password(logged_in: Client, owner: User) -> None:
    enroll_totp(logged_in)
    denied = post(logged_in, "/auth/mfa/totp/disable", {"password": "wrong-password-value"})
    assert denied.status_code == 401 and mfa.mfa_enabled(owner)
    assert post(logged_in, "/auth/mfa/totp/disable", {"password": PASSWORD}).status_code == 204
    assert not mfa.mfa_enabled(owner)


def test_second_enrolment_is_rejected_and_wrong_confirmation_fails(
    logged_in: Client, owner: User
) -> None:
    post(logged_in, "/auth/mfa/totp/begin")
    assert post(logged_in, "/auth/mfa/totp/confirm", {"code": "000000"}).status_code == 422
    enroll_totp(logged_in)
    assert post(logged_in, "/auth/mfa/totp/begin").status_code == 409


def test_recovery_codes_can_be_regenerated_with_the_password(
    logged_in: Client, owner: User
) -> None:
    _, old = enroll_totp(logged_in)
    new = post(logged_in, "/auth/mfa/recovery-codes", {"password": PASSWORD}).json()[
        "recovery_codes"
    ]
    assert len(new) == 10 and not set(old) & set(new)
    status = logged_in.get(f"{BASE}/auth/mfa").json()
    assert status == {"totp_enabled": True, "passkeys": 0, "recovery_codes_remaining": 10}


@override_settings(MFA_REQUIRED_ROLES=["owner"])
def test_roles_that_require_mfa_are_blocked_until_enrolled(
    logged_in: Client, owner: User, workspace: Workspace
) -> None:
    assert logged_in.get(f"{BASE}/auth/me").json()["mfa_enrollment_required"] is True
    blocked = logged_in.get(f"{BASE}/workspaces/")
    assert blocked.status_code == 403 and blocked.json()["code"] == "mfa_required"
    enroll_totp(logged_in)
    assert logged_in.get(f"{BASE}/workspaces/").status_code == 200

    viewer = make_user("viewer@example.com", workspace, Role.VIEWER)
    other = Client()
    other.force_login(viewer)
    assert other.get(f"{BASE}/workspaces/").status_code == 200  # only listed roles are gated


# ---- password reset and change ----------------------------------------------------------------


def _reset_link_parts() -> tuple[str, str]:
    body = mail.outbox[-1].body
    link = next(line for line in body.splitlines() if "reset-password" in line)
    query = link.split("?", 1)[1]
    params = dict(part.split("=", 1) for part in query.split("&"))
    return params["uid"], params["token"]


def test_password_reset_round_trip_revokes_sessions_and_token_is_single_use(owner: User) -> None:
    session_client = Client()
    login(session_client, owner.email)
    assert SessionRecord.objects.filter(user=owner).exists()

    assert post(Client(), "/auth/password/reset-request", {"email": owner.email}).status_code == 202
    uid, token = _reset_link_parts()

    weak = post(
        Client(),
        "/auth/password/reset-confirm",
        {"uid": uid, "token": token, "new_password": "short"},
    )
    assert weak.status_code == 422 and weak.json()["code"] == "weak_password"

    strong = "a-much-stronger-password-2026"
    ok = post(
        Client(),
        "/auth/password/reset-confirm",
        {"uid": uid, "token": token, "new_password": strong},
    )
    assert ok.status_code == 204
    owner.refresh_from_db()
    assert owner.check_password(strong) and owner.password_changed_at is not None
    assert session_client.get(f"{BASE}/auth/me").status_code == 401
    assert login(Client(), owner.email, strong).json()["status"] == "ok"

    reused = post(
        Client(),
        "/auth/password/reset-confirm",
        {"uid": uid, "token": token, "new_password": strong + "x"},
    )
    assert reused.status_code == 422 and reused.json()["code"] == "invalid_token"


def test_reset_request_is_silent_for_unknown_addresses() -> None:
    before = len(mail.outbox)
    assert (
        post(Client(), "/auth/password/reset-request", {"email": "ghost@example.com"}).status_code
        == 202
    )
    assert len(mail.outbox) == before


def test_reset_clears_a_lockout(owner: User) -> None:
    User.objects.filter(pk=owner.pk).update(locked_until=timezone.now() + timedelta(hours=1))
    post(Client(), "/auth/password/reset-request", {"email": owner.email})
    uid, token = _reset_link_parts()
    post(
        Client(),
        "/auth/password/reset-confirm",
        {"uid": uid, "token": token, "new_password": "another-strong-password-9"},
    )
    owner.refresh_from_db()
    assert not owner.is_locked


def test_bad_reset_tokens_are_rejected() -> None:
    bad = post(
        Client(),
        "/auth/password/reset-confirm",
        {"uid": "zzz", "token": "nope", "new_password": "a-good-long-password-1"},
    )
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_token"


def test_change_password_needs_current_and_keeps_only_this_session(owner: User) -> None:
    here, elsewhere = Client(), Client()
    login(here, owner.email)
    login(elsewhere, owner.email)
    wrong = post(
        here, "/auth/password/change", {"current_password": "nope-nope-nope-1", "new_password": "x"}
    )
    assert wrong.status_code == 401
    new = "brand-new-password-value-7"
    ok = post(here, "/auth/password/change", {"current_password": PASSWORD, "new_password": new})
    assert ok.status_code == 204
    assert here.get(f"{BASE}/auth/me").status_code == 200
    assert elsewhere.get(f"{BASE}/auth/me").status_code == 401


# ---- sessions ---------------------------------------------------------------------------------


def test_sessions_can_be_listed_and_revoked(owner: User) -> None:
    here, other = Client(HTTP_USER_AGENT="Browser A"), Client(HTTP_USER_AGENT="Browser B")
    login(here, owner.email)
    login(other, owner.email)
    listing = here.get(f"{BASE}/auth/sessions").json()
    assert len(listing) == 2 and sum(item["current"] for item in listing) == 1
    victim = next(item for item in listing if not item["current"])
    assert here.delete(f"{BASE}/auth/sessions/{victim['id']}").status_code == 204
    assert other.get(f"{BASE}/auth/me").status_code == 401
    assert here.delete(f"{BASE}/auth/sessions/{victim['id']}").status_code == 404


def test_revoke_other_sessions(owner: User) -> None:
    here, other = Client(), Client()
    login(here, owner.email)
    login(other, owner.email)
    assert post(here, "/auth/sessions/revoke-others").status_code == 204
    assert here.get(f"{BASE}/auth/me").status_code == 200
    assert other.get(f"{BASE}/auth/me").status_code == 401


# ---- API tokens -------------------------------------------------------------------------------


def _bearer(secret: str) -> dict[str, str]:
    return {"HTTP_AUTHORIZATION": f"Bearer {secret}"}


def test_api_tokens_authenticate_machine_clients(
    logged_in: Client, owner: User, workspace: Workspace
) -> None:
    created = post(logged_in, "/auth/tokens", {"name": "ci", "scopes": ["read"]}).json()
    secret = created["secret"]
    assert secret.startswith("itb_")
    stored = ApiToken.objects.get(pk=created["token"]["id"])
    assert secret.split("_", 2)[2] not in stored.token_hash and len(stored.token_hash) == 64

    anonymous = Client()
    assert anonymous.get(f"{BASE}/workspaces/").status_code == 401
    ok = anonymous.get(f"{BASE}/workspaces/", **_bearer(secret))
    assert ok.status_code == 200 and ok.json()[0]["slug"] == workspace.slug
    assert anonymous.get(f"{BASE}/workspaces/", **_bearer(secret + "x")).status_code == 401

    blocked = post(
        anonymous,
        "/workspaces/invitations",
        {"email": "x@example.com"},
        **_bearer(secret),
        HTTP_X_WORKSPACE_ID=str(workspace.pk),
    )
    assert blocked.status_code == 401  # read scope cannot use unsafe methods


def test_write_tokens_can_post_and_revocation_takes_effect(
    logged_in: Client, owner: User, workspace: Workspace
) -> None:
    created = post(
        logged_in, "/auth/tokens", {"name": "writer", "scopes": ["read", "write"]}
    ).json()
    secret, token_id = created["secret"], created["token"]["id"]
    anonymous = Client()
    invited = post(
        anonymous,
        "/workspaces/invitations",
        {"email": "new@example.com", "role": "viewer"},
        **_bearer(secret),
        HTTP_X_WORKSPACE_ID=str(workspace.pk),
    )
    assert invited.status_code == 200
    assert logged_in.delete(f"{BASE}/auth/tokens/{token_id}").status_code == 204
    assert anonymous.get(f"{BASE}/workspaces/", **_bearer(secret)).status_code == 401


def test_expired_and_invalid_scope_tokens_are_refused(logged_in: Client, owner: User) -> None:
    created = post(
        logged_in, "/auth/tokens", {"name": "old", "scopes": ["read"], "expires_in_days": 1}
    ).json()
    ApiToken.objects.filter(pk=created["token"]["id"]).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert Client().get(f"{BASE}/workspaces/", **_bearer(created["secret"])).status_code == 401
    assert post(logged_in, "/auth/tokens", {"name": "bad", "scopes": ["admin"]}).status_code == 422


# ---- invitations ------------------------------------------------------------------------------


def _invite(client: Client, workspace: Workspace, email: str, role: str = "editor"):
    return post(
        client,
        "/workspaces/invitations",
        {"email": email, "role": role},
        HTTP_X_WORKSPACE_ID=str(workspace.pk),
    )


def _invite_token() -> str:
    link = next(line for line in mail.outbox[-1].body.splitlines() if "accept-invitation" in line)
    return link.split("token=", 1)[1]


def test_invitation_flow_creates_a_user_and_membership(
    logged_in: Client, workspace: Workspace
) -> None:
    response = _invite(logged_in, workspace, "Newbie@Example.com")
    assert response.status_code == 200
    assert mail.outbox[-1].to == ["newbie@example.com"]
    token = _invite_token()

    weak = post(Client(), "/auth/invitations/accept", {"token": token, "password": "short"})
    assert weak.status_code == 422 and weak.json()["code"] == "weak_password"
    ok = post(
        Client(),
        "/auth/invitations/accept",
        {"token": token, "password": "invitee-strong-password-3", "full_name": "New Bie"},
    )
    assert ok.status_code == 204
    user = User.objects.get(email="newbie@example.com")
    assert user.memberships.get(workspace=workspace).role == "editor"
    again = post(
        Client(),
        "/auth/invitations/accept",
        {"token": token, "password": "invitee-strong-password-3"},
    )
    assert again.json()["code"] == "invalid_token"


def test_existing_users_are_added_without_a_new_password(
    logged_in: Client, workspace: Workspace
) -> None:
    existing = make_user("existing@example.com")
    _invite(logged_in, workspace, existing.email, "analyst")
    assert post(Client(), "/auth/invitations/accept", {"token": _invite_token()}).status_code == 204
    assert existing.memberships.get(workspace=workspace).role == "analyst"


def test_invitation_rules(logged_in: Client, workspace: Workspace) -> None:
    assert _invite(logged_in, workspace, "boss@example.com", "owner").status_code == 403
    member = make_user("member@example.com", workspace, Role.VIEWER)
    assert _invite(logged_in, workspace, member.email).status_code == 409

    first = _invite(logged_in, workspace, "dup@example.com").json()
    _invite(logged_in, workspace, "dup@example.com")
    listing = logged_in.get(
        f"{BASE}/workspaces/invitations", HTTP_X_WORKSPACE_ID=str(workspace.pk)
    ).json()
    assert [i["email"] for i in listing].count("dup@example.com") == 1
    assert first["id"] not in {i["id"] for i in listing}  # the older one was revoked

    latest = next(i for i in listing if i["email"] == "dup@example.com")
    revoke = logged_in.delete(
        f"{BASE}/workspaces/invitations/{latest['id']}", HTTP_X_WORKSPACE_ID=str(workspace.pk)
    )
    assert revoke.status_code == 204
    assert (
        post(
            Client(),
            "/auth/invitations/accept",
            {"token": _invite_token(), "password": "irrelevant-password-1"},
        ).json()["code"]
        == "invalid_token"
    )


def test_only_managers_may_invite(workspace: Workspace) -> None:
    editor = make_user("editor@example.com", workspace, Role.EDITOR)
    client = Client()
    client.force_login(editor)
    assert _invite(client, workspace, "x@example.com").status_code == 403


def test_expired_invitations_are_invalid(logged_in: Client, workspace: Workspace) -> None:
    _invite(logged_in, workspace, "late@example.com")
    from workspaces.models import Invitation

    Invitation.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert (
        post(
            Client(),
            "/auth/invitations/accept",
            {"token": _invite_token(), "password": "some-strong-password-4"},
        ).json()["code"]
        == "invalid_token"
    )


# ---- passkeys ---------------------------------------------------------------------------------


def _fake_verified_registration():
    return mock.Mock(credential_id=b"cred-1", credential_public_key=b"pub-1", sign_count=5)


def test_passkey_registration_and_second_factor_login(logged_in: Client, owner: User) -> None:
    options = post(logged_in, "/auth/mfa/passkeys/options").json()
    assert options["challenge"] and options["rp"]["id"]

    with mock.patch(
        "identity.passkeys.verify_registration_response", return_value=_fake_verified_registration()
    ):
        created = post(
            logged_in,
            "/auth/mfa/passkeys",
            {"credential": {"response": {"transports": ["usb"]}}, "name": "Key"},
        )
    assert created.status_code == 200 and PasskeyCredential.objects.filter(user=owner).count() == 1
    assert mfa.mfa_enabled(owner)

    client = Client()
    assert login(client, owner.email).json()["status"] == "mfa_required"
    assert post(client, "/auth/login/passkey/options").json()["challenge"]
    verified = mock.Mock(new_sign_count=6)
    raw_id = "Y3JlZC0x"  # base64url of b"cred-1"
    with mock.patch("identity.passkeys.verify_authentication_response", return_value=verified):
        done = post(client, "/auth/login/passkey/verify", {"credential": {"rawId": raw_id}})
    assert done.status_code == 200 and client.get(f"{BASE}/auth/me").status_code == 200
    assert PasskeyCredential.objects.get(user=owner).sign_count == 6


def test_failed_passkey_verification_counts_as_a_failure(logged_in: Client, owner: User) -> None:
    post(logged_in, "/auth/mfa/passkeys/options")
    with mock.patch(
        "identity.passkeys.verify_registration_response", return_value=_fake_verified_registration()
    ):
        post(logged_in, "/auth/mfa/passkeys", {"credential": {}, "name": "Key"})
    client = Client()
    login(client, owner.email)
    post(client, "/auth/login/passkey/options")
    with mock.patch(
        "identity.passkeys.verify_authentication_response", side_effect=ValueError("bad signature")
    ):
        bad = post(client, "/auth/login/passkey/verify", {"credential": {"rawId": "Y3JlZC0x"}})
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_mfa_code"
    owner.refresh_from_db()
    assert owner.failed_login_count == 1


def test_registration_needs_a_challenge_and_passkeys_can_be_removed(
    logged_in: Client, owner: User
) -> None:
    assert (
        post(logged_in, "/auth/mfa/passkeys", {"credential": {}, "name": "Key"}).status_code == 422
    )
    post(logged_in, "/auth/mfa/passkeys/options")
    with mock.patch(
        "identity.passkeys.verify_registration_response", return_value=_fake_verified_registration()
    ):
        created = post(logged_in, "/auth/mfa/passkeys", {"credential": {}, "name": "Key"}).json()
    assert logged_in.get(f"{BASE}/auth/mfa/passkeys").json()[0]["name"] == "Key"
    assert logged_in.delete(f"{BASE}/auth/mfa/passkeys/{created['id']}").status_code == 204
    assert logged_in.delete(f"{BASE}/auth/mfa/passkeys/{created['id']}").status_code == 404


# ---- breached passwords and email templates ---------------------------------------------------


def test_breached_password_lookup_uses_k_anonymity_and_fails_open() -> None:
    import hashlib

    digest = hashlib.sha1(b"password123", usedforsecurity=False).hexdigest().upper()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=f"{digest[5:]}:42\r\n0000000000000000000000000000000000A:1")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert is_breached("password123", client=client) is True
    assert seen == [f"https://api.pwnedpasswords.com/range/{digest[:5]}"]
    assert (
        is_breached(
            "a-very-unlikely-passphrase-xyz",
            client=httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ABC:1"))
            ),
        )
        is False
    )

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    assert (
        is_breached("anything", client=httpx.Client(transport=httpx.MockTransport(boom))) is False
    )


def test_every_email_template_renders_with_its_context() -> None:
    contexts = {
        "password_reset": {"link": "https://x.test/reset"},
        "invitation": {
            "workspace": "W",
            "inviter": "I",
            "role": "Editor",
            "days": 7,
            "link": "https://x.test/i",
        },
        "account_locked": {"minutes": 15},
        "crm_digest": {"workspace": "W", "count": 2, "items": "A customer replied: 2"},
        "verify_email": {"link": "https://x.test/verify"},
        "account_exists": {"link": "https://x.test/login"},
        "approval_needed": {"workspace": "W", "agent": "ads", "summary": "S", "risk": "R2"},
    }
    assert set(contexts) == set(TEMPLATES)
    for key, context in contexts.items():
        subject, body = render(key, context)
        assert subject and body and "{" not in subject + body
