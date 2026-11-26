import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ads.meta import auth, provider
from ads.meta.credentials import app_credentials
from connections import oauth
from connections.models import Connection, CredentialKind
from connections.services import read_secret
from core.errors import AuthExpiredError

pytestmark = pytest.mark.django_db

BASE = "/api/v1/meta"
ADS_BASE = "/api/v1/ads/meta"


def headers(workspace):
    return {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}


def post(client, workspace, path, body):
    return client.post(
        path, data=json.dumps(body), content_type="application/json", **headers(workspace)
    )


def test_status_lists_what_each_product_still_needs(logged_in, workspace, settings):
    settings.META_APP_ID = settings.META_APP_SECRET = ""
    settings.INSTAGRAM_APP_ID = settings.INSTAGRAM_APP_SECRET = ""
    body = logged_in.get(f"{BASE}/status", **headers(workspace)).json()
    assert body["facebook"]["missing"] == ["app_id", "app_secret"]
    assert body["instagram"]["missing"] == ["app_id", "app_secret"]
    assert body["facebook"]["redirect_uri"].endswith("/connections/oauth/meta/callback")
    assert body["instagram"]["redirect_uri"].endswith("/connections/oauth/instagram/callback")
    assert "ads_management" in body["facebook"]["scopes"]


def test_one_save_stores_both_pairs_encrypted_and_unlocks_both_products(
    logged_in, workspace, settings
):
    settings.META_APP_ID = settings.META_APP_SECRET = ""
    settings.INSTAGRAM_APP_ID = settings.INSTAGRAM_APP_SECRET = ""
    saved = post(
        logged_in,
        workspace,
        f"{BASE}/keys",
        {
            "app_id": " 111 ",
            "app_secret": "fb-secret",
            "instagram_app_id": "222",
            "instagram_app_secret": "ig-secret",
        },
    )
    body = saved.json()
    assert saved.status_code == 200
    assert body["facebook"]["configured"] and body["instagram"]["configured"]
    keys = app_credentials(workspace.pk)
    assert (keys.app_id, keys.app_secret) == ("111", "fb-secret")
    connection = Connection.objects.get(workspace=workspace, provider="meta_app")
    assert read_secret(connection, CredentialKind.CLIENT_SECRET) == "fb-secret"
    from social.instagram.credentials import app_credentials as instagram_keys

    assert instagram_keys(workspace.pk).app_id == "222"
    again = post(logged_in, workspace, f"{BASE}/keys", {"app_id": "333", "app_secret": "new"})
    assert again.status_code == 200
    assert Connection.objects.filter(workspace=workspace, provider="meta_app").count() == 1
    assert app_credentials(workspace.pk).app_id == "333"


@pytest.mark.parametrize(
    "body",
    [{}, {"app_id": "1"}, {"instagram_app_secret": "x"}],
)
def test_incomplete_pairs_are_refused(logged_in, workspace, body):
    assert post(logged_in, workspace, f"{BASE}/keys", body).status_code == 422


def test_viewers_cannot_save_keys(client, workspace):
    from conftest import make_user
    from workspaces.models import Role

    viewer = make_user("viewer@example.com", workspace, Role.VIEWER)
    client.force_login(viewer)
    assert post(
        client, workspace, f"{BASE}/keys", {"app_id": "1", "app_secret": "2"}
    ).status_code in {
        401,
        403,
    }


def test_the_sign_in_link_uses_the_facebook_dialog_and_the_ads_scopes(workspace, settings):
    settings.META_APP_ID, settings.META_APP_SECRET = "111", "secret"
    config = provider.oauth_config(workspace.pk)
    request = oauth.build_authorization_request(config, redirect_uri="https://app.example/cb")
    url = urlparse(request.url)
    query = parse_qs(url.query)
    assert url.netloc == "www.facebook.com" and url.path.endswith("/dialog/oauth")
    assert query["client_id"] == ["111"] and query["scope"] == ["ads_management,ads_read"]
    assert "code_challenge" not in query


def _graph(requests):
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        requests.append((request.url.path, params))
        if request.url.path.endswith("/oauth/access_token"):
            if params.get("grant_type") == "fb_exchange_token":
                return httpx.Response(200, json={"access_token": "long", "expires_in": 5_184_000})
            return httpx.Response(200, json={"access_token": "short"})
        if request.url.path.endswith("/me/permissions"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"permission": "ads_management", "status": "granted"},
                        {"permission": "ads_read", "status": "granted"},
                        {"permission": "pages_show_list", "status": "declined"},
                    ]
                },
            )
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_code_becomes_a_long_lived_token_with_the_granted_permissions(workspace, settings):
    settings.META_APP_ID, settings.META_APP_SECRET = "111", "secret"
    seen: list = []
    tokens = auth.exchange(
        provider.oauth_config(workspace.pk), "the-code", "https://app.example/cb", "",
        client=_graph(seen),
    )  # fmt: skip
    assert tokens.access_token == "long" and tokens.refresh_token is None
    assert tokens.scopes == ["ads_management", "ads_read"]
    assert tokens.expires_at is not None
    first, second, third = seen
    assert first[1]["code"] == "the-code" and first[1]["client_secret"] == "secret"
    assert second[1]["fb_exchange_token"] == "short"
    assert third[1]["access_token"] == "long"


def test_identify_prefers_an_active_ad_account(monkeypatch):
    monkeypatch.setattr(
        auth,
        "list_ad_accounts",
        lambda token: [
            {"id": "act_1", "name": "Closed", "account_status": 101},
            {"id": "act_2", "name": "Live", "account_status": 1},
        ],
    )
    assert provider.identify("token") == ("act_2", "Live")


def test_a_person_without_an_ad_account_gets_a_clear_message(monkeypatch):
    monkeypatch.setattr(auth, "list_ad_accounts", lambda token: [])
    with pytest.raises(AuthExpiredError, match="no ad account"):
        provider.identify("token")


def test_health_reports_a_disabled_account_as_degraded(meta_connection, fake):
    fake.account_status = 2
    result = provider.health_check(meta_connection)
    assert result.status == "degraded" and "disabled" in result.detail
    fake.account_status = 1
    assert provider.health_check(meta_connection).status == "active"


def test_the_account_list_and_assets_come_from_meta(
    logged_in, workspace, meta_connection, fake, monkeypatch
):
    monkeypatch.setattr(
        auth,
        "list_ad_accounts",
        lambda token: [
            {"id": "act_555", "name": "Acme ads", "currency": "USD", "account_status": 1}
        ],
    )
    accounts = logged_in.get(
        f"{ADS_BASE}/accounts", {"connection_id": str(meta_connection.pk)}, **headers(workspace)
    ).json()
    assert accounts["selected"] == "act_555" and accounts["accounts"][0]["name"] == "Acme ads"
    assets = logged_in.get(
        f"{ADS_BASE}/assets", {"connection_id": str(meta_connection.pk)}, **headers(workspace)
    ).json()
    assert assets["pages"] == [{"id": "77", "name": "Acme"}]
    assert assets["instagram_accounts"] == [{"id": "88", "username": "acme"}]


def test_selecting_an_account_validates_the_id(logged_in, workspace, meta_connection, fake):
    bad = post(
        logged_in, workspace, f"{ADS_BASE}/accounts/select",
        {"connection_id": str(meta_connection.pk), "account_id": "not-a-number"},
    )  # fmt: skip
    assert bad.status_code == 422
    good = post(
        logged_in, workspace, f"{ADS_BASE}/accounts/select",
        {"connection_id": str(meta_connection.pk), "account_id": "555"},
    )  # fmt: skip
    assert good.status_code == 200 and good.json()["selected"] == "act_555"
