# ruff: noqa: F811 - pytest fixtures are imported and re-declared as arguments
import json

import pytest
from django.test import Client

from actions.models import ProposedAction
from ads.models import AdEntity
from ads.openai.tests.test_openai_ads import connection  # noqa: F401 - fixture
from ads.openai.tests.test_store import _h, _sync, platform  # noqa: F401 - fixtures
from conftest import make_user
from llm.fake import FakeGateway, text_response
from llm.service import LlmService, set_llm_service
from workspaces.models import Role

pytestmark = pytest.mark.django_db

BASE = "/api/v1/ads/openai/store"

CLEAN = {
    "hints": [
        "managed IT support for nonprofits in Metro Vancouver",
        "outsourced IT helpdesk for charities",
        "affordable IT for small nonprofit offices",
        "network security for nonprofit organizations",
        "Microsoft 365 setup and support for charities",
        "on-site IT technicians in Burnaby and New Westminster",
        "how much does managed IT cost for a nonprofit",
        "replacing an unreliable IT provider",
    ],
    "summary": "Three hints were sentence fragments; they were rejoined into full phrases.",
    "notes": "The campaign also targets Ontario while the hints focus on Metro Vancouver.",
}


@pytest.fixture(autouse=True)
def _immediate(monkeypatch):
    monkeypatch.setattr("ads.openai.hints.transaction.on_commit", lambda fn: fn())
    yield
    set_llm_service(None)


def _group(connection):
    return AdEntity.objects.get(connection=connection, level="ad_group")


def _start(client, workspace, connection, instruction=""):
    return client.post(
        f"{BASE}/entities/{_group(connection).pk}/hints",
        data=json.dumps({"instruction": instruction}),
        content_type="application/json",
        **_h(workspace),
    )


def _apply(client, workspace, proposal_id):
    return client.post(f"{BASE}/hints/{proposal_id}/apply", **_h(workspace))


def test_hints_are_rewritten_and_kept_next_to_the_originals(
    logged_in, workspace, connection, platform
):
    gateway = FakeGateway([text_response(json.dumps(CLEAN))])
    set_llm_service(LlmService(gateway))
    _sync(logged_in, workspace, connection)
    response = _start(logged_in, workspace, connection, "Focus on Metro Vancouver")
    assert response.status_code == 200, response.content
    stored = logged_in.get(
        f"{BASE}/entities/{_group(connection).pk}/hints", **_h(workspace)
    ).json()[0]
    assert stored["status"] == "done" and len(stored["after"]) == 8
    assert "or comparing managed providers" in stored["before"]
    assert stored["notes"].startswith("The campaign also targets Ontario")
    prompt = gateway.requests[-1][0].system[0].text
    assert "Focus on Metro Vancouver" in prompt and "or comparing managed providers" in prompt


def test_a_list_with_fragments_or_duplicates_is_sent_back_for_repair(
    logged_in, workspace, connection, platform
):
    broken = json.loads(json.dumps(CLEAN))
    broken["hints"][0] = "or comparing providers"
    duplicate = json.loads(json.dumps(CLEAN))
    duplicate["hints"][1] = duplicate["hints"][0]
    set_llm_service(
        LlmService(
            FakeGateway(
                [
                    text_response(json.dumps(broken)),
                    text_response(json.dumps(duplicate)),
                    text_response(json.dumps(CLEAN)),
                ]
            )
        )
    )
    _sync(logged_in, workspace, connection)
    _start(logged_in, workspace, connection)
    stored = logged_in.get(
        f"{BASE}/entities/{_group(connection).pk}/hints", **_h(workspace)
    ).json()[0]
    assert stored["status"] == "done" and stored["after"][0].startswith("managed IT")


def test_applying_edits_the_group_and_updates_the_local_copy(
    logged_in, workspace, connection, platform, django_capture_on_commit_callbacks
):
    set_llm_service(LlmService(FakeGateway([text_response(json.dumps(CLEAN))])))
    _sync(logged_in, workspace, connection)
    proposal_id = _start(logged_in, workspace, connection).json()["id"]
    with django_capture_on_commit_callbacks(execute=True):
        result = _apply(logged_in, workspace, proposal_id)
    assert result.status_code == 200, result.content
    body = result.json()
    assert body["decided"] is True and body["applied"] is True
    action = ProposedAction.objects.get(pk=body["action_id"])
    assert action.kind == "ads.openai.ad_group.update"
    assert action.payload["ad_group_id"] == "grp_1" and len(action.payload["context_hints"]) == 8
    assert _apply(logged_in, workspace, proposal_id).status_code == 409


def test_an_editor_can_request_but_the_change_waits_for_approval(
    logged_in, workspace, connection, platform
):
    set_llm_service(LlmService(FakeGateway([text_response(json.dumps(CLEAN))])))
    _sync(logged_in, workspace, connection)
    proposal_id = _start(logged_in, workspace, connection).json()["id"]
    editor = Client()
    editor.force_login(make_user("hints@example.com", workspace, Role.EDITOR))
    body = _apply(editor, workspace, proposal_id).json()
    assert body["applied"] is False
    assert ProposedAction.objects.get(pk=body["action_id"]).status == "awaiting_approval"


def test_only_ad_groups_have_hints(logged_in, workspace, connection, platform):
    _sync(logged_in, workspace, connection)
    ad = AdEntity.objects.get(connection=connection, level="ad")
    response = logged_in.post(
        f"{BASE}/entities/{ad.pk}/hints",
        data=json.dumps({}),
        content_type="application/json",
        **_h(workspace),
    )
    assert response.status_code in {400, 422}
