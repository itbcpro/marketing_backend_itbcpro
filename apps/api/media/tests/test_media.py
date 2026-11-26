import io
import json
import struct

import httpx
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from PIL import Image

from conftest import make_user
from media import services
from media.models import MediaAsset
from workspaces.models import Role, Workspace

pytestmark = pytest.mark.django_db

BASE = "/api/v1/media"
VIDEO_ID = "dQw4w9WgXcQ"


@pytest.fixture(autouse=True)
def _storage(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


def png(width: int, height: int, color=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def mp4(seconds: int = 12, width: int = 1920, height: int = 1080) -> bytes:
    mvhd = box(b"mvhd", struct.pack(">IIIII", 0, 0, 0, 1000, seconds * 1000) + bytes(80))
    tkhd_payload = (
        struct.pack(">IIIIII", 0, 0, 0, 1, 0, seconds * 1000)
        + bytes(8 + 2 + 2 + 2 + 2 + 36)
        + struct.pack(">II", width * 65536, height * 65536)
    )
    trak = box(b"trak", box(b"tkhd", tkhd_payload))
    ftyp = box(b"ftyp", b"isom" + bytes(4) + b"isomiso2")
    return ftyp + box(b"moov", mvhd + trak) + box(b"mdat", bytes(64))


def _h(workspace):
    return {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}


def _upload(client, workspace, data: bytes, name: str, **extra):
    return client.post(
        f"{BASE}/upload",
        data={"file": SimpleUploadedFile(name, data), **extra},
        **_h(workspace),
    )


def _post(client, workspace, path, body):
    return client.post(
        f"{BASE}/{path}", data=json.dumps(body), content_type="application/json", **_h(workspace)
    )


def test_an_image_is_stored_inspected_and_checked_against_every_destination(logged_in, workspace):
    response = _upload(
        logged_in, workspace, png(1200, 628), "hero.png", title="Hero", tags="Spring, sale"
    )
    assert response.status_code == 200, response.content
    body = response.json()
    asset = body["asset"]
    assert body["created"] is True and asset["kind"] == "image"
    assert (asset["width"], asset["height"], asset["file_format"]) == (1200, 628, "PNG")
    assert asset["name"] == "Hero" and asset["tags"] == ["spring", "sale"]
    checks = {c["spec"]: c for c in asset["checks"]}
    assert checks["google_landscape"]["ok"] is True
    assert checks["google_square"]["ok"] is False
    assert checks["google_square"]["reasons"][0]["code"] == "wrong_ratio"
    assert checks["openai_chat_card"]["ok"] is False  # 628 px high is under 640
    assert any(r["code"] == "too_small" for r in checks["openai_chat_card"]["reasons"])


def test_the_same_file_is_not_stored_twice(logged_in, workspace):
    first = _upload(logged_in, workspace, png(800, 800), "a.png").json()
    second = _upload(logged_in, workspace, png(800, 800), "b.png").json()
    assert second["created"] is False and second["asset"]["id"] == first["asset"]["id"]
    assert MediaAsset.objects.count() == 1


PNG_MAGIC = bytes([0x89]) + b"PNG"


def test_uploading_the_same_file_twice_keeps_the_file(logged_in, workspace):
    first = _upload(logged_in, workspace, png(640, 640), "p.png").json()["asset"]
    again = _upload(logged_in, workspace, png(640, 640), "p.png").json()
    assert again["asset"]["id"] == first["id"]
    served = logged_in.get(f"{BASE}/{first['id']}/file", **_h(workspace))
    assert served.status_code == 200 and b"".join(served.streaming_content)[:4] == PNG_MAGIC


def test_files_that_are_not_real_images_or_videos_are_refused(logged_in, workspace):
    assert _upload(logged_in, workspace, b"just some text", "fake.png").status_code in {400, 422}
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    assert _upload(logged_in, workspace, svg, "x.svg").status_code in {400, 422}
    assert MediaAsset.objects.count() == 0


def test_a_video_header_gives_duration_and_size_without_external_tools(logged_in, workspace):
    body = _upload(logged_in, workspace, mp4(12, 1920, 1080), "promo.mp4").json()["asset"]
    assert body["kind"] == "video" and body["file_format"] == "MP4"
    assert body["duration_seconds"] == 12.0 and (body["width"], body["height"]) == (1920, 1080)
    checks = {c["spec"]: c for c in body["checks"]}
    assert checks["google_video"]["reasons"][0]["code"] == "needs_youtube"  # a file, not YouTube


def test_a_youtube_video_is_linked_by_url_and_satisfies_the_google_video_check(
    logged_in, workspace, monkeypatch
):
    def fake_fetch(url, **kwargs):
        assert "oembed" in url and kwargs["allowed_hosts"] == frozenset({"www.youtube.com"})
        return httpx.Response(
            200,
            json={
                "title": "Our story",
                "author_name": "ITBCPro",
                "thumbnail_url": f"https://i.ytimg.com/vi/{VIDEO_ID}/hqdefault.jpg",
            },
        )

    monkeypatch.setattr(services, "safe_fetch", fake_fetch)
    response = _post(logged_in, workspace, "youtube", {"video": f"https://youtu.be/{VIDEO_ID}?t=5"})
    assert response.status_code == 200, response.content
    asset = response.json()["asset"]
    assert asset["youtube_video_id"] == VIDEO_ID and asset["name"] == "Our story"
    assert {c["spec"]: c["ok"] for c in asset["checks"]}["google_video"] is True
    again = _post(logged_in, workspace, "youtube", {"video": VIDEO_ID}).json()
    assert again["created"] is False


@pytest.mark.parametrize(
    "value",
    [
        f"https://youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"youtu.be/{VIDEO_ID}",
        VIDEO_ID,
    ],
)
def test_youtube_links_in_common_shapes_are_understood(value):
    assert services.parse_youtube_id(value) == VIDEO_ID


@pytest.mark.parametrize(
    "value", ["https://vimeo.com/123", "not a link", f"https://evil.test/watch?v={VIDEO_ID}"]
)
def test_other_links_are_not_accepted_as_youtube(value):
    from core.errors import ValidationFailedError

    with pytest.raises(ValidationFailedError):
        services.parse_youtube_id(value)


def test_one_photo_yields_the_crops_google_needs_and_each_passes_its_own_check(
    logged_in, workspace
):
    source = _upload(logged_in, workspace, png(3000, 2000), "photo.png").json()["asset"]
    targets = ["google_landscape", "google_square", "google_portrait", "google_logo_landscape"]
    response = _post(logged_in, workspace, f"{source['id']}/derive", {"targets": targets})
    assert response.status_code == 200, response.content
    body = response.json()
    assert len(body["created"]) == 4 and body["skipped"] == []
    by_variant = {a["variant"]: a for a in body["created"]}
    landscape, square = by_variant["google_landscape"], by_variant["google_square"]
    assert (landscape["width"], landscape["height"]) == (1200, 628)
    assert (square["width"], square["height"]) == (1200, 1200)
    for variant, asset in by_variant.items():
        ok = {c["spec"]: c["ok"] for c in asset["checks"]}
        assert ok[variant] is True, (variant, asset["checks"])
        assert asset["derived_from_id"] == source["id"]
    originals = logged_in.get(f"{BASE}/", {"originals": "true"}, **_h(workspace)).json()["items"]
    assert [a["id"] for a in originals] == [source["id"]]


def test_a_source_that_is_too_small_is_reported_not_silently_upscaled(logged_in, workspace):
    source = _upload(logged_in, workspace, png(400, 300), "small.png").json()["asset"]
    body = _post(
        logged_in,
        workspace,
        f"{source['id']}/derive",
        {"targets": ["google_landscape", "google_square"]},
    ).json()
    assert [a["variant"] for a in body["created"]] == ["google_square"]
    assert body["skipped"][0]["target"] == "google_landscape"


def test_the_file_is_served_only_to_its_own_workspace(logged_in, workspace):
    asset = _upload(logged_in, workspace, png(640, 640), "p.png").json()["asset"]
    ok = logged_in.get(f"{BASE}/{asset['id']}/file", **_h(workspace))
    assert ok.status_code == 200 and ok["Content-Type"] == "image/png"
    assert ok["X-Content-Type-Options"] == "nosniff"
    assert b"".join(ok.streaming_content)[:4] == b"\x89PNG"
    other = Workspace.objects.create(name="Other", slug="other-media")
    stranger = Client()
    stranger.force_login(make_user("m@example.com", other, Role.OWNER))
    assert stranger.get(f"{BASE}/{asset['id']}/file", **_h(other)).status_code == 404


def test_plain_image_tags_can_name_the_workspace_in_the_address(logged_in, workspace):
    asset = _upload(logged_in, workspace, png(640, 640), "p.png").json()["asset"]
    url = f"{BASE}/{asset['id']}/file"
    assert logged_in.get(url).status_code == 422  # no header and no parameter
    assert logged_in.get(url, {"workspace": str(workspace.pk)}).status_code == 200
    other = Workspace.objects.create(name="Other", slug="other-media-2")
    assert logged_in.get(url, {"workspace": str(other.pk)}).status_code == 422  # not a member
    stranger = Client()
    stranger.force_login(make_user("m2@example.com", other, Role.OWNER))
    assert stranger.get(url, {"workspace": str(other.pk)}).status_code == 404  # not its asset
    assert stranger.get(url, {"workspace": str(workspace.pk)}).status_code == 422


def test_a_viewer_can_look_but_not_upload_or_delete(logged_in, workspace):
    asset = _upload(logged_in, workspace, png(640, 640), "p.png").json()["asset"]
    viewer = Client()
    viewer.force_login(make_user("v@example.com", workspace, Role.VIEWER))
    assert viewer.get(f"{BASE}/{asset['id']}", **_h(workspace)).status_code == 200
    assert _upload(viewer, workspace, png(700, 700), "q.png").status_code == 403
    assert viewer.delete(f"{BASE}/{asset['id']}", **_h(workspace)).status_code == 403


def test_metadata_can_be_edited_and_an_asset_with_crops_cannot_be_deleted(logged_in, workspace):
    source = _upload(logged_in, workspace, png(1500, 1500), "s.png").json()["asset"]
    patched = logged_in.patch(
        f"{BASE}/{source['id']}",
        data=json.dumps(
            {
                "name": "Office photo",
                "alt_text": "Our office",
                "rights": "owned",
                "ai_generated": False,
                "tags": ["Office", "office"],
            }
        ),
        content_type="application/json",
        **_h(workspace),
    ).json()
    assert patched["name"] == "Office photo" and patched["rights"] == "owned"
    assert patched["tags"] == ["office"]
    crop = _post(logged_in, workspace, f"{source['id']}/derive", {"targets": ["google_square"]})
    crop_id = crop.json()["created"][0]["id"]
    assert logged_in.delete(f"{BASE}/{source['id']}", **_h(workspace)).status_code == 409
    assert logged_in.delete(f"{BASE}/{crop_id}", **_h(workspace)).status_code == 204
    assert logged_in.delete(f"{BASE}/{source['id']}", **_h(workspace)).status_code == 204


def test_an_upload_larger_than_the_limit_is_rejected(logged_in, workspace):
    with override_settings(API_MAX_UPLOAD_BYTES=1000, MEDIA_MAX_IMAGE_BYTES=1000):
        response = _upload(logged_in, workspace, png(300, 300, (1, 2, 3)) + bytes(2000), "big.png")
    assert response.status_code in {400, 413, 422}


def test_importing_a_url_uses_the_safe_fetcher(logged_in, workspace, monkeypatch):
    monkeypatch.setattr(
        services,
        "safe_fetch",
        lambda url, **kw: httpx.Response(200, content=png(700, 700, (9, 9, 9))),
    )
    response = _post(
        logged_in, workspace, "import-url", {"url": "https://cdn.example.com/a/logo.png"}
    )
    assert response.status_code == 200, response.content
    asset = response.json()["asset"]
    assert asset["source"] == "url" and asset["name"] == "logo"
    monkeypatch.undo()
    blocked = _post(logged_in, workspace, "import-url", {"url": "http://127.0.0.1/secret.png"})
    assert blocked.status_code in {400, 422}
