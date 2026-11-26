import json
import uuid
from datetime import timedelta

import jwt
import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from audit.models import AuditEvent
from conftest import PASSWORD, make_user
from identity import jwt_keys, jwt_service, tokens
from identity.models import RefreshToken, User
from workspaces.models import Role, Workspace
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
BASE = "/api/v1"
PROTECTED = f"{BASE}/actions/inbox"


@pytest.fixture(autouse=True)
def _fresh_keyring():
    jwt_keys.reset_keyring()
    cache.clear()
    yield
    jwt_keys.reset_keyring()


def _post(client: Client, path: str, body: dict | None = None):
    if "csrftoken" not in client.cookies:
        client.get(f"{BASE}/auth/csrf")
    token = client.cookies["csrftoken"].value
    return client.post(
        f"{BASE}{path}",
        data=json.dumps(body or {}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )


def _pair(owner: User, workspace: Workspace) -> dict:
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    response = _post(client, "/auth/token", {"workspace_id": str(workspace.pk)})
    assert response.status_code == 200, response.content
    return response.json()


def _get(token: str, path: str = PROTECTED, **extra):
    return Client().get(path, HTTP_AUTHORIZATION=f"Bearer {token}", **extra)


def _refresh(raw: str):
    return Client().post(
        f"{BASE}/auth/refresh",
        data=json.dumps({"refresh_token": raw}),
        content_type="application/json",
    )


def test_session_exchanges_for_a_token_that_reaches_workspace_endpoints(owner, workspace):
    pair = _pair(owner, workspace)
    assert pair["token_type"] == "Bearer"
    assert pair["workspace_id"] == str(workspace.pk)
    assert _get(pair["access_token"]).status_code == 200  # the token names the workspace
    assert _get(pair["access_token"], HTTP_X_WORKSPACE_ID=str(workspace.pk)).status_code == 200
    assert AuditEvent.objects.filter(action="auth.token_issued").exists()


def test_token_is_bound_to_its_workspace(owner, workspace):
    other = create_workspace(name="Other", owner=owner)
    pair = _pair(owner, workspace)
    response = _get(pair["access_token"], HTTP_X_WORKSPACE_ID=str(other.pk))
    assert response.status_code == 422
    assert response.json()["code"] == "workspace_required"


def test_cannot_exchange_for_a_workspace_without_membership(owner, workspace):
    outsider = make_user("outsider@example.com")
    client = Client(enforce_csrf_checks=True)
    client.force_login(outsider)
    assert _post(client, "/auth/token", {"workspace_id": str(workspace.pk)}).status_code == 401
    assert _post(client, "/auth/token", {"workspace_id": str(uuid.uuid4())}).status_code == 401


def test_exchange_requires_a_session():
    response = Client(enforce_csrf_checks=True).post(
        f"{BASE}/auth/token",
        data=json.dumps({"workspace_id": str(uuid.uuid4())}),
        content_type="application/json",
    )
    assert response.status_code in {401, 403}


def test_tampered_signature_is_rejected(owner, workspace):
    token = _pair(owner, workspace)["access_token"]
    head, body, sig = token.split(".")
    forged = ".".join([head, body, ("A" if sig[0] != "A" else "B") + sig[1:]])
    assert _get(forged).status_code == 401


def test_alg_none_is_rejected(owner, workspace):
    token = _pair(owner, workspace)["access_token"]
    claims = jwt.decode(token, options={"verify_signature": False})
    unsigned = jwt.encode(claims, key=None, algorithm="none", headers={"kid": "ephemeral"})
    assert _get(unsigned).status_code == 401


def test_algorithm_confusion_with_hmac_is_rejected(owner, workspace):
    token = _pair(owner, workspace)["access_token"]
    claims = jwt.decode(token, options={"verify_signature": False})
    ring = jwt_keys.get_keyring()
    public_secret = ring.jwks()["keys"][0]["x"]
    forged = jwt.encode(claims, public_secret, algorithm="HS256", headers={"kid": ring.active_kid})
    assert _get(forged).status_code == 401


def test_unknown_kid_is_rejected(owner, workspace):
    token = _pair(owner, workspace)["access_token"]
    claims = jwt.decode(token, options={"verify_signature": False})
    other = jwt_keys.KeyRing({"other": jwt_keys._generate("EdDSA")}, "other", "EdDSA")
    assert _get(other.sign(claims)).status_code == 401


def test_expired_wrong_audience_and_wrong_issuer_are_rejected(owner, workspace):
    token = _pair(owner, workspace)["access_token"]
    claims = jwt.decode(token, options={"verify_signature": False})
    ring = jwt_keys.get_keyring()
    past = int((timezone.now() - timedelta(hours=1)).timestamp())
    expired = {**claims, "exp": past, "iat": past - 60, "nbf": past - 60}
    assert _get(ring.sign(expired)).status_code == 401
    assert _get(ring.sign({**claims, "aud": "someone-else"})).status_code == 401
    assert _get(ring.sign({**claims, "iss": "someone-else"})).status_code == 401


def test_missing_required_claims_are_rejected(owner, workspace):
    token = _pair(owner, workspace)["access_token"]
    claims = jwt.decode(token, options={"verify_signature": False})
    claims.pop("sid")
    assert _get(jwt_keys.get_keyring().sign(claims)).status_code == 401


def test_jwks_publishes_only_public_material(owner, workspace):
    response = Client().get("/.well-known/jwks.json")
    assert response.status_code == 200
    key = response.json()["keys"][0]
    assert key["use"] == "sig" and key["alg"] == "EdDSA" and key["kid"]
    assert "d" not in key
    pair = _pair(owner, workspace)
    jwt.decode(
        pair["access_token"],
        jwt.PyJWK(key).key,
        algorithms=["EdDSA"],
        audience="itbcpro",
        issuer="itbcpro-api",
    )


def test_refresh_rotates_the_token(owner, workspace):
    first = _pair(owner, workspace)
    second = _refresh(first["refresh_token"])
    assert second.status_code == 200
    new = second.json()
    assert new["refresh_token"] != first["refresh_token"]
    assert _get(new["access_token"]).status_code == 200


def test_refresh_reuse_revokes_the_whole_family(owner, workspace):
    first = _pair(owner, workspace)
    second = _refresh(first["refresh_token"]).json()
    assert _refresh(first["refresh_token"]).status_code == 401  # replay of a used token
    assert AuditEvent.objects.filter(action="auth.refresh_reuse_detected").exists()
    assert _refresh(second["refresh_token"]).status_code == 401  # the family is dead
    assert _get(second["access_token"]).status_code == 401  # and so are its access tokens


def test_unknown_refresh_token_is_rejected():
    assert _refresh("nope").status_code == 401


def test_refresh_stops_when_membership_is_removed(owner, workspace):
    member = make_user("member@example.com", workspace, Role.EDITOR)
    pair = _pair(member, workspace)
    member.memberships.all().delete()
    assert _refresh(pair["refresh_token"]).status_code == 401


def test_disabled_user_loses_access_immediately(owner, workspace):
    pair = _pair(owner, workspace)
    User.objects.filter(pk=owner.pk).update(is_active=False)
    assert _get(pair["access_token"]).status_code == 401


def test_idle_expiry_blocks_refresh(owner, workspace):
    pair = _pair(owner, workspace)
    RefreshToken.objects.update(idle_expires_at=timezone.now() - timedelta(seconds=1))
    assert _refresh(pair["refresh_token"]).status_code == 401


def test_revoke_endpoint_kills_the_family(owner, workspace):
    pair = _pair(owner, workspace)
    done = Client().post(
        f"{BASE}/auth/token/revoke",
        data=json.dumps({"refresh_token": pair["refresh_token"]}),
        content_type="application/json",
    )
    assert done.status_code == 204
    assert _get(pair["access_token"]).status_code == 401
    assert _refresh(pair["refresh_token"]).status_code == 401


def test_password_change_revokes_issued_tokens(owner, workspace):
    pair = _pair(owner, workspace)
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    response = _post(
        client,
        "/auth/password/change",
        {"current_password": PASSWORD, "new_password": "another-very-long-passphrase-77"},
    )
    assert response.status_code in {200, 204}, response.content
    assert _get(pair["access_token"]).status_code == 401


def test_revocation_list_outage_fails_closed(owner, workspace, monkeypatch):
    pair = _pair(owner, workspace)

    def boom(*args, **kwargs):
        raise ConnectionError("cache down")

    monkeypatch.setattr(jwt_service.cache, "get", boom)
    assert _get(pair["access_token"]).status_code == 401


def test_machine_api_tokens_still_work(owner, workspace):
    _, raw = tokens.create_token(owner, name="ci", scopes=["read"])
    response = Client().get(
        PROTECTED, HTTP_AUTHORIZATION=f"Bearer {raw}", HTTP_X_WORKSPACE_ID=str(workspace.pk)
    )
    assert response.status_code == 200


@override_settings(JWT_ACCESS_TTL_SECONDS=120)
def test_access_lifetime_follows_settings(owner, workspace):
    pair = _pair(owner, workspace)
    assert pair["expires_in"] == 120
    claims = jwt.decode(pair["access_token"], options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 120
