from datetime import timedelta

import pytest
from django.utils import timezone

from actions import services as action_services
from actions.models import ProposedAction
from social import messaging, response_settings
from social.models import ConversationStatus, SocialConversation, SocialMessage
from social.schemas import ReplySettings
from social.tests.conftest import llm

pytestmark = pytest.mark.django_db


def dm(**fields):
    base = {
        "intent": "availability",
        "urgency": "normal",
        "sentiment": "neutral",
        "language": "English",
        "summary": "Asks whether onboarding is offered.",
        "reply": "Yes, we do onboarding. What size is your team?",
        "handoff": False,
        "handoff_reason": "",
        "confidence": 0.95,
        "lead": {
            "is_lead": False,
            "name": "",
            "need": "",
            "contact": "",
            "budget": "",
            "timeline": "",
        },
    }
    return {**base, **fields}


def configure(account, owner, **message_fields):
    fields = {"enabled": True, "mode": "draft", **message_fields}
    return response_settings.save(
        account, ReplySettings.model_validate({"messages": fields}), owner
    )


def conversation(account, text="Do you do onboarding?", *, minutes_ago=5, **fields):
    now = timezone.now() - timedelta(minutes=minutes_ago)
    convo = SocialConversation.objects.create(
        workspace_id=account.workspace_id,
        account=account,
        external_id=f"t_{SocialConversation.objects.count()}",
        participant_id="u1",
        participant_username="alice",
        last_message_at=now,
        last_inbound_at=now,
        needs_reply=True,
        **fields,
    )
    SocialMessage.objects.create(
        workspace_id=account.workspace_id,
        conversation=convo,
        external_id=f"m-{convo.pk}",
        inbound=True,
        text=text,
        sent_at=now,
    )
    return convo


def run(account):
    return messaging.process_account(account)


def test_nothing_happens_while_messages_are_switched_off(account):
    convo = conversation(account)
    gateway = llm()
    assert run(account) == {}
    assert gateway.requests == [] and not convo.draft_reply


def test_a_faq_answer_becomes_a_draft_for_a_person(account, owner):
    configure(account, owner, knowledge_base="We offer onboarding for teams of any size.")
    convo = conversation(account)
    gateway = llm(dm())
    assert run(account) == {"drafted": 1}
    convo.refresh_from_db()
    assert convo.draft_reply.startswith("Yes, we do onboarding")
    assert convo.intent == "availability" and convo.status == ConversationStatus.OPEN
    system = gateway.requests[0][0].system[0].text
    assert "We offer onboarding" in system and "<untrusted_content" in system
    assert ProposedAction.objects.count() == 0


def test_auto_mode_sends_through_the_action_layer_inside_the_window(account, owner, fake):
    configure(account, owner, mode="auto")
    convo = conversation(account)
    llm(dm())
    assert run(account) == {"sent": 1}
    action = ProposedAction.objects.get()
    assert action.kind == "social.instagram.send_message" and action.auto_approved
    action_services.execute(action.pk)
    convo.refresh_from_db()
    assert not convo.needs_reply and convo.draft_reply == ""
    sent = fake.calls_to("POST", r"/messages$")[0]
    assert sent["recipient"] == {"id": "u1"} and sent["message"]["text"].startswith("Yes")
    assert convo.messages.filter(inbound=False, author="ai").count() == 1


def test_a_closed_reply_window_never_sends(account, owner):
    configure(account, owner, mode="auto")
    conversation(account, minutes_ago=60 * 30)
    llm(dm())
    assert run(account) == {"drafted": 1}
    assert ProposedAction.objects.count() == 0


def test_opt_out_words_stop_the_bot_without_asking_the_model(account, owner):
    configure(account, owner, mode="auto")
    convo = conversation(account, "STOP")
    gateway = llm()
    assert run(account) == {"opted_out": 1}
    convo.refresh_from_db()
    assert convo.status == ConversationStatus.OPTED_OUT and convo.bot_paused
    assert gateway.requests == []
    assert run(account) == {}  # never touched again


def test_a_request_for_a_human_hands_off_immediately(account, owner):
    configure(account, owner, mode="auto")
    convo = conversation(account, "Can I talk to a human please?")
    gateway = llm()
    assert run(account) == {"handoff": 1}
    convo.refresh_from_db()
    assert convo.status == ConversationStatus.HANDOFF and convo.bot_paused
    assert gateway.requests == []


def test_always_human_topics_hand_off_even_with_a_confident_reply(account, owner):
    configure(account, owner, mode="auto")
    convo = conversation(account, "I want to complain about your service")
    llm(dm(intent="complaint", sentiment="negative", reply="We are so sorry."))
    run(account)
    convo.refresh_from_db()
    assert convo.status == ConversationStatus.HANDOFF and convo.intent == "complaint"
    assert ProposedAction.objects.count() == 0 and convo.draft_reply


def test_low_confidence_hands_off(account, owner):
    configure(account, owner, mode="auto")
    convo = conversation(account)
    llm(dm(confidence=0.5))
    run(account)
    convo.refresh_from_db()
    assert convo.status == ConversationStatus.HANDOFF and ProposedAction.objects.count() == 0


def test_the_model_can_hand_off_with_a_reason_and_a_summary(account, owner):
    configure(account, owner)
    convo = conversation(account, "What is the price for 40 seats?")
    llm(
        dm(
            handoff=True,
            handoff_reason="No pricing in the approved knowledge",
            reply="",
            intent="pricing",
        )
    )
    run(account)
    convo.refresh_from_db()
    assert convo.status == ConversationStatus.HANDOFF
    assert convo.handoff_reason.startswith("No pricing") and convo.summary


def test_the_away_message_goes_out_when_no_one_is_in(account, owner):
    hours = {"timezone": "UTC", "days": [], "start": "09:00", "end": "17:00"}
    configure(
        account,
        owner,
        mode="auto",
        business_hours=hours,
        away_message="We are out of office, back tomorrow.",
    )
    convo = conversation(account, "Is anyone there? I need a quote")
    llm(dm(intent="quote_request", reply="Sure"))
    assert run(account) == {"sent": 1}
    assert ProposedAction.objects.get().payload["text"] == "We are out of office, back tomorrow."
    convo.refresh_from_db()
    assert convo.status == ConversationStatus.HANDOFF


def test_business_hours_logic():
    cfg = ReplySettings.model_validate(
        {
            "messages": {
                "business_hours": {"timezone": "UTC", "days": [0], "start": "09:00", "end": "17:00"}
            }
        }
    ).messages
    monday_noon = timezone.now().replace(year=2026, month=9, day=28, hour=12, minute=0)
    monday_night = monday_noon.replace(hour=22)
    tuesday = monday_noon.replace(day=29)
    assert messaging.inside_business_hours(cfg, monday_noon)
    assert not messaging.inside_business_hours(cfg, monday_night)
    assert not messaging.inside_business_hours(cfg, tuesday)


def test_leads_are_captured_with_only_what_the_person_said(account, owner):
    configure(account, owner)
    convo = conversation(
        account, "We need IT support for 12 people, email me at sam@clinic.example"
    )
    lead = {
        "is_lead": True,
        "name": "Sam",
        "need": "IT support for 12 people",
        "contact": "sam@clinic.example",
        "budget": "",
        "timeline": "",
    }
    llm(dm(intent="quote_request", lead=lead))
    run(account)
    convo.refresh_from_db()
    assert convo.lead["contact"] == "sam@clinic.example" and convo.lead["status"] == "new"
    assert "budget" not in convo.lead


def test_the_per_conversation_cap_stops_a_bot_loop(account, owner):
    configure(account, owner, mode="auto", max_auto_per_conversation=1)
    convo = conversation(account)
    SocialMessage.objects.create(
        workspace_id=account.workspace_id,
        conversation=convo,
        external_id="out",
        inbound=False,
        author="ai",
        text="Earlier answer",
        sent_at=timezone.now() - timedelta(hours=1),
    )
    llm(dm())
    assert run(account) == {"drafted": 1}


def test_links_outside_the_allowed_domains_hand_the_reply_to_a_person(account, owner):
    configure(account, owner, mode="auto", allowed_domains=["acme.example"])
    convo = conversation(account)
    llm(dm(reply="Book here https://evil.example/now"))
    run(account)
    convo.refresh_from_db()
    assert "evil.example" not in convo.draft_reply


def test_taking_over_and_handing_back(account, owner):
    convo = conversation(account)
    messaging.take_over(convo, owner.pk)
    convo.refresh_from_db()
    assert convo.status == ConversationStatus.HANDOFF and convo.bot_paused
    messaging.hand_back(convo)
    convo.refresh_from_db()
    assert convo.status == ConversationStatus.OPEN and not convo.bot_paused


def test_greeting_is_added_once_to_a_first_reply(account, owner):
    configure(account, owner, greeting="Hi from Acme!")
    convo = conversation(account)
    llm(dm())
    run(account)
    convo.refresh_from_db()
    assert convo.draft_reply.startswith("Hi from Acme!")


def test_a_model_failure_leaves_the_conversation_waiting(account, owner):
    from core.errors import RetryableProviderError
    from llm.fake import FakeGateway
    from llm.service import LlmService, set_llm_service

    configure(account, owner)
    convo = conversation(account)
    set_llm_service(LlmService(FakeGateway([RetryableProviderError("down", provider="x")])))
    assert run(account) == {"failed": 1}
    convo.refresh_from_db()
    assert convo.needs_reply and convo.analysed_through is None
