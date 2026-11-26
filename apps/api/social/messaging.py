"""Direct messages: understand a conversation, capture leads, draft or send a safe reply, hand off.

The model drafts; code decides. A message is only ever sent by itself when all of these hold: the
owner chose "auto", the 24 hour reply window is open, the person did not opt out, the topic is not
on the always-human list, the model is confident, the text passes the hard rules and the hourly
and per-conversation caps allow it. Anything else becomes a draft or a handoff with a summary.
"""

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.utils import timezone

from actions.models import ActionStatus
from core.errors import DomainError
from core.logging import get_logger
from social import ai, changes, response_settings, text_rules
from social.models import (
    ConversationStatus,
    SocialAccount,
    SocialConversation,
    SocialMessage,
)
from social.response_settings import MESSAGE_KIND
from social.schemas import DmAnalysis, MessageSettings

log = get_logger(__name__)

PROMPT_KEY = "social/instagram-dm"
HISTORY = 20
REPLY_WINDOW = timedelta(hours=24)
AWAY_COOLDOWN = timedelta(hours=12)
MAX_REPLY = 1000
MAX_TEXT_IN_PROMPT = 1000


def window_open(conversation: SocialConversation) -> bool:
    last = conversation.last_inbound_at
    return bool(last and timezone.now() - last < REPLY_WINDOW)


def _history(conversation: SocialConversation) -> list[dict[str, Any]]:
    rows = list(conversation.messages.order_by("-sent_at")[:HISTORY])[::-1]
    return [
        {
            "from": "person" if m.inbound else "company",
            "text": m.text[:MAX_TEXT_IN_PROMPT],
            "attachments": len(m.attachments or []),
            "at": m.sent_at.isoformat(),
        }
        for m in rows
    ]


def _rules(cfg: MessageSettings) -> str:
    domains = ", ".join(cfg.allowed_domains) or "none (do not include links)"
    return (
        f"- Always hand off these topics: {', '.join(cfg.always_human_intents) or 'none'}.\n"
        f"- Hand off when the person writes any of: {', '.join(cfg.handoff_keywords) or 'none'}.\n"
        f"- Allowed link domains: {domains}.\n"
        f"- Lead capture is {'on' if cfg.capture_leads else 'off'}."
    )


def analyse(conversation: SocialConversation, cfg: MessageSettings) -> DmAnalysis:
    return ai.run_prompt(
        conversation.workspace_id,
        PROMPT_KEY,
        DmAnalysis,
        memory_query="instagram direct message customer questions",
        user_message="Analyse the conversation and write the reply.",
        tone=cfg.tone,
        knowledge=cfg.knowledge_base or "(nothing provided: answer only from the brand profile)",
        rules=_rules(cfg),
        conversation=ai.untrusted("instagram_messages", _history(conversation)),
        now=timezone.now().isoformat(timespec="minutes"),
    )


def inside_business_hours(cfg: MessageSettings, now: datetime | None = None) -> bool:
    hours = cfg.business_hours
    if hours is None:
        return True
    local = (now or timezone.now()).astimezone(ZoneInfo(hours.timezone))
    if local.weekday() not in hours.days:
        return False
    return hours.start <= local.strftime("%H:%M") < hours.end


def _ai_messages_since(conversation: SocialConversation, delta: timedelta) -> int:
    return conversation.messages.filter(
        inbound=False, author="ai", sent_at__gte=timezone.now() - delta
    ).count()


def _sent_last_hour(account: SocialAccount) -> int:
    return SocialMessage.objects.filter(
        conversation__account=account,
        inbound=False,
        author="ai",
        sent_at__gte=timezone.now() - timedelta(hours=1),
    ).count()


def merge_lead(
    conversation: SocialConversation, analysis: DmAnalysis, cfg: MessageSettings
) -> None:
    if not (cfg.capture_leads and analysis.lead.is_lead):
        return
    lead = {
        **conversation.lead,
        **{k: v for k, v in analysis.lead.model_dump().items() if k != "is_lead" and v},
    }
    lead.setdefault("status", "new")
    lead["captured_at"] = lead.get("captured_at") or timezone.now().isoformat()
    conversation.lead = lead


def _clean(text: str, cfg: MessageSettings) -> text_rules.Cleaned:
    return text_rules.clean_reply(
        text,
        max_chars=MAX_REPLY,
        allowed_domains=cfg.allowed_domains,
        blocked_words=[],
        forbid_contact_details=False,
    )


def _last_inbound_text(conversation: SocialConversation) -> str:
    last = conversation.messages.filter(inbound=True).order_by("-sent_at").first()
    return last.text if last else ""


def _hand_off(conversation: SocialConversation, reason: str) -> None:
    conversation.status = ConversationStatus.HANDOFF
    conversation.handoff_reason = reason[:300]
    conversation.bot_paused = True


def apply_analysis(
    conversation: SocialConversation, analysis: DmAnalysis, cfg: MessageSettings
) -> tuple[str, bool]:
    """Store the analysis; return ``(reply text, may send automatically)``."""
    conversation.intent = analysis.intent
    conversation.urgency = analysis.urgency
    conversation.sentiment = analysis.sentiment
    conversation.language = analysis.language[:20]
    conversation.summary = analysis.summary
    conversation.analysed_through = conversation.last_inbound_at
    merge_lead(conversation, analysis, cfg)
    cleaned = _clean(analysis.reply, cfg) if analysis.reply else text_rules.Cleaned("")
    reply = cleaned.text
    must_hand_off = (
        analysis.handoff
        or analysis.intent in cfg.always_human_intents
        or analysis.confidence < cfg.min_confidence
        or bool(cleaned.violations)
        or not reply
    )
    if analysis.intent == "spam":
        conversation.status = ConversationStatus.CLOSED
        conversation.needs_reply = False
        return "", False
    if must_hand_off:
        reason = analysis.handoff_reason or "needs a person to decide"
        _hand_off(conversation, reason)
        away = (
            cfg.away_message
            and not inside_business_hours(cfg)
            and not _ai_messages_since(conversation, AWAY_COOLDOWN)
        )
        return (cfg.away_message if away else reply), bool(away)
    return reply, True


def _greeting(conversation: SocialConversation, cfg: MessageSettings, reply: str) -> str:
    if cfg.greeting and not conversation.messages.filter(inbound=False).exists():
        return f"{cfg.greeting}\n\n{reply}"[:MAX_REPLY]
    return reply


def _gate(conversation: SocialConversation, cfg: MessageSettings) -> str:
    """Rules that need no AI: skip, opt-out and handoff words. Empty string means "analyse"."""
    closed = {ConversationStatus.OPTED_OUT, ConversationStatus.CLOSED}
    paused = conversation.bot_paused or conversation.status == ConversationStatus.HANDOFF
    already = conversation.analysed_through == conversation.last_inbound_at
    if conversation.status in closed or paused or not conversation.needs_reply or already:
        return "skipped"
    text = _last_inbound_text(conversation)
    if text_rules.mentions_any(text, cfg.opt_out_keywords):
        conversation.status = ConversationStatus.OPTED_OUT
        conversation.bot_paused = True
        conversation.needs_reply = False
        outcome = "opted_out"
    elif text_rules.mentions_any(text, cfg.handoff_keywords):
        _hand_off(conversation, "the person asked for a human")
        outcome = "handoff"
    else:
        return ""
    conversation.analysed_through = conversation.last_inbound_at
    conversation.save()
    return outcome


def process_conversation(conversation: SocialConversation, cfg: MessageSettings) -> str:
    """Handle the newest unanswered message of one conversation. Returns what happened."""
    decided = _gate(conversation, cfg)
    if decided:
        return decided
    try:
        analysis = analyse(conversation, cfg)
    except DomainError as exc:
        log.warning("dm_analysis_failed", code=exc.code.value)
        return "failed"
    reply, may_send = apply_analysis(conversation, analysis, cfg)
    if reply and conversation.status != ConversationStatus.HANDOFF:
        reply = _greeting(conversation, cfg, reply)
    conversation.draft_reply = reply
    conversation.save()
    if reply and may_send and cfg.mode == "auto" and _may_auto_send(conversation, cfg):
        return "sent" if _propose_send(conversation, reply) else "drafted"
    return "drafted" if reply else "handoff"


def _may_auto_send(conversation: SocialConversation, cfg: MessageSettings) -> bool:
    return (
        window_open(conversation)
        and _ai_messages_since(conversation, timedelta(hours=24)) < cfg.max_auto_per_conversation
        and _sent_last_hour(conversation.account) < cfg.max_auto_per_hour
    )


def _propose_send(conversation: SocialConversation, text: str) -> bool:
    action = changes.propose_as_responder(
        conversation.workspace_id,
        MESSAGE_KIND,
        {
            "connection_id": str(conversation.account.connection_id),
            "conversation_id": str(conversation.pk),
            "recipient_id": conversation.participant_id,
            "text": text,
        },
        summary=f"Reply to {conversation.participant_username or 'a message'}",
    )
    return action.status in {ActionStatus.APPROVED, ActionStatus.AWAITING_APPROVAL}


def process_account(account: SocialAccount) -> dict[str, int]:
    cfg = response_settings.load(account).messages
    counts: dict[str, int] = {}
    if not cfg.enabled or cfg.mode == "off":
        return counts
    waiting = SocialConversation.objects.filter(
        account=account, needs_reply=True, bot_paused=False, status=ConversationStatus.OPEN
    ).select_related("account")[:25]
    for conversation in waiting:
        outcome = process_conversation(conversation, cfg)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


# --- human actions ----------------------------------------------------------------------------


def take_over(conversation: SocialConversation, user_id: Any) -> SocialConversation:
    conversation.status = ConversationStatus.HANDOFF
    conversation.assigned_to_id = user_id
    conversation.bot_paused = True
    conversation.save()
    return conversation


def hand_back(conversation: SocialConversation) -> SocialConversation:
    conversation.status = ConversationStatus.OPEN
    conversation.assigned_to = None
    conversation.bot_paused = False
    conversation.handoff_reason = ""
    conversation.analysed_through = None
    conversation.save()
    return conversation
