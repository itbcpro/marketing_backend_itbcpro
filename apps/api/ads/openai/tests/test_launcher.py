# ruff: noqa: F811 - pytest fixtures are imported and re-declared as arguments
import json

import pytest

from actions import services as action_services
from ads.models import AssistSession, AssistStatus
from ads.openai import changes, launcher
from ads.openai.client import OpenAiAdsClient
from ads.openai.tests.test_assist import DRAFT, _h, _post, _start  # noqa: F401
from ads.openai.tests.test_openai_ads import (  # noqa: F401 - fixtures
    FakeAdsServer,
    connection,
    server,
)
from conftest import make_user
from core.errors import ValidationFailedError
from llm.service import set_llm_service
from workspaces.models import Role

pytestmark = pytest.mark.django_db


def _draft(groups=2, ads=2):
    draft = json.loads(json.dumps(DRAFT))
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
            for a in range(ads)
        ]
        draft["ad_groups"].append(group)
    draft["places"] = [{"query": "Vancouver, BC", "ids": ["9105600", "9105617"]}]
    draft["countries"] = ["CA"]
    return draft


@pytest.fixture
def session(workspace, connection):
    return AssistSession.objects.create(
        workspace=workspace,
        connection=connection,
        title="t",
        goal="g",
        status=AssistStatus.LAUNCHED,
        draft=_draft(),
        image_url="https://x.test/a.png",
        history=[{"type": "launch_started"}],
    )


@pytest.fixture(autouse=True)
def _run_actions_inline(monkeypatch):
    """Approved actions normally run in a worker; here they run as soon as they are approved."""

    def wait(action, timeout=0):
        action_services.execute(action.pk)
        action.refresh_from_db()
        return action

    monkeypatch.setattr(changes, "wait_for", wait)
    yield
    set_llm_service(None)


def _rows(server, prefix):
    return [r for r in server.rows.values() if r["id"].startswith(prefix)]


def test_the_whole_draft_is_created_paused_in_order_with_exact_places(
    workspace, connection, server, session
):
    approver = make_user("appr@example.com", workspace, Role.APPROVER)
    assert launcher.run(session.pk, approver.pk) == "done"
    campaigns, groups, ads = _rows(server, "cam"), _rows(server, "ad_"), _rows(server, "ads")
    assert len(campaigns) == 1 and len(groups) == 2 and len(ads) == 4
    campaign = campaigns[0]
    assert campaign["status"] == "paused"
    assert campaign["targeting"]["locations"] == {"include": [{"id": "9105600"}, {"id": "9105617"}]}
    assert {g["campaign_id"] for g in groups} == {campaign["id"]}
    assert all(a["creative"]["file_id"] == "file_1" for a in ads)
    session.refresh_from_db()
    assert session.history[-1]["type"] == "launch_done"
    assert (session.history[-1]["groups"], session.history[-1]["ads"]) == (2, 4)


def test_running_it_again_never_duplicates_anything(workspace, connection, server, session):
    approver = make_user("appr@example.com", workspace, Role.APPROVER)
    launcher.run(session.pk, approver.pk)
    launcher.run(session.pk, approver.pk)
    assert len(_rows(server, "cam")) == 1 and len(_rows(server, "ads")) == 4


def test_it_stops_and_says_so_when_a_step_needs_someone_to_approve(
    workspace, connection, server, session
):
    editor = make_user("edit@example.com", workspace, Role.EDITOR)
    assert launcher.run(session.pk, editor.pk) == "waiting"
    session.refresh_from_db()
    last = session.history[-1]
    assert last["type"] == "launch_stopped" and last["waiting"] is True
    assert _rows(server, "cam") == [] and "Approvals" in last["text"]


def test_a_rejected_step_is_reported_and_stops_the_launch(workspace, connection, server, session):
    approver = make_user("appr@example.com", workspace, Role.APPROVER)
    server.fail["/campaigns"] = 400
    assert launcher.run(session.pk, approver.pk) == "stopped"
    session.refresh_from_db()
    last = session.history[-1]
    assert last["type"] == "launch_stopped" and "did not go through" in last["text"]
    assert _rows(server, "ad_") == []


def test_without_an_image_the_campaign_and_groups_are_made_and_ads_are_skipped(
    workspace, connection, server, session
):
    session.image_url = ""
    session.save()
    approver = make_user("appr@example.com", workspace, Role.APPROVER)
    assert launcher.run(session.pk, approver.pk) == "done"
    assert len(_rows(server, "cam")) == 1 and len(_rows(server, "ad_")) == 2
    assert _rows(server, "ads") == []
    session.refresh_from_db()
    assert session.history[-1]["ads_skipped"] == 4


def test_a_web_page_is_refused_as_an_image(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        launcher.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))]
    )
    monkeypatch.setattr(launcher.httpx, "stream", lambda *a, **k: Response())
    with pytest.raises(ValidationFailedError, match="not a JPEG, PNG or WebP"):
        launcher.check_image_url("https://shiftvale.ca/")


def test_the_launch_endpoint_queues_the_background_job(
    logged_in, workspace, connection, server, session, monkeypatch
):
    queued = []
    monkeypatch.setattr("ads.tasks.launch_task.delay", lambda *a: queued.append(a))
    monkeypatch.setattr("ads.openai.launcher.transaction.on_commit", lambda fn: fn())
    monkeypatch.setattr(launcher, "check_image_url", lambda url: None)
    session.status = AssistStatus.DRAFT
    session.save()
    response = _post(
        logged_in, workspace, f"sessions/{session.pk}/launch", {"image_url": "https://x.test/b.png"}
    )
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "launched" and len(queued) == 1
    session.refresh_from_db()
    assert session.status == AssistStatus.LAUNCHED
    assert session.image_url == "https://x.test/b.png"


def test_without_an_image_an_image_already_in_the_account_is_reused(
    workspace, connection, server, session
):
    from ads.models import AdEntity

    group = AdEntity.objects.create(
        workspace=workspace, connection=connection, level="ad_group", external_id="g0", name="old"
    )
    AdEntity.objects.create(
        workspace=workspace,
        connection=connection,
        level="ad",
        external_id="a0",
        name="Old ad",
        parent=group,
        data={"creative": {"file_id": "file_existing"}},
    )
    session.image_url = ""
    session.save()
    approver = make_user("appr@example.com", workspace, Role.APPROVER)
    assert launcher.run(session.pk, approver.pk) == "done"
    ads = _rows(server, "ads")
    assert len(ads) == 4 and {a["creative"]["file_id"] for a in ads} == {"file_existing"}


def test_sync_shows_ads_this_app_created_even_when_the_platform_list_omits_them(
    workspace, connection, server, session, monkeypatch
):
    from ads.models import AdEntity
    from ads.openai import sync

    approver = make_user("appr@example.com", workspace, Role.APPROVER)
    assert launcher.run(session.pk, approver.pk) == "done"
    real_list = OpenAiAdsClient.list_resource

    def list_without_ads(self, resource, **kwargs):
        return [] if resource == "ads" else real_list(self, resource, **kwargs)

    monkeypatch.setattr(OpenAiAdsClient, "list_resource", list_without_ads)
    sync.sync_connection(connection)
    assert AdEntity.objects.filter(connection=connection, level="ad").count() == 4
