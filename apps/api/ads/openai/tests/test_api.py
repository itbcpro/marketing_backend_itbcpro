# ruff: noqa: F811 - pytest fixtures are imported and re-declared as arguments
import json

import httpx
import pytest
from django.test import Client

from ads.openai.tests.test_openai_ads import (  # noqa: F401 - fixtures
    FakeAdsServer,
    connection,
    server,
)
from conftest import make_user
from workspaces.models import Role

pytestmark = pytest.mark.django_db

BASE = "/api/v1/ads/openai"


def _h(workspace):
    return {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}


def _post(client, workspace, body):
    return client.post(
        f"{BASE}/changes", data=json.dumps(body), content_type="application/json", **_h(workspace)
    )


def test_overview_lists_the_account_and_campaigns(logged_in, workspace, connection, server) -> None:
    server.rows["cam_1"] = {"id": "cam_1", "name": "Spring", "status": "paused"}
    out = logged_in.get(
        f"{BASE}/overview", {"connection_id": str(connection.pk)}, **_h(workspace)
    ).json()
    assert out["account"]["id"] == "adacct_1"
    assert [c["id"] for c in out["campaigns"]] == ["cam_1"]


def test_insights_validates_and_returns_rows(logged_in, workspace, connection, server) -> None:
    query = {
        "connection_id": str(connection.pk),
        "date_from": "2026-09-01",
        "date_to": "2026-09-07",
        "segment": "country",
    }
    response = logged_in.get(f"{BASE}/insights", query, **_h(workspace))
    assert response.status_code == 200 and response.json()["rows"][0]["clicks"] == 3
    bad = logged_in.get(f"{BASE}/insights", {**query, "segment": "colour"}, **_h(workspace))
    assert bad.status_code in {400, 422}
    too_long = logged_in.get(
        f"{BASE}/insights", {**query, "date_from": "2024-01-01"}, **_h(workspace)
    )
    assert too_long.status_code in {400, 422}


def test_billing_reports_spend_and_tolerates_missing_billing_permission(
    logged_in, workspace, connection, server
) -> None:
    ok = logged_in.get(
        f"{BASE}/billing", {"connection_id": str(connection.pk)}, **_h(workspace)
    ).json()
    assert ok["month_to_date"] == 1.5 and ok["spend_limits_available"] is True

    server.fail["no-billing"] = 1
    limited = logged_in.get(
        f"{BASE}/billing", {"connection_id": str(connection.pk)}, **_h(workspace)
    ).json()
    assert limited["spend_limits"] is None and limited["spend_limits_available"] is False


def test_an_edit_becomes_an_action_and_can_be_applied_immediately(
    logged_in, workspace, connection, server, django_capture_on_commit_callbacks
) -> None:
    server.rows["cam_1"] = {"id": "cam_1", "name": "Spring", "status": "paused"}
    body = {
        "connection_id": str(connection.pk),
        "kind": "campaign.update",
        "payload": {"campaign_id": "cam_1", "name": "Spring sale"},
    }
    queued = _post(logged_in, workspace, body).json()
    assert queued["status"] == "awaiting_approval" and queued["applied"] is False

    with django_capture_on_commit_callbacks(execute=True):
        applied = _post(
            logged_in,
            workspace,
            {
                **body,
                "payload": {"campaign_id": "cam_1", "name": "Spring sale 2"},
                "apply_now": True,
            },
        ).json()
    assert applied["applied"] is True
    assert server.rows["cam_1"]["name"] == "Spring sale 2"


def test_an_editor_can_propose_but_not_apply(workspace, connection, server) -> None:
    server.rows["cam_1"] = {"id": "cam_1", "name": "Spring", "status": "paused"}
    editor = make_user("ed@example.com", workspace, Role.EDITOR)
    client = Client()
    client.force_login(editor)
    result = _post(
        client,
        workspace,
        {
            "connection_id": str(connection.pk),
            "kind": "campaign.update",
            "payload": {"campaign_id": "cam_1", "name": "Nope nope"},
            "apply_now": True,
        },
    ).json()
    assert result["applied"] is False and result["status"] == "awaiting_approval"


def test_unknown_change_kinds_and_foreign_connections_are_rejected(
    logged_in, workspace, connection, server
) -> None:
    bad = _post(
        logged_in,
        workspace,
        {"connection_id": str(connection.pk), "kind": "does.not.exist", "payload": {}},
    )
    assert bad.status_code in {400, 422}
    missing = logged_in.get(
        f"{BASE}/overview",
        {"connection_id": "00000000-0000-0000-0000-000000000000"},
        **_h(workspace),
    )
    assert missing.status_code == 404


def test_connecting_by_key_discovers_the_account_id(logged_in, workspace, monkeypatch) -> None:
    from dataclasses import replace

    from ads.openai import client as openai_client
    from connections.providers import get_provider, register_provider

    def fake_identify(key: str) -> tuple[str, str]:
        assert key == "sk-test-key"
        return "adacct_77", "Acme Ads"

    original = get_provider("openai_ads")
    register_provider(replace(original, identify=fake_identify))
    monkeypatch.setattr(
        "ads.openai.actions.client_factory",
        lambda c: openai_client.OpenAiAdsClient(
            c,
            api_key="sk-test-key",
            transport=httpx.MockTransport(FakeAdsServer().handler),
            throttle=lambda _: None,
        ),
    )
    try:
        response = logged_in.post(
            "/api/v1/connections/",
            data=json.dumps({"provider": "openai_ads", "fields": {"api_key": "sk-test-key"}}),
            content_type="application/json",
            **_h(workspace),
        )
    finally:
        register_provider(original)
    assert response.status_code == 200, response.content
    created = response.json()
    assert created["external_account_id"] == "adacct_77" and created["status"] == "active"
