from django.test import Client

from content.tests.fake_wordpress import HOST, PASSWORD, USER


def _login(client: Client, owner, workspace):
    client.force_login(owner)
    return {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}


def test_connect_wordpress_then_add_website(client, owner, workspace, wp):
    headers = _login(client, owner, workspace)
    created = client.post(
        "/api/v1/connections/",
        data={
            "provider": "wordpress",
            "fields": {
                "site_url": f"https://{HOST}",
                "username": USER,
                "application_password": PASSWORD,
            },
        },
        content_type="application/json",
        **headers,
    )
    assert created.status_code == 200, created.content
    body = created.json()
    assert body["status"] == "active"
    assert body["external_account_id"] == f"https://{HOST}"
    assert "application_password" not in created.content.decode()

    site = client.post(
        "/api/v1/websites/",
        data={"kind": "wordpress", "connection_id": body["id"]},
        content_type="application/json",
        **headers,
    )
    assert site.status_code == 200, site.content
    assert site.json()["profile"]["counts"]["posts"] == 3

    listing = client.get("/api/v1/websites/", **headers)
    assert [s["base_url"] for s in listing.json()] == [f"https://{HOST}"]

    probe = client.post(f"/api/v1/websites/{site.json()['id']}/probe", **headers)
    assert probe.status_code == 200


def test_connect_wordpress_with_wrong_password_is_rejected(client, owner, workspace, wp):
    headers = _login(client, owner, workspace)
    response = client.post(
        "/api/v1/connections/",
        data={
            "provider": "wordpress",
            "fields": {
                "site_url": f"https://{HOST}",
                "username": USER,
                "application_password": "wrong",
            },
        },
        content_type="application/json",
        **headers,
    )
    assert response.status_code == 422


def test_add_git_site_and_remove(client, owner, workspace, wp):
    headers = _login(client, owner, workspace)
    site = client.post(
        "/api/v1/websites/",
        data={"kind": "git_site", "name": "AI site", "base_url": f"https://{HOST}", "repo": "a/b"},
        content_type="application/json",
        **headers,
    )
    assert site.status_code == 200, site.content
    assert site.json()["kind"] == "git_site"
    gone = client.delete(f"/api/v1/websites/{site.json()['id']}", **headers)
    assert gone.status_code == 204
    assert client.get("/api/v1/websites/", **headers).json() == []


def test_websites_require_login(client):
    assert client.get("/api/v1/websites/").status_code == 401


def test_inventory_api_sync_list_filter_and_detail(client, owner, workspace, wp, wp_connection):
    headers = _login(client, owner, workspace)
    site = client.post(
        "/api/v1/websites/",
        data={"kind": "wordpress", "connection_id": str(wp_connection.pk)},
        content_type="application/json",
        **headers,
    ).json()
    base = f"/api/v1/websites/{site['id']}"

    run = client.post(
        base + "/sync", data={"full": True}, content_type="application/json", **headers
    )
    assert run.status_code == 200, run.content
    assert run.json()["status"] == "done"
    assert client.get(base + "/sync-runs", **headers).json()[0]["stats"]["created"] == 5

    summary = client.get(base + "/inventory/summary", **headers).json()
    assert summary["wordpress_check"]["post"]["match"] is True

    listing = client.get(base + "/inventory?kind=post&sort=title&page_size=2", **headers).json()
    assert listing["total"] == 3
    assert len(listing["items"]) == 2
    assert [i["title"] for i in listing["items"]] == ["Draft Post", "Hello World"]

    thin = client.get(base + "/inventory?issue=thin_content", **headers).json()
    assert {i["title"] for i in thin["items"]} == {"Short Post", "Draft Post"}
    assert all("thin_content" in i["issues"] for i in thin["items"])

    found = client.get(base + "/inventory?q=hello", **headers).json()
    assert [i["title"] for i in found["items"]] == ["Hello World"]

    detail = client.get(base + f"/inventory/{found['items'][0]['id']}", **headers).json()
    assert detail["outline"] == [{"level": 2, "text": "Sub heading"}]
    assert detail["terms"]["categories"] == ["News"]
    assert detail["text_preview"].startswith("word")
    assert client.get(base + f"/inventory/{site['id']}", **headers).status_code == 404
