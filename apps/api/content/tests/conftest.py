import httpx
import pytest

from audit.actors import Actor
from connections import services as connection_services
from connections.models import ConnectionStatus, CredentialKind
from content import gsc_sync, net
from content.google import gsc
from content.tests.fake_google import SITE, FakeGoogle
from content.tests.fake_wordpress import HOST, PASSWORD, USER, FakeWordPress


@pytest.fixture
def wp(monkeypatch) -> FakeWordPress:
    server = FakeWordPress()
    monkeypatch.setattr(net, "transport", httpx.MockTransport(server.handler))
    monkeypatch.setattr(net, "resolver", lambda host, port: ["93.184.216.34"])
    return server


@pytest.fixture
def wp_connection(workspace, wp):
    connection = connection_services.create_connection(
        workspace_id=workspace.pk,
        provider="wordpress",
        external_account_id=f"https://{HOST}",
        display_name="Acme IT",
        actor=Actor.system("test"),
    )
    connection.config = {"form": {"site_url": f"https://{HOST}", "username": USER}}
    connection.save()
    connection_services.store_secret(
        connection, CredentialKind.ACCESS_TOKEN, PASSWORD, actor=Actor.system("test")
    )
    connection_services.set_status(connection, ConnectionStatus.ACTIVE)
    return connection


GOOD_REVIEW = {
    "summary": "A solid guide that needs a sharper title.",
    "verdict": "good",
    "search_intent": "informational",
    "primary_keyword": "hello world guide",
    "secondary_keywords": ["intro"],
    "strengths": ["Clear structure"],
    "problems": [
        {
            "severity": "high",
            "area": "title",
            "evidence": "Title is generic",
            "fix": "Name the topic",
        }
    ],
    "proposals": [
        {
            "field": "seo_title",
            "proposed": "Hello World: The Short Guide for Small Businesses",
            "reason": "Names the audience",
            "impact": "high",
        },
        {
            "field": "meta_description",
            "proposed": "A short, practical hello world guide for small business owners. Read it now.",
            "reason": "Says what the visitor gets",
            "impact": "medium",
        },
        {
            "field": "advice",
            "proposed": "Add a comparison table.",
            "reason": "Scannable",
            "impact": "low",
        },
        {"field": "title", "proposed": "Hello World", "reason": "same as now", "impact": "low"},
    ],
    "faq": [{"question": "What is it?", "answer": ""}],
    "internal_links": [
        {"anchor": "about us", "target_url": "https://wp.example.com/about/", "reason": "Trust"},
        {"anchor": "made up", "target_url": "https://evil.example.org/x", "reason": "invented"},
    ],
    "geo_tips": ["Answer first"],
}


def script_llm(*payloads):
    """Install a fake model that answers each call with the next payload."""
    import json

    from llm.fake import FakeGateway, text_response
    from llm.service import LlmService, set_llm_service

    gateway = FakeGateway([text_response(json.dumps(p)) for p in payloads])
    set_llm_service(LlmService(gateway))
    return gateway


@pytest.fixture(autouse=True)
def reset_llm():
    from llm.service import set_llm_service

    yield
    set_llm_service(None)


@pytest.fixture
def wp_site(workspace, wp_connection):
    from content import services
    from content.models import WebsiteKind

    return services.create_website(
        workspace_id=workspace.pk,
        name="Acme",
        kind=WebsiteKind.WORDPRESS,
        connection_id=wp_connection.pk,
        actor=Actor.system("test"),
    )


@pytest.fixture
def synced(wp_site):
    from content import sync

    sync.execute_run(sync.start_run(wp_site, full=True))
    return wp_site


@pytest.fixture
def post(synced):
    from content.models import ContentItem

    return ContentItem.objects.get(website=synced, key="wp:post:1")


TODAY = __import__("datetime").date(2026, 10, 1)


@pytest.fixture
def google(monkeypatch) -> FakeGoogle:
    server = FakeGoogle()
    monkeypatch.setattr(gsc, "transport", httpx.MockTransport(server.handler))
    monkeypatch.setattr(gsc_sync, "today", lambda: TODAY)
    return server


@pytest.fixture
def google_keys(workspace):
    connection = connection_services.create_connection(
        workspace_id=workspace.pk,
        provider="google_search_app",
        external_account_id="google-search-app",
        display_name="keys",
        actor=Actor.system("test"),
    )
    system = Actor.system("test")
    connection_services.store_secret(connection, CredentialKind.API_KEY, "client-id", actor=system)
    connection_services.store_secret(
        connection, CredentialKind.CLIENT_SECRET, "client-secret", actor=system
    )
    connection_services.set_status(connection, ConnectionStatus.ACTIVE)
    return connection


@pytest.fixture
def google_connection(workspace, google, google_keys):
    from datetime import timedelta

    from django.utils import timezone

    connection = connection_services.create_connection(
        workspace_id=workspace.pk,
        provider="google_search",
        external_account_id="owner@example.com",
        display_name="Google (owner@example.com)",
        actor=Actor.system("test"),
    )
    system = Actor.system("test")
    connection_services.store_secret(connection, CredentialKind.ACCESS_TOKEN, "tok-1", actor=system)
    connection_services.store_secret(
        connection, CredentialKind.REFRESH_TOKEN, "refresh-1", actor=system
    )
    connection.expires_at = timezone.now() + timedelta(hours=1)
    connection.granted_scopes = [
        "openid",
        "email",
        "https://www.googleapis.com/auth/webmasters.readonly",
    ]
    connection.save()
    connection_services.set_status(connection, ConnectionStatus.ACTIVE)
    return connection


@pytest.fixture
def linked(wp_site, google_connection):
    from content import search

    search.link(wp_site, google_connection, SITE)
    return wp_site
