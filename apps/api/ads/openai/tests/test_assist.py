# ruff: noqa: F811 - pytest fixtures are imported and re-declared as arguments
import json

import pytest

from ads.openai.tests.test_openai_ads import (  # noqa: F401 - fixtures
    FakeAdsServer,
    connection,
    server,
)
from llm.fake import FakeGateway, text_response
from llm.service import LlmService, set_llm_service

pytestmark = pytest.mark.django_db

BASE = "/api/v1/ads/openai/assist"

DRAFT = {
    "campaign_name": "Managed IT - Vancouver",
    "objective": "clicks",
    "daily_budget": 25.0,
    "countries": ["CA"],
    "platforms": ["web", "ios_app", "android_app"],
    "rationale": "A modest test budget while there is no history.",
    "ad_groups": [
        {
            "name": "Nonprofit IT support",
            "theme": "IT support for nonprofits",
            "context_hints": ["managed IT for nonprofits in Vancouver"],
            "strategy": "maximize_clicks",
            "max_bid": None,
            "rationale": "One theme per group keeps results readable.",
            "ads": [
                {
                    "name": "Nonprofit IT A",
                    "title": "IT support for Vancouver nonprofits",
                    "body": "Managed IT with predictable monthly pricing.",
                    "url": "https://itbcpro.ca/nonprofit",
                }
            ],
        }
    ],
    "checks": ["Wait a week before judging results."],
    "warnings": [],
}


def _h(workspace):
    return {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}


@pytest.fixture(autouse=True)
def _reset_llm(monkeypatch):
    # Commits never happen inside a test transaction, so run the queued turn right away.
    monkeypatch.setattr("ads.openai.sessions.transaction.on_commit", lambda fn: fn())
    yield
    set_llm_service(None)


def _post(client, workspace, path, body=None):
    return client.post(
        f"{BASE}/{path}",
        data=json.dumps(body or {}),
        content_type="application/json",
        **_h(workspace),
    )


def _get(client, workspace, path, query=None):
    return client.get(f"{BASE}/{path}", query or {}, **_h(workspace))


def _start(client, workspace, connection, goal="Get leads for managed IT"):
    return _post(client, workspace, "sessions", {"connection_id": str(connection.pk), "goal": goal})


def _llm(*payloads):
    set_llm_service(LlmService(FakeGateway([text_response(json.dumps(p)) for p in payloads])))


QUESTIONS = {"status": "needs_input", "questions": ["What is the daily budget?"], "draft": None}


def test_a_conversation_is_saved_and_can_be_reloaded(logged_in, workspace, connection, server):
    _llm(QUESTIONS)
    started = _start(logged_in, workspace, connection)
    assert started.status_code == 200, started.content
    reloaded = _get(logged_in, workspace, f"sessions/{started.json()['id']}").json()
    assert reloaded["status"] == "needs_input"
    assert reloaded["questions"] == ["What is the daily budget?"]
    assert [e["type"] for e in reloaded["history"]] == ["goal", "questions"]
    listing = _get(logged_in, workspace, "sessions", {"connection_id": str(connection.pk)}).json()
    assert [row["id"] for row in listing] == [started.json()["id"]]


def test_answers_continue_the_same_thread_and_reach_the_model(
    logged_in, workspace, connection, server
):
    gateway = FakeGateway(
        [text_response(json.dumps(QUESTIONS)), text_response(json.dumps(_draft_payload()))]
    )
    set_llm_service(LlmService(gateway))
    sid = _start(logged_in, workspace, connection).json()["id"]
    response = _post(
        logged_in,
        workspace,
        f"sessions/{sid}/answers",
        {"answers": [{"question": "What is the daily budget?", "answer": "25 CAD"}]},
    )
    assert response.status_code == 200, response.content
    session = _get(logged_in, workspace, f"sessions/{sid}").json()
    assert (
        session["status"] == "draft" and session["draft"]["campaign_name"] == DRAFT["campaign_name"]
    )
    assert [e["type"] for e in session["history"]] == ["goal", "questions", "answers", "draft"]
    sent = json.dumps(gateway.requests[-1][0].system[0].text)
    assert "25 CAD" in sent and "What is the daily budget?" in sent


def _draft_payload(url="https://itbcpro.ca/nonprofit"):
    draft = json.loads(json.dumps(DRAFT))
    draft["ad_groups"][0]["ads"][0]["url"] = url
    return {"status": "draft", "questions": [], "draft": draft}


def test_feedback_refines_the_saved_draft(logged_in, workspace, connection, server):
    _llm(_draft_payload(), _draft_payload())
    sid = _start(logged_in, workspace, connection).json()["id"]
    response = _post(logged_in, workspace, f"sessions/{sid}/feedback", {"feedback": "Lower budget"})
    assert response.status_code == 200, response.content
    history = _get(logged_in, workspace, f"sessions/{sid}").json()["history"]
    assert [e["type"] for e in history] == ["goal", "draft", "feedback", "draft"]


def test_answering_a_conversation_that_is_not_waiting_is_a_conflict(
    logged_in, workspace, connection, server
):
    _llm(_draft_payload())
    sid = _start(logged_in, workspace, connection).json()["id"]
    response = _post(
        logged_in,
        workspace,
        f"sessions/{sid}/answers",
        {"answers": [{"question": "q", "answer": "a"}]},
    )
    assert response.status_code == 409


def test_a_failed_turn_is_saved_and_can_be_retried(logged_in, workspace, connection, server):
    set_llm_service(
        LlmService(
            FakeGateway([text_response("not json")] * 3 + [text_response(json.dumps(QUESTIONS))])
        )
    )
    sid = _start(logged_in, workspace, connection).json()["id"]
    failed = _get(logged_in, workspace, f"sessions/{sid}").json()
    assert failed["status"] == "failed" and failed["error_code"]
    retried = _post(logged_in, workspace, f"sessions/{sid}/retry").json()
    assert retried["status"] in {"thinking", "needs_input"}
    assert _get(logged_in, workspace, f"sessions/{sid}").json()["status"] == "needs_input"


def test_archived_and_foreign_conversations_are_not_reachable(
    logged_in, workspace, connection, server
):
    from django.test import Client

    from conftest import make_user
    from workspaces.models import Role, Workspace

    _llm(QUESTIONS)
    sid = _start(logged_in, workspace, connection).json()["id"]
    other = Workspace.objects.create(name="Other", slug="other-ws")
    stranger = make_user("stranger@example.com", other, Role.EDITOR)
    foreign = Client()
    foreign.force_login(stranger)
    assert _get(foreign, other, f"sessions/{sid}").status_code == 404

    deleted = logged_in.delete(f"{BASE}/sessions/{sid}", **_h(workspace))
    assert deleted.status_code == 204
    assert _get(logged_in, workspace, f"sessions/{sid}").status_code == 404


def _many_ads(draft_payload, groups, per_group):
    draft = draft_payload["draft"]
    template = draft["ad_groups"][0]
    draft["ad_groups"] = []
    for g in range(groups):
        group = json.loads(json.dumps(template))
        group["name"] = f"Group {g + 1}"
        group["ads"] = [
            {
                **template["ads"][0],
                "name": f"Ad {g + 1}.{a + 1}",
                "title": f"Headline {g + 1}.{a + 1}",
            }
            for a in range(per_group)
        ]
        draft["ad_groups"].append(group)
    return draft_payload


def test_the_number_of_ads_asked_for_is_enforced_with_one_retry(
    logged_in, workspace, connection, server
):
    short = _many_ads(_draft_payload(), 2, 2)
    short["draft"]["requested_ads"] = 6
    right = _many_ads(_draft_payload(), 2, 3)
    right["draft"]["requested_ads"] = 6
    gateway = FakeGateway([text_response(json.dumps(short)), text_response(json.dumps(right))])
    set_llm_service(LlmService(gateway))
    sid = _start(logged_in, workspace, connection, goal="6 ads for managed IT").json()["id"]
    draft = _get(logged_in, workspace, f"sessions/{sid}").json()["draft"]
    assert sum(len(g["ads"]) for g in draft["ad_groups"]) == 6
    assert not any("You asked for" in w for w in draft["warnings"])
    assert "exactly 6 are required" in gateway.requests[-1][0].messages[-1].content[0].text


def test_a_count_that_stays_wrong_is_reported_as_a_warning(
    logged_in, workspace, connection, server
):
    wrong = _many_ads(_draft_payload(), 1, 2)
    wrong["draft"]["requested_ads"] = 5
    _llm(wrong, wrong)
    sid = _start(logged_in, workspace, connection).json()["id"]
    draft = _get(logged_in, workspace, f"sessions/{sid}").json()["draft"]
    assert any("You asked for 5 ads but the draft has 2" in w for w in draft["warnings"])


def test_places_resolve_to_regions_cities_and_postal_areas(
    logged_in, workspace, connection, server
):
    payload = _draft_payload()
    payload["draft"]["places"] = [
        {"query": "British Columbia", "reason": "Province"},
        {"query": "Vancouver, BC", "reason": "Service area"},
        {"query": "V6B 1A1", "reason": "Downtown postal code"},
        {"query": "Atlantis"},
    ]
    _llm(payload)
    sid = _start(logged_in, workspace, connection).json()["id"]
    draft = _get(logged_in, workspace, f"sessions/{sid}").json()["draft"]
    by_query = {p["query"]: p for p in draft["places"]}
    assert by_query["British Columbia"]["ids"] == ["2000009"]
    # A city is every postal area of that city in the allowed country, never the US namesake.
    assert sorted(by_query["Vancouver, BC"]["ids"]) == ["9105600", "9105617"]
    assert by_query["Vancouver, BC"]["name"] == "Vancouver, BC (2 postal areas)"
    # A full postal code is matched by its first three characters.
    assert by_query["V6B 1A1"]["ids"] == ["9105617"]
    assert by_query["Atlantis"]["ids"] == []
    assert any("Atlantis" in w for w in draft["warnings"])


def test_a_draft_with_no_matched_place_warns_that_it_covers_the_whole_country(
    logged_in, workspace, connection, server
):
    payload = _draft_payload()
    payload["draft"]["places"] = [{"query": "Atlantis"}]
    _llm(payload)
    sid = _start(logged_in, workspace, connection).json()["id"]
    draft = _get(logged_in, workspace, f"sessions/{sid}").json()["draft"]
    assert any("run across all of CA" in w for w in draft["warnings"])
