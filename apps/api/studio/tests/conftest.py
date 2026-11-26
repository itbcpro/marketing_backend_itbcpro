import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from llm.fake import FakeGateway, text_response
from llm.service import LlmService, set_llm_service
from social import images
from social.tests.conftest import account, connection, fake  # noqa: F401 - shared fixtures
from studio import brands, config_store, net
from studio.config import ChannelPlan, Pillar, StudioConfig

PIECE = {
    "title": "Test your backup",
    "body": (
        "Most small offices have a backup. Far fewer have ever restored from it.\n\n"
        "Pick one file today and bring it back. If it takes more than five minutes, "
        "you just found the real problem.\n\nSave this for Friday."
    ),
    "hook": "Most small offices have a backup.",
    "hashtags": ["#itsupport", "backup"],
    "cta": "Save this for Friday.",
    "first_comment": "",
    "alternate_hooks": ["When did you last restore?", "A backup nobody tested"],
    "slides": [],
    "video": None,
    "visual": {
        "scene": "A tidy desk with a laptop and an external drive next to a coffee cup",
        "shot": "Eye level, soft window light",
        "on_image_text": "",
        "alt_text": "A laptop and an external drive on a desk",
        "library_keywords": ["backup", "desk"],
    },
    "rationale": "Opens with a gap the audience recognises.",
}

CAROUSEL = {
    **PIECE,
    "slides": [
        {"headline": "Test your backup", "body": "Restore one file today.", "alt_text": "Slide 1"},
        {"headline": "Check the date", "body": "Old backups are a risk.", "alt_text": "Slide 2"},
        {"headline": "Save this", "body": "Send it to your IT person.", "alt_text": "Slide 3"},
    ],
}

VIDEO = {
    **PIECE,
    "video": {
        "hook": "Your backup is probably broken",
        "beats": ["Show a failed restore", "Explain why", "Show the fix"],
        "on_screen_text": ["Probably broken", "Test it"],
        "shot_list": ["Screen recording"],
        "duration_seconds": 20,
        "audio_note": "Original voice",
        "cta": "Save this",
    },
}

IDEA = {
    "title": "The five minute restore test",
    "angle": "Why a backup you never restored is not a backup, and the test that proves it.",
    "why_now": "Ransomware stories are in the news this week.",
    "pillar": "education",
    "urgency": "timely",
    "shelf_life_days": 10,
    "trends": [1],
    "formats": [
        {"channel": "instagram", "kind": "carousel"},
        {"channel": "linkedin", "kind": "text"},
    ],
    "needs_from_owner": "",
    "score": 8,
}


def llm(*payloads: Any) -> FakeGateway:
    gateway = FakeGateway([text_response(json.dumps(p)) for p in payloads])
    set_llm_service(LlmService(gateway))
    return gateway


def ideas(count: int, **overrides: Any) -> dict[str, Any]:
    return {
        "ideas": [{**IDEA, "title": f"Idea {n}", **overrides} for n in range(count)],
        "notes": "",
    }


@pytest.fixture(autouse=True)
def _isolation(monkeypatch) -> Iterator[None]:
    # Tests run the background work themselves, so queuing is a no-op here.
    monkeypatch.setattr("studio.writer.enqueue", lambda piece: None)
    monkeypatch.setattr("social.drafts._publish_soon", lambda: None)
    monkeypatch.setattr(net, "resolver", lambda host, port: ["93.184.216.34"])
    yield
    set_llm_service(None)
    images.set_image_generator(None)
    monkeypatch.setattr(net, "transport", None)


def feed_transport(routes: dict[str, bytes | int]) -> httpx.MockTransport:
    """A web that answers by URL prefix: bytes are a 200 body, an int is an error status."""

    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, answer in routes.items():
            if str(request.url).startswith(prefix):
                if isinstance(answer, int):
                    return httpx.Response(answer)
                return httpx.Response(200, content=answer)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def brand_of(workspace) -> Any:
    """The workspace's (first) company; the default one is created on first use."""
    return brands.listing(workspace.pk)[0]


def make_config(**overrides: Any) -> StudioConfig:
    base: dict[str, Any] = {
        "goals": "Book more IT support consultations",
        "audience": "Owners of small offices",
        "pillars": [Pillar(name="education", weight=2), Pillar(name="proof", weight=1)],
        "channels": {
            "instagram": ChannelPlan(
                enabled=True, posts_per_week=3, days=[0, 2, 4], times=["10:00"]
            ),
            "linkedin": ChannelPlan(enabled=True, posts_per_week=2, days=[1, 3], times=["09:00"]),
        },
    }
    visuals = {"logo_on_pictures": False, **overrides.pop("visuals", {})}
    return StudioConfig.model_validate({**base, "visuals": visuals, **overrides})


@pytest.fixture
def configured(workspace, owner) -> StudioConfig:
    config = make_config()
    config_store.save(brand_of(workspace), config, owner)
    return config
