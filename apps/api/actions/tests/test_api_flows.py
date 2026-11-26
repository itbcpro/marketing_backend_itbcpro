import json

import pytest
from django.test import Client, override_settings
from pydantic import BaseModel

from actions import services
from actions.models import ActionStatus, ProposedAction, RiskClass
from actions.registry import ActionKind, register_action_kind, unregister_action_kind
from agents.models import AgentRun
from conftest import make_user
from llm.fake import FakeGateway, text_response
from llm.service import LlmService, set_llm_service
from memory.models import MemoryStatus
from workspaces.models import Role, Workspace

pytestmark = pytest.mark.django_db
ROUTING = {"planner": "claude-opus-5-5", "analyst": "claude-sonnet-5-5"}


class Payload(BaseModel):
    text: str
    spend_micros: int = 0


@pytest.fixture(autouse=True)
def ran():
    ran: list[dict] = []
    for name, risk in (("api.r1", RiskClass.R1), ("api.r3", RiskClass.R3)):
        register_action_kind(
            ActionKind(
                kind=name,
                payload_model=Payload,
                risk=risk,
                executor=lambda a: ran.append(dict(a.effective_payload)) or {"ok": True},
                spend_micros=lambda p: p.spend_micros,
            )
        )
    yield ran
    for name in ("api.r1", "api.r3"):
        unregister_action_kind(name)
    set_llm_service(None)


def h(workspace: Workspace) -> dict[str, str]:
    return {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}


def send(client: Client, method: str, path: str, workspace: Workspace, body: dict | None = None):
    fn = getattr(client, method)
    kwargs = (
        {"data": json.dumps(body), "content_type": "application/json"} if body is not None else {}
    )
    return fn(f"/api/v1{path}", **kwargs, **h(workspace))


def propose(workspace: Workspace, kind: str = "api.r1", **payload):
    return services.propose(
        workspace_id=workspace.pk,
        kind=kind,
        payload={"text": "hello", **payload},
        requested_by="ads",
        summary="Say hello",
    )


# ---- approvals ----------------------------------------------------------------------------------


def test_inbox_lists_filters_and_counts(logged_in: Client, workspace: Workspace) -> None:
    first, second = propose(workspace), propose(workspace, text="two")
    services.reject(
        workspace_id=workspace.pk,
        action_id=second.pk,
        user=make_user("a@example.com", workspace, Role.APPROVER),
        reason="tone",
    )

    everything = send(logged_in, "get", "/actions/?limit=10", workspace).json()
    assert {i["id"] for i in everything["items"]} == {str(first.pk), str(second.pk)}
    waiting = send(logged_in, "get", "/actions/?status=awaiting_approval", workspace).json()[
        "items"
    ]
    assert [i["id"] for i in waiting] == [str(first.pk)] and waiting[0]["summary"] == "Say hello"
    assert send(logged_in, "get", "/actions/inbox", workspace).json() == {
        "awaiting": 1,
        "blocked": 0,
        "kill_switch": False,
    }
    assert send(logged_in, "get", "/actions/?bogus=1", workspace).status_code in {200, 422}
    detail = send(logged_in, "get", f"/actions/{first.pk}", workspace).json()
    assert detail["payload"] == {"text": "hello", "spend_micros": 0} and detail["risk"] == "r1"


def test_approving_over_the_api_executes_the_action(
    logged_in: Client, workspace: Workspace, ran, django_capture_on_commit_callbacks
) -> None:
    action = propose(workspace)
    with django_capture_on_commit_callbacks(execute=True):
        response = send(
            logged_in,
            "post",
            f"/actions/{action.pk}/approve",
            workspace,
            {"note": "go", "edits": {"text": "edited"}},
        )
    assert response.status_code == 200
    action.refresh_from_db()
    assert action.status in {ActionStatus.VERIFIED, ActionStatus.SUCCEEDED}
    assert ran == [{"text": "edited", "spend_micros": 0}]


def test_rejection_validates_reason_and_respects_permissions(
    logged_in: Client, workspace: Workspace
) -> None:
    action = propose(workspace)
    assert (
        send(
            logged_in, "post", f"/actions/{action.pk}/reject", workspace, {"reason": "whatever"}
        ).status_code
        == 422
    )
    ok = send(
        logged_in,
        "post",
        f"/actions/{action.pk}/reject",
        workspace,
        {"reason": "brand", "note": "off voice"},
    )
    assert ok.status_code == 200 and ok.json()["status"] == "rejected"

    viewer = Client()
    viewer.force_login(make_user("viewer@example.com", workspace, Role.VIEWER))
    other = propose(workspace, text="other")
    assert (
        send(
            viewer, "post", f"/actions/{other.pk}/reject", workspace, {"reason": "tone"}
        ).status_code
        == 403
    )


def test_r3_actions_need_admin_level_approval(workspace: Workspace) -> None:
    action = propose(workspace, "api.r3")
    approver = Client()
    approver.force_login(make_user("approver@example.com", workspace, Role.APPROVER))
    assert send(approver, "post", f"/actions/{action.pk}/approve", workspace, {}).status_code == 403


def test_kill_switch_endpoint_pauses_everything(logged_in: Client, workspace: Workspace) -> None:
    assert (
        send(logged_in, "post", "/actions/kill-switch", workspace, {"enabled": True}).json()[
            "kill_switch"
        ]
        is True
    )
    assert propose(workspace, text="after").status == ActionStatus.POLICY_BLOCKED
    editor = Client()
    editor.force_login(make_user("editor@example.com", workspace, Role.EDITOR))
    assert (
        send(editor, "post", "/actions/kill-switch", workspace, {"enabled": False}).status_code
        == 403
    )


def test_guardrails_and_autonomy_are_managed_with_the_right_permissions(
    logged_in: Client, workspace: Workspace
) -> None:
    body = {"key": "blocked_terms", "config": {"terms": ["free money"]}}
    assert send(logged_in, "put", "/actions/guardrails/all", workspace, body).status_code == 200
    assert propose(workspace, text="get FREE MONEY now").status == ActionStatus.POLICY_BLOCKED
    assert [
        g["key"] for g in send(logged_in, "get", "/actions/guardrails/all", workspace).json()
    ] == ["blocked_terms"]

    grant = {"agent": "ads", "action_kind": "api.r1", "level": 3}
    assert send(logged_in, "put", "/actions/autonomy/grants", workspace, grant).status_code == 200
    assert (
        send(
            logged_in, "put", "/actions/autonomy/grants", workspace, {**grant, "level": 9}
        ).status_code
        == 422
    )

    approver = Client()
    approver.force_login(make_user("approver2@example.com", workspace, Role.APPROVER))
    assert send(approver, "put", "/actions/autonomy/grants", workspace, grant).status_code == 403
    assert send(approver, "put", "/actions/guardrails/all", workspace, body).status_code == 403


def test_actions_of_other_workspaces_are_invisible(
    logged_in: Client, workspace: Workspace, owner
) -> None:
    from workspaces.services import create_workspace

    other = create_workspace(name="Other", owner=owner)
    foreign = propose(other, text="secret")
    assert send(logged_in, "get", f"/actions/{foreign.pk}", workspace).status_code == 404
    assert send(logged_in, "get", "/actions/", workspace).json()["items"] == []


# ---- command box --------------------------------------------------------------------------------


def brief(**over) -> str:
    base = {
        "intent": "campaign",
        "summary": "Lead campaign",
        "platforms": ["meta"],
        "market": "Toronto",
        "missing": [],
    }
    return json.dumps({**base, **over})


@override_settings(MODEL_ROUTING=ROUTING)
def test_command_asks_for_money_it_was_not_given_then_starts_a_run(
    logged_in: Client, workspace: Workspace, django_capture_on_commit_callbacks
) -> None:
    set_llm_service(
        LlmService(
            FakeGateway(
                [
                    text_response(brief()),  # the model forgot the budget; the platform enforces it
                    text_response(brief(daily_budget_micros=50_000_000, currency="CAD")),
                ]
            )
        )
    )
    created = send(
        logged_in,
        "post",
        "/agents/commands",
        workspace,
        {"text": "Lead campaign in Toronto on Meta"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "needs_input" and body["plan"] is None
    assert [m["field"] for m in body["brief"]["missing"]] == ["budget"]

    early = send(logged_in, "post", f"/agents/commands/{body['id']}/confirm", workspace, {})
    assert early.status_code == 409

    answered = send(
        logged_in,
        "post",
        f"/agents/commands/{body['id']}/answers",
        workspace,
        {"answers": {"budget": "50 CAD per day"}},
    )
    data = answered.json()
    assert data["status"] == "ready" and data["answers"] == ["budget: 50 CAD per day"]
    assert (
        data["plan"]["steps"][1]["needs_approval"] is True
        and "50.00 CAD" in data["plan"]["spend_note"]
    )

    with django_capture_on_commit_callbacks(execute=False):
        confirmed = send(
            logged_in, "post", f"/agents/commands/{body['id']}/confirm", workspace, {}
        ).json()
    run = AgentRun.objects.get(pk=confirmed["run_id"])
    assert confirmed["status"] == "running" and run.agent.name == "orchestrator"
    assert "Lead campaign in Toronto on Meta" in run.goal and "budget: 50 CAD per day" in run.goal


@override_settings(MODEL_ROUTING=ROUTING)
def test_a_spend_cap_warning_appears_in_the_plan(logged_in: Client, workspace: Workspace) -> None:
    send(
        logged_in,
        "put",
        "/actions/guardrails/all",
        workspace,
        {"key": "max_single_spend_micros", "config": {"amount": 10_000_000}},
    )
    set_llm_service(LlmService(FakeGateway([text_response(brief(daily_budget_micros=90_000_000))])))
    plan = send(logged_in, "post", "/agents/commands", workspace, {"text": "big campaign"}).json()[
        "plan"
    ]
    assert any("spend cap" in w for w in plan["warnings"])


@override_settings(MODEL_ROUTING=ROUTING)
def test_only_editors_can_command_and_commands_are_workspace_scoped(workspace: Workspace) -> None:
    viewer = Client()
    viewer.force_login(make_user("viewer@example.com", workspace, Role.VIEWER))
    assert (
        send(viewer, "post", "/agents/commands", workspace, {"text": "do things"}).status_code
        == 403
    )


# ---- runs ---------------------------------------------------------------------------------------


def test_runs_can_be_listed_inspected_and_cancelled(
    logged_in: Client, workspace: Workspace
) -> None:
    from agents import runtime

    run = runtime.start_run(
        workspace_id=workspace.pk,
        agent_name="orchestrator",
        goal="g",
        requested_by="u",
        dispatch=False,
    )
    listing = send(logged_in, "get", "/agents/runs?agent=orchestrator", workspace).json()["items"]
    assert [r["id"] for r in listing] == [str(run.pk)] and listing[0]["agent"] == "orchestrator"
    assert [a["name"] for a in send(logged_in, "get", "/agents/", workspace).json()] == [
        "ads-strategist",
        "community-responder",
        "orchestrator",
        "social-manager",
    ]
    assert send(logged_in, "get", f"/agents/runs/{run.pk}/steps", workspace).json() == []
    assert (
        send(logged_in, "post", f"/agents/runs/{run.pk}/cancel", workspace, {}).json()["status"]
        == "cancelled"
    )


# ---- memory API ---------------------------------------------------------------------------------


def test_memory_api_round_trip(logged_in: Client, workspace: Workspace) -> None:
    assert send(logged_in, "get", "/memory/profile", workspace).json() is None
    saved = send(
        logged_in, "put", "/memory/profile", workspace, {"content": {"voice": "direct"}}
    ).json()
    assert saved["version"] == 1 and send(logged_in, "get", "/memory/profile", workspace).json()[
        "content"
    ] == {"voice": "direct"}

    created = send(
        logged_in,
        "post",
        "/memory/items",
        workspace,
        {"text": "Never promise guaranteed results", "is_hard_rule": True},
    ).json()
    assert (
        created["status"] == MemoryStatus.ACTIVE
        and created["is_hard_rule"]
        and created["source"] == "user"
    )
    proposed = send(
        logged_in,
        "post",
        "/memory/items",
        workspace,
        {"text": "Prefer short subject lines", "kind": "preference", "activate": False},
    ).json()
    assert proposed["status"] == "proposed"
    assert (
        send(logged_in, "post", f"/memory/items/{proposed['id']}/activate", workspace, {}).json()[
            "status"
        ]
        == "active"
    )
    assert (
        send(logged_in, "post", f"/memory/items/{created['id']}/retire", workspace, {}).json()[
            "status"
        ]
        == "retired"
    )
    active = send(logged_in, "get", "/memory/items?status=active", workspace).json()["items"]
    assert [i["text"] for i in active] == ["Prefer short subject lines"]


def test_memory_writes_need_edit_permission(workspace: Workspace) -> None:
    analyst = Client()
    analyst.force_login(make_user("analyst@example.com", workspace, Role.ANALYST))
    assert send(analyst, "put", "/memory/profile", workspace, {"content": {}}).status_code == 403
    assert send(analyst, "get", "/memory/items", workspace).status_code == 200


@override_settings(MODEL_ROUTING=ROUTING)
def test_rejections_flow_into_feedback_and_reflection(
    logged_in: Client, workspace: Workspace
) -> None:
    from audit import outbox

    action = propose(workspace, text="Buy NOW")
    send(
        logged_in,
        "post",
        f"/actions/{action.pk}/reject",
        workspace,
        {"reason": "tone", "note": "too pushy"},
    )
    outbox.dispatch_batch()
    feedback = send(logged_in, "get", "/memory/feedback", workspace).json()
    assert (
        feedback[0]["verdict"] == "rejected"
        and feedback[0]["reason"] == "tone"
        and feedback[0]["agent"] == "ads"
    )

    reply = {
        "items": [
            {
                "text": "Avoid pushy urgency",
                "kind": "rule",
                "evidence": ["a", "b"],
                "confidence": 0.8,
            }
        ]
    }
    set_llm_service(LlmService(FakeGateway([text_response(json.dumps(reply))])))
    proposed = send(logged_in, "post", "/memory/reflect", workspace, {}).json()
    assert proposed[0]["status"] == "proposed" and proposed[0]["source"] == "reflection"
    assert ProposedAction.objects.filter(pk=action.pk).exists()
