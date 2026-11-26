# ruff: noqa: F811 - pytest fixtures are imported and re-declared as arguments
import json

import pytest
from django.test import Client

from actions.models import ProposedAction
from ads.models import AdChange, AdEntity
from ads.openai.tests.test_openai_ads import connection  # noqa: F401 - fixture
from ads.openai.tests.test_store import _h, _sync, platform  # noqa: F401 - fixtures
from conftest import make_user
from llm.fake import FakeGateway, text_response
from llm.service import LlmService, set_llm_service
from workspaces.models import Role

pytestmark = pytest.mark.django_db

BASE = "/api/v1/ads/openai/store"

VARIANTS = {
    "variants": [
        {
            "title": "Managed IT in Metro Vancouver",
            "body": "Local technicians for nonprofits and small teams. Clear monthly pricing.",
            "angle": "Place first",
            "rationale": "Names the service area the campaign targets.",
        },
        {
            "title": "IT help that answers the phone",
            "body": "Fast, friendly support for charities in Burnaby and New West.",
            "angle": "Responsiveness",
            "rationale": "Speaks to the pain of waiting for help.",
        },
        {
            "title": "Nonprofit IT, without the stress",
            "body": "Secure networks and helpdesk support that fit a charity budget.",
            "angle": "Outcome",
            "rationale": "Leads with the result the owner wants.",
        },
    ]
}


@pytest.fixture(autouse=True)
def _immediate(monkeypatch):
    monkeypatch.setattr("ads.openai.variants.transaction.on_commit", lambda fn: fn())
    yield
    set_llm_service(None)


def _ad(connection):
    return AdEntity.objects.get(connection=connection, level="ad")


def _start(client, workspace, connection, instruction=""):
    return client.post(
        f"{BASE}/entities/{_ad(connection).pk}/variants",
        data=json.dumps({"instruction": instruction}),
        content_type="application/json",
        **_h(workspace),
    )


def _choose(client, workspace, set_id, index):
    return client.post(
        f"{BASE}/variants/{set_id}/choose",
        data=json.dumps({"index": index}),
        content_type="application/json",
        **_h(workspace),
    )


def test_three_versions_are_written_saved_and_listed(logged_in, workspace, connection, platform):
    gateway = FakeGateway([text_response(json.dumps(VARIANTS))])
    set_llm_service(LlmService(gateway))
    _sync(logged_in, workspace, connection)
    started = _start(logged_in, workspace, connection, "Mention Metro Vancouver")
    assert started.status_code == 200, started.content
    sets = logged_in.get(f"{BASE}/entities/{_ad(connection).pk}/variants", **_h(workspace)).json()
    assert sets[0]["status"] == "done" and len(sets[0]["variants"]) == 3
    assert "Mention Metro Vancouver" in gateway.requests[-1][0].system[0].text


def test_the_model_is_asked_again_when_a_version_breaks_the_length_limit(
    logged_in, workspace, connection, platform
):
    too_long = json.loads(json.dumps(VARIANTS))
    too_long["variants"][0]["title"] = "x" * 80
    set_llm_service(
        LlmService(
            FakeGateway([text_response(json.dumps(too_long)), text_response(json.dumps(VARIANTS))])
        )
    )
    _sync(logged_in, workspace, connection)
    _start(logged_in, workspace, connection)
    stored = logged_in.get(f"{BASE}/entities/{_ad(connection).pk}/variants", **_h(workspace)).json()
    assert stored[0]["status"] == "done"
    assert all(len(v["title"]) <= 50 for v in stored[0]["variants"])


def test_choosing_a_version_edits_the_ad_with_the_chosen_text_and_keeps_link_and_image(
    logged_in, workspace, connection, platform, django_capture_on_commit_callbacks
):
    set_llm_service(LlmService(FakeGateway([text_response(json.dumps(VARIANTS))])))
    _sync(logged_in, workspace, connection)
    set_id = _start(logged_in, workspace, connection).json()["id"]
    with django_capture_on_commit_callbacks(execute=True):
        chosen = _choose(logged_in, workspace, set_id, 1)
    assert chosen.status_code == 200, chosen.content
    body = chosen.json()
    assert body["chosen_index"] == 1 and body["applied"] is True
    action = ProposedAction.objects.get(pk=body["action_id"])
    creative = action.payload["creative"]
    assert action.kind == "ads.openai.ad.update"
    assert creative["title"] == "IT help that answers the phone"
    assert creative["target_url"] == "https://itbcpro.ca/x" and creative["file_id"] == "file_1"
    assert _choose(logged_in, workspace, set_id, 2).status_code == 409
    # the platform accepted the edit and the local copy already shows it
    assert platform.ad_title == "IT help that answers the phone"
    assert _ad(connection).data["creative"]["title"] == "IT help that answers the phone"
    assert AdChange.objects.filter(entity=_ad(connection)).exists()


def test_an_editor_can_choose_but_the_change_waits_for_approval(
    logged_in, workspace, connection, platform
):
    set_llm_service(LlmService(FakeGateway([text_response(json.dumps(VARIANTS))])))
    _sync(logged_in, workspace, connection)
    set_id = _start(logged_in, workspace, connection).json()["id"]
    editor = Client()
    editor.force_login(make_user("copy@example.com", workspace, Role.EDITOR))
    body = _choose(editor, workspace, set_id, 0).json()
    assert body["applied"] is False
    assert ProposedAction.objects.get(pk=body["action_id"]).status == "awaiting_approval"


def test_bad_choices_and_non_ads_are_rejected(logged_in, workspace, connection, platform):
    set_llm_service(LlmService(FakeGateway([text_response(json.dumps(VARIANTS))])))
    _sync(logged_in, workspace, connection)
    set_id = _start(logged_in, workspace, connection).json()["id"]
    assert _choose(logged_in, workspace, set_id, 7).status_code in {400, 422}
    group = AdEntity.objects.get(connection=connection, level="ad_group")
    response = logged_in.post(
        f"{BASE}/entities/{group.pk}/variants",
        data=json.dumps({}),
        content_type="application/json",
        **_h(workspace),
    )
    assert response.status_code in {400, 422}
