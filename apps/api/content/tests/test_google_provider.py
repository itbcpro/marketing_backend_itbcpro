from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.utils import timezone

from connections import lifecycle
from connections.models import ConnectionStatus
from content.google import gsc, provider
from content.google.credentials import app_credentials
from content.tests.fake_google import EMAIL
from core.errors import AuthExpiredError

JSON = "application/json"


def test_keys_come_from_the_workspace_connection(workspace, google_keys):
    keys = app_credentials(workspace.pk)
    assert (keys.client_id, keys.client_secret) == ("client-id", "client-secret")
    assert keys.missing() == []


def test_keys_are_missing_without_any_source(workspace, settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = ""
    settings.GOOGLE_OAUTH_CLIENT_SECRET = ""
    assert app_credentials(workspace.pk).missing() == ["client_id", "client_secret"]


def test_settings_are_the_last_fallback(workspace, settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = "env-id"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "env-secret"
    assert app_credentials(workspace.pk).client_id == "env-id"


def test_oauth_config_asks_for_offline_read_only_access(workspace, google_keys):
    config = provider.oauth_config(workspace.pk)
    assert config.client_id == "client-id"
    assert config.uses_pkce
    assert config.extra_authorize_params["access_type"] == "offline"
    assert gsc.PROVIDER == "google_search"
    assert "https://www.googleapis.com/auth/webmasters.readonly" in config.scopes
    assert not any("analytics" in scope or "adwords" in scope for scope in config.scopes)


def test_oauth_start_builds_the_google_url(client, owner, workspace, google_keys):
    client.force_login(owner)
    response = client.post(
        "/api/v1/connections/oauth/google_search/start",
        content_type=JSON,
        HTTP_X_WORKSPACE_ID=str(workspace.pk),
    )
    assert response.status_code == 200, response.content
    url = urlsplit(response.json()["authorization_url"])
    query = parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert query["client_id"] == ["client-id"]
    assert "webmasters.readonly" in query["scope"][0]
    assert query["access_type"] == ["offline"]
    assert query["redirect_uri"][0].endswith("/connections/oauth/google_search/callback")


def test_identify_returns_the_email(google):
    assert provider.identify("tok-1") == (EMAIL, f"Google ({EMAIL})")


def test_identify_rejects_a_bad_token(google):
    with pytest.raises(AuthExpiredError):
        provider.identify("wrong")


def test_health_check_is_active(google_connection):
    assert lifecycle.check_connection(google_connection).status == ConnectionStatus.ACTIVE


def test_health_check_without_properties_is_degraded(google_connection, google):
    google.sites = []
    result = lifecycle.check_connection(google_connection)
    assert result.status == ConnectionStatus.DEGRADED
    assert "no Search Console property" in result.detail


def test_health_check_with_a_revoked_token_is_expired(google_connection, google):
    google.token = "something-else"
    assert lifecycle.check_connection(google_connection).status == ConnectionStatus.EXPIRED


def test_an_expiring_token_is_refreshed_before_use(google_connection, google):
    google_connection.expires_at = timezone.now() + timedelta(seconds=30)
    google_connection.save()
    sites = gsc.SearchConsoleClient(google_connection).list_sites()
    assert google.refreshes == 1
    assert sites[0]["site_url"] == "sc-domain:wp.example.com"
    assert ("GET", "/webmasters/v3/sites") in google.calls
