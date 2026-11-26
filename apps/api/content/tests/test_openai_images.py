import base64
import json

import httpx
import pytest

from audit.actors import Actor
from connections import services as connection_services
from connections.models import ConnectionStatus, CredentialKind
from content import net
from content.blog import openai_images
from core.errors import AuthExpiredError, PermanentProviderError, ValidationFailedError

PNG = b"RIFFxxxxWEBPfake"


@pytest.fixture
def openai(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.headers.get("authorization") != "Bearer sk-good":
            return httpx.Response(401, json={"error": {"code": "invalid_api_key"}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        body = json.loads(request.content)
        if "forbidden" in body["prompt"]:
            return httpx.Response(400, json={"error": {"code": "moderation_blocked"}})
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    monkeypatch.setattr(net, "transport", httpx.MockTransport(handler))
    monkeypatch.setattr(net, "resolver", lambda host, port: ["93.184.216.34"])
    return calls


def connect(workspace, key):
    connection = connection_services.create_connection(
        workspace_id=workspace.pk,
        provider=openai_images.PROVIDER,
        external_account_id="openai-images",
        display_name="OpenAI",
        actor=Actor.system("test"),
    )
    connection_services.store_secret(
        connection, CredentialKind.API_KEY, key, actor=Actor.system("test")
    )
    connection_services.set_status(connection, ConnectionStatus.ACTIVE)
    return connection


def test_generate_returns_the_decoded_image_and_sends_the_expected_request(workspace, openai):
    connect(workspace, "sk-good")
    assert openai_images.has_key(workspace.pk)
    assert openai_images.generate(workspace.pk, "a calm desk") == PNG
    sent = json.loads(openai[0].content)
    assert sent["model"] == "gpt-image-1"
    assert sent["size"] == "1536x1024" and sent["output_format"] == "webp"
    assert openai[0].url.path == "/v1/images/generations"


def test_a_refused_prompt_is_a_permanent_error(workspace, openai):
    connect(workspace, "sk-good")
    with pytest.raises(PermanentProviderError):
        openai_images.generate(workspace.pk, "forbidden thing")


def test_a_rejected_key_is_reported(workspace, openai):
    connection = connect(workspace, "sk-bad")
    with pytest.raises(AuthExpiredError):
        openai_images.generate(workspace.pk, "a desk")
    assert openai_images.health_check(connection).status == ConnectionStatus.EXPIRED


def test_no_connection_means_no_images(workspace, openai):
    assert not openai_images.has_key(workspace.pk)
    with pytest.raises(ValidationFailedError):
        openai_images.generate(workspace.pk, "a desk")


def test_identify_checks_the_key(openai):
    assert openai_images.identify("sk-good")[0] == "openai-images"
    with pytest.raises(ValidationFailedError):
        openai_images.identify("sk-bad")
