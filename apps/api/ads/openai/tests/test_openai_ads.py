import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import ValidationError

from actions import services
from actions.models import ActionStatus, Guardrail, RiskClass
from actions.registry import get_action_kind
from ads.openai import actions, schemas
from ads.openai.client import OpenAiAdsClient
from agents.tools import ToolContext, ToolKind, execute_tool, registry
from audit.actors import Actor
from conftest import make_user
from connections import ratelimit
from connections import services as connection_services
from connections.models import ConnectionStatus
from core.errors import (
    AuthExpiredError,
    PermanentProviderError,
    RateLimitedError,
    RetryableProviderError,
)
from workspaces.models import Role

pytestmark = pytest.mark.django_db


def _fsa(code: str, city: str) -> dict:
    return {
        "id": f"91{abs(hash(code)) % 100000}",
        "name": code,
        "canonical_name": f"{code}, {city}, BC, Canada",
        "country_code": "CA",
        "type": "postal_code",
    }


PLACES = [
    {
        "id": "2000009",
        "name": "British Columbia",
        "canonical_name": "British Columbia, Canada",
        "country_code": "CA",
        "type": "region",
    },
    {
        "id": "9105617",
        "name": "V6B",
        "canonical_name": "V6B, Vancouver, BC, Canada",
        "country_code": "CA",
        "type": "postal_code",
    },
    {
        "id": "9105600",
        "name": "V5K",
        "canonical_name": "V5K, Vancouver, BC, Canada",
        "country_code": "CA",
        "type": "postal_code",
    },
    {
        "id": "9043964",
        "name": "43964",
        "canonical_name": "43964, Vancouver, Ohio, United States",
        "country_code": "US",
        "type": "postal_code",
    },
]


class FakeAdsServer:
    """Just enough of the Advertiser API to exercise the adapter end to end."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict | None, dict[str, str]]] = []
        self.fail: dict[str, int] = {}
        self._n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911, PLR0912, C901
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body, dict(request.headers)))
        path = request.url.path.removeprefix("/v1")
        if path in self.fail:
            return httpx.Response(self.fail[path], text="boom", headers={"Retry-After": "2"})
        if path == "/ad_account":
            return httpx.Response(
                200, json={"id": "adacct_1", "status": "active", "review": {"status": "approved"}}
            )
        if path.endswith("/insights"):
            return httpx.Response(200, json={"data": [{"spend": 1.5, "clicks": 3}]})
        if path == "/ad_account/spend_limit_windows":
            if "no-billing" in self.fail:
                return httpx.Response(403, text="forbidden")
            return httpx.Response(200, json={"data": [], "daily_limit": None, "revision": 0})
        if path == "/upload":
            return httpx.Response(200, json={"file_id": "file_1"})
        if path == "/geo_lookup/search":
            wanted = request.url.params.get("q", "").casefold()
            data = [p for p in PLACES if wanted in p["canonical_name"].casefold()]
            return httpx.Response(200, json={"count": len(data), "results": data})
        parts = path.strip("/").split("/")
        if request.method == "POST" and len(parts) == 1:
            self._n += 1
            row = {"id": f"{parts[0][:3]}_{self._n}", **body}
            self.rows[row["id"]] = row
            return httpx.Response(200, json=row)
        if request.method == "POST" and len(parts) == 2:
            self.rows[parts[1]].update(body)
            return httpx.Response(200, json=self.rows[parts[1]])
        if request.method == "POST" and len(parts) == 3:
            state = {"activate": "active", "pause": "paused", "archive": "archived"}[parts[2]]
            self.rows[parts[1]]["status"] = state
            return httpx.Response(200, json=self.rows[parts[1]])
        if request.method == "GET" and len(parts) == 2:
            if parts[1] not in self.rows:
                return httpx.Response(404, text="missing")
            return httpx.Response(200, json=self.rows[parts[1]])
        if request.method == "GET" and len(parts) == 1:
            data = [r for r in self.rows.values() if r["id"].startswith(parts[0][:3])]
            return httpx.Response(200, json={"object": "list", "data": data, "has_more": False})
        return httpx.Response(404, text="nope")


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeAdsServer:
    fake = FakeAdsServer()
    monkeypatch.setattr(
        actions,
        "client_factory",
        lambda connection: OpenAiAdsClient(
            connection,
            api_key="test-key",
            transport=httpx.MockTransport(fake.handler),
            throttle=lambda _: None,
        ),
    )
    return fake


@pytest.fixture
def connection(workspace):
    connection = connection_services.create_connection(
        workspace_id=workspace.pk,
        provider="openai_ads",
        external_account_id="adacct_1",
        display_name="Acme",
        actor=Actor.system("test"),
    )
    connection_services.set_status(connection, ConnectionStatus.ACTIVE)
    return connection


def _propose(workspace, connection, suffix: str, payload: dict, **kw):
    return services.propose(
        workspace_id=workspace.pk,
        kind=actions.PREFIX + suffix,
        payload={"connection_id": str(connection.pk), **payload},
        requested_by="ads-strategist",
        **kw,
    )


def _approve(workspace, action, capture):
    approver = make_user(f"appr-{action.pk}@example.com", workspace, Role.APPROVER)
    with capture(execute=True):
        services.approve(workspace_id=workspace.pk, action_id=action.pk, user=approver)
    action.refresh_from_db()
    return action


CAMPAIGN = {"name": "Spring launch", "budget": {"daily_spend_limit_micros": 25_000_000}}


# ---- client ------------------------------------------------------------------------------------


def _client(handler: Callable[[httpx.Request], httpx.Response], connection) -> OpenAiAdsClient:
    return OpenAiAdsClient(
        connection, api_key="k", transport=httpx.MockTransport(handler), throttle=lambda _: None
    )


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthExpiredError),
        (403, AuthExpiredError),
        (400, PermanentProviderError),
        (404, PermanentProviderError),
        (500, RetryableProviderError),
        (409, RetryableProviderError),
    ],
)
def test_client_maps_http_errors_to_domain_errors(connection, status, error) -> None:
    with _client(lambda r: httpx.Response(status, text="x"), connection) as client:
        with pytest.raises(error):
            client.get_account()


def test_rate_limit_response_penalizes_the_bucket(connection, monkeypatch) -> None:
    penalties: list[float] = []
    monkeypatch.setattr(ratelimit, "penalize", lambda *a, seconds, **k: penalties.append(seconds))
    with _client(lambda r: httpx.Response(429, headers={"Retry-After": "7"}), connection) as client:
        with pytest.raises(RateLimitedError):
            client.get_account()
    assert penalties == [7.0]


def test_client_sends_bearer_and_idempotency_key_and_follows_cursors(connection) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "cmpn_1"})
        after = request.url.params.get("after")
        page = (
            {"data": [{"id": "b"}], "has_more": False}
            if after
            else {
                "data": [{"id": "a"}],
                "has_more": True,
                "last_id": "a",
            }
        )
        return httpx.Response(200, json=page)

    with _client(handler, connection) as client:
        rows = client.list_resource("campaigns")
        client.create("campaigns", {"name": "x"}, idempotency_key="k-1")
    assert [r["id"] for r in rows] == ["a", "b"]
    assert seen[0].headers["Authorization"] == "Bearer k"
    assert seen[-1].headers["Idempotency-Key"] == "k-1"


# ---- payload rules -----------------------------------------------------------------------------


def test_budget_needs_exactly_one_limit_at_or_above_the_minimum() -> None:
    with pytest.raises(ValidationError):
        schemas.Budget()
    with pytest.raises(ValidationError):
        schemas.Budget(daily_spend_limit_micros=2_000_000, lifetime_spend_limit_micros=2_000_000)
    with pytest.raises(ValidationError):
        schemas.Budget(daily_spend_limit_micros=10)


@pytest.mark.parametrize(
    "bidding",
    [
        {"strategy": "fixed_bid", "billing_event_type": "click"},
        {"strategy": "maximize_clicks", "billing_event_type": "impression"},
        {"strategy": "maximize_clicks", "billing_event_type": "click", "max_bid_micros": 5},
    ],
)
def test_incompatible_bidding_is_rejected_before_approval(bidding) -> None:
    with pytest.raises(ValidationError):
        schemas.Bidding(**bidding)


def test_conversion_campaigns_need_an_event_and_others_must_not_have_one() -> None:
    base = {
        "connection_id": "00000000-0000-0000-0000-000000000001",
        "name": "Buyers",
        "budget": {"daily_spend_limit_micros": 5_000_000},
    }
    with pytest.raises(ValidationError):
        schemas.CampaignCreate(**base, objective="conversions")
    with pytest.raises(ValidationError):
        schemas.CampaignCreate(**base, objective="clicks", conversion_event_setting_id="ces_1")


def test_created_resources_are_always_paused() -> None:
    campaign = schemas.CampaignCreate(
        connection_id="00000000-0000-0000-0000-000000000001", **CAMPAIGN, countries=["CA"]
    )
    assert campaign.api_body()["status"] == "paused"
    assert campaign.api_body()["targeting"] == {"locations": {"countries": ["CA"]}}
    placed = schemas.CampaignCreate(
        connection_id="00000000-0000-0000-0000-000000000001",
        **CAMPAIGN,
        countries=["CA"],
        location_ids=["3000200"],
        excluded_location_ids=["2000043"],
    )
    assert placed.api_body()["targeting"] == {
        "locations": {"include": [{"id": "3000200"}]},
        "excluded_locations": {"include": [{"id": "2000043"}]},
    }
    with pytest.raises(ValueError):
        schemas.CampaignCreate(
            connection_id="00000000-0000-0000-0000-000000000001",
            **CAMPAIGN,
            location_ids=["1"],
            excluded_location_ids=["1"],
        )
    assert "status" not in schemas.CampaignCreate.model_fields


# ---- action flow -------------------------------------------------------------------------------


def test_campaign_creation_is_approved_created_paused_and_verified(
    workspace, connection, server, django_capture_on_commit_callbacks
) -> None:
    action = _propose(workspace, connection, "campaign.create", CAMPAIGN)
    assert action.status == ActionStatus.AWAITING_APPROVAL
    assert action.risk == RiskClass.R2 and action.spend_micros == 25_000_000
    assert action.after["request"]["status"] == "paused"
    assert server.calls == []  # nothing reaches the platform before approval

    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    assert done.status == ActionStatus.VERIFIED
    created = next(c for c in server.calls if c[0] == "POST" and c[1].endswith("/campaigns"))
    assert created[2]["status"] == "paused" and created[3]["idempotency-key"] == f"act-{action.pk}"
    assert done.result["resource"] == "campaigns"


def test_activation_carries_spend_and_can_be_rolled_back_by_pausing(
    workspace, connection, server, django_capture_on_commit_callbacks
) -> None:
    server.rows["cmp_9"] = {"id": "cmp_9", "status": "paused"}
    action = _propose(
        workspace,
        connection,
        "activate",
        {"resource": "campaigns", "resource_id": "cmp_9", "daily_spend_micros": 20_000_000},
    )
    assert action.risk == RiskClass.R2 and action.spend_micros == 20_000_000
    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    assert done.status == ActionStatus.VERIFIED and server.rows["cmp_9"]["status"] == "active"

    owner = make_user("rb@example.com", workspace, Role.APPROVER)
    rollback = services.request_rollback(workspace_id=workspace.pk, action_id=done.pk, user=owner)
    assert rollback.kind == actions.PREFIX + "pause" and rollback.risk == RiskClass.R1


def test_spend_cap_blocks_an_activation_that_would_exceed_it(workspace, connection, server) -> None:
    Guardrail.objects.create(
        workspace=workspace, key="max_single_spend_micros", config={"amount": 10_000_000}
    )
    action = _propose(
        workspace,
        connection,
        "activate",
        {"resource": "campaigns", "resource_id": "cmp_9", "daily_spend_micros": 20_000_000},
    )
    assert action.status == ActionStatus.POLICY_BLOCKED and server.calls == []


def test_archive_is_r3_and_irreversible() -> None:
    spec = get_action_kind(actions.PREFIX + "archive")
    assert spec.risk == RiskClass.R3 and spec.rollback_kind is None


def test_budget_update_stores_the_previous_budget_for_rollback(
    workspace, connection, server, django_capture_on_commit_callbacks
) -> None:
    server.rows["cmp_1"] = {"id": "cmp_1", "budget": {"daily_spend_limit_micros": 5_000_000}}
    action = _propose(
        workspace,
        connection,
        "campaign.update_budget",
        {"campaign_id": "cmp_1", "budget": {"daily_spend_limit_micros": 9_000_000}},
    )
    assert action.before == {"budget": {"daily_spend_limit_micros": 5_000_000}}
    done = _approve(workspace, action, django_capture_on_commit_callbacks)
    assert done.status == ActionStatus.VERIFIED
    plan = get_action_kind(done.kind).rollback_payload(done)
    assert plan["budget"] == {"daily_spend_limit_micros": 5_000_000}


def test_a_connection_of_another_workspace_is_never_used(
    workspace, connection, server, django_capture_on_commit_callbacks
) -> None:
    from workspaces.models import Workspace

    other = Workspace.objects.create(name="Other", slug="other")
    action = _propose(other, connection, "image.upload", {"image_url": "https://example.com/a.png"})
    approver = make_user("x@example.com", other, Role.APPROVER)
    with django_capture_on_commit_callbacks(execute=True):
        services.approve(workspace_id=other.pk, action_id=action.pk, user=approver)
    action.refresh_from_db()
    assert action.status == ActionStatus.FAILED_FINAL and server.calls == []


# ---- tools and provider ------------------------------------------------------------------------


def test_every_write_tool_only_proposes_and_defaults_list_all_tools() -> None:
    from ads.openai.tools import TOOL_NAMES, WRITE_TOOLS
    from agents.defaults import OPENAI_ADS_TOOLS

    assert sorted(OPENAI_ADS_TOOLS) == sorted(TOOL_NAMES)
    assert all(registry.get(name).kind == ToolKind.WRITE for name in WRITE_TOOLS)


def test_agent_tool_proposes_an_action_without_touching_the_platform(
    workspace, connection, server
) -> None:
    from agents import runtime

    run = runtime.start_run(
        workspace_id=workspace.pk,
        agent_name="ads-strategist",
        goal="x",
        requested_by="test",
        dispatch=False,
    )
    ctx = ToolContext(run=run, agent_name="ads-strategist")
    outcome = execute_tool(
        registry,
        ["openai_ads_create_campaign"],
        ctx,
        "openai_ads_create_campaign",
        {"connection_id": str(connection.pk), **CAMPAIGN, "summary": "Spring launch"},
    )
    assert "waiting for human approval" in outcome.text and server.calls == []


def test_overview_tool_reads_the_account(workspace, connection, server) -> None:
    from agents import runtime

    server.rows["cam_1"] = {"id": "cam_1", "name": "A", "status": "paused"}
    run = runtime.start_run(
        workspace_id=workspace.pk,
        agent_name="ads-strategist",
        goal="x",
        requested_by="test",
        dispatch=False,
    )
    outcome = execute_tool(
        registry,
        ["openai_ads_overview"],
        ToolContext(run=run, agent_name="a"),
        "openai_ads_overview",
        {},
    )
    assert not outcome.is_error and "cam_1" in outcome.text and str(connection.pk) in outcome.text


def test_health_check_reports_active_and_expired(connection, monkeypatch) -> None:
    from ads.openai.provider import health_check

    good = FakeAdsServer()
    monkeypatch.setattr(
        actions,
        "client_factory",
        lambda c: OpenAiAdsClient(
            c, api_key="k", transport=httpx.MockTransport(good.handler), throttle=lambda _: None
        ),
    )
    assert health_check(connection).status == ConnectionStatus.ACTIVE
    good.fail["/ad_account"] = 401
    assert health_check(connection).status == ConnectionStatus.EXPIRED
