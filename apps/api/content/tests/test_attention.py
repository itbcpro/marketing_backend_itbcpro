import pytest

from content import sync
from content.models import ContentItem, IgnoredCheck

JSON = "application/json"


@pytest.fixture
def api(client, owner, workspace, synced, post):
    client.force_login(owner)
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    base = f"/api/v1/websites/{synced.pk}"

    class Api:
        def get(self, threshold=101):
            return client.get(f"{base}/attention?threshold={threshold}", **headers).json()

        def ignore(self, check=""):
            return client.post(
                f"{base}/ignores",
                data={"item_id": str(post.pk), "check_key": check},
                content_type=JSON,
                **headers,
            )

        def restore(self, ignore_id):
            return client.delete(f"{base}/ignores/{ignore_id}", **headers)

    return Api()


def test_lowest_scores_come_first_with_their_problems(api):
    data = api.get()
    scores = [r["score"] for r in data["items"]]
    assert scores == sorted(scores)
    assert all(r["problems"] for r in data["items"])


def test_ignoring_a_check_raises_the_score_and_lists_it(api, post):
    before = next(r for r in api.get()["items"] if r["id"] == str(post.pk))
    check = before["problems"][0]["key"]
    row = api.ignore(check).json()
    after = next(r for r in api.get()["items"] if r["id"] == str(post.pk))
    assert after["score"] >= before["score"]
    assert check not in {p["key"] for p in after["problems"]}
    assert [i["check_key"] for i in api.get()["ignored"]] == [check]
    api.restore(row["id"])
    restored = next(r for r in api.get()["items"] if r["id"] == str(post.pk))
    assert restored["score"] == before["score"]


def test_ignoring_a_page_hides_it_and_survives_a_full_resync(api, post, synced):
    api.ignore()
    assert str(post.pk) not in {r["id"] for r in api.get()["items"]}
    sync.execute_run(sync.start_run(synced, full=True))
    assert str(post.pk) not in {r["id"] for r in api.get()["items"]}
    assert IgnoredCheck.objects.filter(website=synced, item_key=post.key, check_key="").exists()
    assert ContentItem.objects.filter(pk=post.pk).exists()


def test_ignoring_twice_keeps_one_row(api):
    first, second = api.ignore("seo_title").json(), api.ignore("seo_title").json()
    assert first["id"] == second["id"]
