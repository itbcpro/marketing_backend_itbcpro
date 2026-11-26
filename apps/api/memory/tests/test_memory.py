import json
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from llm.fake import FakeGateway, text_response
from llm.service import LlmService
from memory import reflection, services
from memory.embeddings import HashEmbedder
from memory.models import FeedbackEvent, MemoryItem, MemoryKind, MemoryScope, MemoryStatus

pytestmark = pytest.mark.django_db
ACTIVE = MemoryStatus.ACTIVE


def _mem(workspace, text, **kw) -> MemoryItem:
    kw.setdefault("status", ACTIVE)
    return services.remember(workspace_id=workspace.pk, text=text, **kw)


def _texts(hits) -> list[str]:
    return [h.item.text for h in hits]


# ---- brand profile ----------------------------------------------------------------------------


def test_brand_profile_is_versioned_and_rendered(workspace, owner) -> None:
    assert services.render_brand_profile(services.current_brand_profile(workspace.pk)).startswith(
        "(no brand"
    )
    services.save_brand_profile(workspace_id=workspace.pk, content={"voice": "warm"}, user=owner)
    second = services.save_brand_profile(
        workspace_id=workspace.pk,
        content={"voice": "direct", "banned_claims": ["guaranteed", "#1"]},
        user=owner,
    )
    assert second.version == 2 and services.current_brand_profile(workspace.pk).pk == second.pk
    rendered = services.render_brand_profile(second)
    assert "Voice: direct" in rendered and "Banned claims: guaranteed; #1" in rendered


# ---- retrieval --------------------------------------------------------------------------------


def test_keyword_and_semantic_search_find_relevant_memories(workspace) -> None:
    _mem(workspace, "Headlines under forty characters get approved most often")
    _mem(workspace, "Never mention competitor pricing in ad copy")
    _mem(workspace, "Our customers are small accounting firms in Ontario")
    hits = services.retrieve(workspace_id=workspace.pk, query="headline length for ads")
    assert _texts(hits)[0].startswith("Headlines under forty")
    assert services.retrieve(workspace_id=workspace.pk, query="accounting firms")[
        0
    ].item.text.startswith("Our customers")
    assert services.retrieve(workspace_id=workspace.pk, query="quantum chromodynamics") == []


def test_only_active_current_items_are_returned(workspace) -> None:
    _mem(workspace, "Active rule about budgets", kind=MemoryKind.RULE)
    _mem(workspace, "Proposed rule about budgets", status=MemoryStatus.PROPOSED)
    _mem(workspace, "Retired rule about budgets", status=MemoryStatus.RETIRED)
    expired = _mem(workspace, "Expired rule about budgets")
    MemoryItem.objects.filter(pk=expired.pk).update(valid_to=timezone.now() - timedelta(days=1))
    assert _texts(services.retrieve(workspace_id=workspace.pk, query="budgets rule")) == [
        "Active rule about budgets"
    ]


def test_hard_rules_are_always_included_and_first(workspace) -> None:
    _mem(workspace, "Never claim guaranteed results", is_hard_rule=True, kind=MemoryKind.RULE)
    _mem(workspace, "Prefer short subject lines")
    hits = services.retrieve(workspace_id=workspace.pk, query="subject lines")
    assert _texts(hits)[0] == "Never claim guaranteed results" and hits[0].why == "hard rule"
    unrelated = services.retrieve(workspace_id=workspace.pk, query="totally different topic")
    assert _texts(unrelated) == ["Never claim guaranteed results"]
    assert (
        len(services.retrieve(workspace_id=workspace.pk, query="x", limit=0)) == 1
    )  # hard rule survives limit


def test_scope_filters_and_workspace_isolation(workspace, owner) -> None:
    from workspaces.services import create_workspace

    _mem(
        workspace,
        "Instagram captions use two hashtags",
        scope=MemoryScope.CHANNEL,
        scope_ref="instagram",
    )
    _mem(workspace, "LinkedIn posts avoid emojis", scope=MemoryScope.CHANNEL, scope_ref="linkedin")
    _mem(
        workspace, "Only for the ads agent about hashtags", scope=MemoryScope.AGENT, scope_ref="ads"
    )
    hits = services.retrieve(
        workspace_id=workspace.pk, query="hashtags emojis captions", channel="instagram"
    )
    assert _texts(hits) == ["Instagram captions use two hashtags"]
    agent_hits = services.retrieve(workspace_id=workspace.pk, query="hashtags", agent="ads")
    assert "Only for the ads agent about hashtags" in _texts(agent_hits)

    other = create_workspace(name="Other Co", owner=owner)
    assert services.retrieve(workspace_id=other.pk, query="hashtags") == []


def test_budget_limit_and_usage_tracking(workspace) -> None:
    for i in range(6):
        _mem(workspace, f"Preference {i} about landing pages and conversion rate testing")
    assert len(services.retrieve(workspace_id=workspace.pk, query="landing pages", limit=3)) == 3
    tiny = services.retrieve(workspace_id=workspace.pk, query="landing pages", token_budget=10)
    assert len(tiny) <= 1
    assert MemoryItem.objects.filter(usage_count__gt=0).exists()
    before = MemoryItem.objects.filter(usage_count__gt=0).count()
    services.retrieve(workspace_id=workspace.pk, query="landing pages", track_usage=False)
    assert MemoryItem.objects.filter(usage_count__gt=0).count() == before


def test_confidence_lifts_better_supported_items(workspace) -> None:
    _mem(workspace, "Weak guess about subject lines", confidence=0.05)
    _mem(workspace, "Strong finding about subject lines", confidence=0.95)
    assert _texts(services.retrieve(workspace_id=workspace.pk, query="subject lines"))[
        0
    ].startswith("Strong")


# ---- lifecycle --------------------------------------------------------------------------------


def test_activate_supersede_and_retire(workspace, owner) -> None:
    old = _mem(workspace, "Budget ceiling is 50 dollars per day")
    proposal = _mem(
        workspace,
        "Budget ceiling is 80 dollars per day",
        status=MemoryStatus.PROPOSED,
        supersedes=old,
    )
    assert old.status == ACTIVE
    services.activate(workspace_id=workspace.pk, item_id=proposal.pk, user=owner)
    old.refresh_from_db()
    proposal.refresh_from_db()
    assert (
        proposal.status == ACTIVE
        and old.status == MemoryStatus.RETIRED
        and old.valid_to is not None
    )
    assert _texts(services.retrieve(workspace_id=workspace.pk, query="budget ceiling")) == [
        "Budget ceiling is 80 dollars per day"
    ]

    services.retire(workspace_id=workspace.pk, item_id=proposal.pk, user=owner, reject=True)
    proposal.refresh_from_db()
    assert proposal.status == MemoryStatus.REJECTED


def test_embedder_is_deterministic_and_normalised() -> None:
    a, b = (
        HashEmbedder().embed(["Short headlines win"]),
        HashEmbedder().embed(["Short headlines win"]),
    )
    assert a == b and abs(sum(x * x for x in a[0]) - 1.0) < 1e-9


# ---- feedback and reflection ------------------------------------------------------------------


def _decided(workspace, **kw) -> dict:
    return {
        "action_id": str(kw.pop("action_id", "01a0efe9-0000-7000-8000-000000000001")),
        "workspace_id": str(workspace.pk),
        "kind": "ads.meta.create_campaign",
        "agent": "ads",
        "verdict": "rejected",
        "reason": "tone",
        "note": "too pushy",
        "proposed": {"headline": "Buy now!!!", "budget": 10},
        "final": {"headline": "Buy now!!!", "budget": 10},
        **kw,
    }


def test_feedback_capture_is_idempotent_and_lists_edited_fields(workspace) -> None:
    payload = _decided(
        workspace, verdict="approved", final={"headline": "Learn more", "budget": 10}
    )
    services.record_feedback(payload)
    services.record_feedback(payload)
    event = FeedbackEvent.objects.get()
    assert event.changed_fields == ["headline"]


REFLECTION = {
    "items": [
        {
            "text": "Avoid pushy urgency in headlines",
            "kind": "rule",
            "evidence": ["rejected #1 tone", "rejected #2 tone"],
            "confidence": 0.7,
        },
        {
            "text": "Owner likes emoji in captions",
            "kind": "preference",
            "evidence": ["one comment"],
            "confidence": 0.9,
        },
        {
            "text": "Never discount below 20 percent",
            "kind": "rule",
            "evidence": [],
            "explicit_instruction": True,
        },
    ]
}


@override_settings(MODEL_ROUTING={"analyst": "claude-sonnet-5-5"})
def test_reflection_proposes_only_well_supported_items_and_marks_feedback_processed(
    workspace,
) -> None:
    for n in range(3):
        services.record_feedback(
            _decided(workspace, action_id=f"01a0efe9-0000-7000-8000-00000000010{n}")
        )
    gateway = FakeGateway([text_response(json.dumps(REFLECTION))])
    created = reflection.run_reflection(workspace.pk, LlmService(gateway))

    assert [i.text for i in created] == [
        "Avoid pushy urgency in headlines",
        "Never discount below 20 percent",
    ]
    assert all(i.status == MemoryStatus.PROPOSED and i.source == "reflection" for i in created)
    assert not FeedbackEvent.objects.filter(processed=False).exists()
    prompt_text = gateway.requests[0][0].system[0].text
    assert "rejected (reason: tone)" in prompt_text and "too pushy" in prompt_text
    # Proposed items are invisible to agents until a human activates them.
    assert services.retrieve(workspace_id=workspace.pk, query="pushy headlines") == []


@override_settings(MODEL_ROUTING={"analyst": "claude-sonnet-5-5"})
def test_reflection_without_feedback_makes_no_llm_call(workspace) -> None:
    gateway = FakeGateway([])
    assert reflection.run_reflection(workspace.pk, LlmService(gateway)) == []
    assert gateway.requests == []
