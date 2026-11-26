import httpx
import pytest

from ads.meta import factory
from ads.meta.client import MetaAdsClient
from ads.meta.tests.fake_meta import ACCOUNT, FakeMeta
from audit.actors import Actor
from connections import services as connection_services
from connections.models import ConnectionStatus, CredentialKind


@pytest.fixture
def meta_connection(workspace):
    connection = connection_services.create_connection(
        workspace_id=workspace.pk,
        provider="meta",
        external_account_id=ACCOUNT,
        display_name="Acme ads",
        actor=Actor.system("test"),
    )
    connection.granted_scopes = ["ads_management", "ads_read"]
    connection.save()
    connection_services.store_secret(
        connection, CredentialKind.ACCESS_TOKEN, "token", actor=Actor.system("test")
    )
    connection_services.set_status(connection, ConnectionStatus.ACTIVE)
    return connection


@pytest.fixture
def fake(monkeypatch):
    server = FakeMeta()
    monkeypatch.setattr(
        factory,
        "client_factory",
        lambda connection: MetaAdsClient(
            connection,
            token="token",
            app_secret="secret",
            transport=httpx.MockTransport(server.handler),
            throttle=lambda _: None,
        ),
    )
    return server
