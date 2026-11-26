import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from django.utils import timezone

from audit.actors import Actor
from connections import services as connection_services
from connections.models import ConnectionStatus, CredentialKind
from llm.fake import FakeGateway, text_response
from llm.service import LlmService, set_llm_service
from social import images, publishing
from social.instagram import factory
from social.instagram.client import InstagramClient
from social.models import SocialAccount, SocialComment, SocialMedia
from social.tests.fake_instagram import ACCOUNT_ID, FakeInstagram


@pytest.fixture
def connection(workspace):
    connection = connection_services.create_connection(
        workspace_id=workspace.pk,
        provider="instagram",
        external_account_id=ACCOUNT_ID,
        display_name="@acme_it",
        actor=Actor.system("test"),
    )
    connection_services.store_secret(
        connection, CredentialKind.ACCESS_TOKEN, "ig-token", actor=Actor.system("test")
    )
    connection.granted_scopes = [
        "instagram_business_basic",
        "instagram_business_content_publish",
        "instagram_business_manage_comments",
        "instagram_business_manage_messages",
        "instagram_business_manage_insights",
    ]
    connection.save()
    connection_services.set_status(connection, ConnectionStatus.ACTIVE)
    return connection


@pytest.fixture
def fake(monkeypatch) -> FakeInstagram:
    server = FakeInstagram()
    monkeypatch.setattr(
        factory,
        "client_factory",
        lambda connection: InstagramClient(
            connection,
            access_token="ig-token",
            transport=httpx.MockTransport(server.handler),
            throttle=lambda _: None,
        ),
    )
    monkeypatch.setattr(publishing, "sleeper", lambda _seconds: None)
    return server


@pytest.fixture
def account(workspace, connection) -> SocialAccount:
    return SocialAccount.objects.create(
        workspace_id=workspace.pk,
        connection=connection,
        external_id=ACCOUNT_ID,
        username="acme_it",
        account_type="BUSINESS",
        followers_count=1200,
    )


@pytest.fixture(autouse=True)
def _isolation(monkeypatch) -> Iterator[None]:
    # Tests run the background work themselves, so queuing is a no-op here.
    monkeypatch.setattr("social.analysis._enqueue", lambda report: None)
    monkeypatch.setattr("social.composer._enqueue", lambda draft: None)
    monkeypatch.setattr("social.drafts._publish_soon", lambda: None)
    yield
    set_llm_service(None)
    images.set_image_generator(None)


def llm(*payloads: Any) -> FakeGateway:
    gateway = FakeGateway([text_response(json.dumps(p)) for p in payloads])
    set_llm_service(LlmService(gateway))
    return gateway


def make_media(account: SocialAccount, external_id: str, **fields: Any) -> SocialMedia:
    defaults: dict[str, Any] = {
        "workspace_id": account.workspace_id,
        "kind": "image",
        "caption": "A post",
        "posted_at": timezone.now() - timedelta(days=5),
        "metrics": {},
    }
    defaults.update(fields)
    return SocialMedia.objects.create(account=account, external_id=external_id, **defaults)


def make_comment(
    account: SocialAccount, external_id: str, text: str, **fields: Any
) -> SocialComment:
    defaults: dict[str, Any] = {
        "workspace_id": account.workspace_id,
        "username": "alice",
        "posted_at": timezone.now() - timedelta(minutes=30),
    }
    defaults.update(fields)
    return SocialComment.objects.create(
        account=account, external_id=external_id, text=text, **defaults
    )


def headers(workspace) -> dict[str, str]:
    return {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}


def new_uuid() -> str:
    return str(uuid.uuid4())
