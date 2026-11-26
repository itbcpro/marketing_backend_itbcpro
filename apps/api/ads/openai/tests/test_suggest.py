# ruff: noqa: F811 - pytest fixtures are imported and re-declared as arguments
import json

import pytest

from ads.models import AdEntity
from ads.openai.tests.test_openai_ads import connection  # noqa: F401 - fixture
from ads.openai.tests.test_store import _h, _sync, platform  # noqa: F401 - fixtures
from llm.fake import FakeGateway, text_response
from llm.service import LlmService, set_llm_service

pytestmark = pytest.mark.django_db

BASE = "/api/v1/ads/openai/suggest"

HINTS = [f"managed IT question number {i} for nonprofits in Vancouver" for i in range(9)]
GROUP = {
    "name": "Cybersecurity for charities",
    "theme": "Charities worried about data safety",
    "context_hints": HINTS,
    "strategy": "maximize_clicks",
    "max_bid": None,
    "ads": [
        {
            "name": f"Cyber ad {i}",
            "title": f"Charity security tip {i}",
            "body": "Plain-language IT security checks for nonprofits.",
            "url": "https://itbcpro.ca/nonprofit",
            "angle": "Question-led",
        }
        for i in range(3)
    ],
    "rationale": "A new topic the campaign does not cover yet.",
    "notes": "",
}
AD = {
    "name": "Cybersecurity question",
    "title": "Is your charity's data safe?",
    "body": "Plain-language IT security checks for nonprofits in Vancouver.",
    "url": "https://itbcpro.ca/nonprofit",
    "angle": "Question-led",
    "rationale": "A different angle from the existing ads.",
    "notes": "",
}


@pytest.fixture(autouse=True)
def _reset():
    yield
    set_llm_service(None)


def _post(client, workspace, path, body):
    return client.post(
        f"{BASE}/{path}", data=json.dumps(body), content_type="application/json", **_h(workspace)
    )


def _llm(payload):
    gateway = FakeGateway([text_response(json.dumps(payload))])
    set_llm_service(LlmService(gateway))
    return gateway


def test_an_ad_group_is_suggested_from_the_campaign_and_its_other_groups(
    logged_in, workspace, connection, platform
):
    gateway = _llm(GROUP)
    _sync(logged_in, workspace, connection)
    campaign = AdEntity.objects.get(connection=connection, level="campaign")
    response = _post(
        logged_in,
        workspace,
        "ad-group",
        {"connection_id": str(connection.pk), "parent_id": campaign.external_id},
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["name"] == "Cybersecurity for charities" and len(body["context_hints"]) == 9
    assert len(body["ads"]) == 3
    sent = gateway.requests[-1][0].system[0].text
    assert campaign.name in sent and "ad_groups" in sent


def test_a_suggested_group_that_repeats_existing_hints_is_flagged(
    logged_in, workspace, connection, platform
):
    _sync(logged_in, workspace, connection)
    campaign = AdEntity.objects.get(connection=connection, level="campaign")
    group = AdEntity.objects.get(connection=connection, level="ad_group")
    group.data = {**group.data, "context_hints": HINTS}
    group.save()
    _llm({**GROUP, "name": group.name})
    body = _post(
        logged_in,
        workspace,
        "ad-group",
        {"connection_id": str(connection.pk), "parent_id": campaign.external_id},
    ).json()
    assert any("already exists" in w for w in body["warnings"])
    assert any("already appear in other ad groups" in w for w in body["warnings"])


def test_an_ad_is_suggested_for_a_group(logged_in, workspace, connection, platform):
    gateway = _llm(AD)
    _sync(logged_in, workspace, connection)
    group = AdEntity.objects.get(connection=connection, level="ad_group")
    response = _post(
        logged_in,
        workspace,
        "ad",
        {"connection_id": str(connection.pk), "parent_id": group.external_id, "instruction": "ask"},
    )
    assert response.status_code == 200, response.content
    assert response.json()["title"] == "Is your charity's data safe?"
    sent = gateway.requests[-1][0].system[0].text
    assert "is_the_group_the_new_ad_goes_in" in sent and "ask" in sent


def test_an_unknown_parent_is_not_found(logged_in, workspace, connection, platform):
    _llm(GROUP)
    response = _post(
        logged_in, workspace, "ad", {"connection_id": str(connection.pk), "parent_id": "nope"}
    )
    assert response.status_code == 404


def test_a_missing_landing_page_is_left_empty_and_flagged_never_guessed(
    logged_in, workspace, connection, platform
):
    _llm({**AD, "url": ""})
    _sync(logged_in, workspace, connection)
    group = AdEntity.objects.get(connection=connection, level="ad_group")
    body = _post(
        logged_in,
        workspace,
        "ad",
        {"connection_id": str(connection.pk), "parent_id": group.external_id},
    ).json()
    assert body["url"] == ""
    assert any("No landing page link" in w for w in body["warnings"])
