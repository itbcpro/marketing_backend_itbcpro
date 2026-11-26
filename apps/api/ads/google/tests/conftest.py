import httpx
import pytest

from ads.google import factory
from ads.google.client import GoogleAdsClient
from ads.google.tests.fake_google import FakeGoogle
from audit.actors import Actor
from connections import services as connection_services
from connections.models import ConnectionStatus


@pytest.fixture
def google_connection(workspace):
    connection = connection_services.create_connection(
        workspace_id=workspace.pk,
        provider="google_ads",
        external_account_id="1234567890",
        display_name="Acme",
        actor=Actor.system("test"),
    )
    connection.config = {"login_customer_id": "111"}
    connection.save()
    connection_services.set_status(connection, ConnectionStatus.ACTIVE)
    return connection


@pytest.fixture
def fake(monkeypatch):
    server = FakeGoogle()
    monkeypatch.setattr(
        factory,
        "client_factory",
        lambda connection: GoogleAdsClient(
            connection,
            access_token="token",
            developer_token="dev-token",
            transport=httpx.MockTransport(server.handler),
            throttle=lambda _: None,
            refresher=lambda _: False,
        ),
    )
    return server
