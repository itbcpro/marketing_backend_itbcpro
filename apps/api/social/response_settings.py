"""Response settings of an account and the autonomy they grant.

"Auto" in a setting is the owner telling the platform that the community responder may act alone
for that kind of reply. It is stored as an autonomy grant, so the same kill switch, guardrails and
audit trail as every other action apply. Turning "auto" off removes the grant again.
"""

import uuid
from typing import Any

from actions.models import AutonomyGrant
from audit.actors import Actor
from audit.services import record_event
from identity.models import User
from social.models import AccountSettings, SocialAccount
from social.schemas import CommentSettings, MessageSettings, ReplySettings

AGENT = "community-responder"
PREFIX = "social.instagram."
REPLY_KIND = PREFIX + "reply_comment"
HIDE_KIND = PREFIX + "hide_comment"
MESSAGE_KIND = PREFIX + "send_message"
ACT_LEVEL = 3
SPEND_LIMIT = {"max_spend_micros": 1}  # replies cost nothing; R2 needs any positive limit


def load(account: SocialAccount) -> ReplySettings:
    row = AccountSettings.objects.filter(account=account).first()
    return ReplySettings.model_validate(row.config if row else {})


def wants_auto(settings: ReplySettings) -> dict[str, bool]:
    comments, messages = settings.comments, settings.messages
    return {
        REPLY_KIND: comments.enabled and "auto" in comments.rules.values(),
        HIDE_KIND: comments.enabled and (comments.hide_spam or comments.hide_abuse),
        MESSAGE_KIND: messages.enabled and messages.mode == "auto",
    }


def needs_autonomy(settings: ReplySettings, previous: ReplySettings) -> bool:
    """Whether saving grants more autonomy than before (that needs a more privileged user)."""
    before, after = wants_auto(previous), wants_auto(settings)
    return any(after[kind] and not before[kind] for kind in after)


def _sync_grants(workspace_id: uuid.UUID, settings: ReplySettings) -> None:
    for kind, wanted in wants_auto(settings).items():
        if wanted:
            AutonomyGrant.objects.update_or_create(
                workspace_id=workspace_id,
                agent=AGENT,
                action_kind=kind,
                defaults={"level": ACT_LEVEL, "limits": SPEND_LIMIT, "expires_at": None},
            )
        else:
            AutonomyGrant.objects.filter(
                workspace_id=workspace_id, agent=AGENT, action_kind=kind
            ).delete()


def save(account: SocialAccount, settings: ReplySettings, user: User) -> ReplySettings:
    row, _ = AccountSettings.objects.get_or_create(
        account=account, defaults={"workspace_id": account.workspace_id}
    )
    before: dict[str, Any] = row.config
    row.config = settings.model_dump(mode="json")
    row.updated_by = user
    row.save()
    _sync_grants(account.workspace_id, settings)
    record_event(
        action="social.settings_changed",
        actor=Actor.human(user.pk),
        workspace_id=account.workspace_id,
        target_type="social_account",
        target_id=account.pk,
        before={"auto": wants_auto(ReplySettings.model_validate(before))},
        after={"auto": wants_auto(settings)},
    )
    return settings


def disable_all_automation(account: SocialAccount, user: User | None = None) -> None:
    """The panic button: every "auto" becomes "draft" and every grant is removed."""
    settings = load(account)
    comments: CommentSettings = settings.comments
    messages: MessageSettings = settings.messages
    comments.rules = {k: ("draft" if v == "auto" else v) for k, v in comments.rules.items()}
    comments.hide_spam = comments.hide_abuse = False
    if messages.mode == "auto":
        messages.mode = "draft"
    row, _ = AccountSettings.objects.get_or_create(
        account=account, defaults={"workspace_id": account.workspace_id}
    )
    row.config = settings.model_dump(mode="json")
    row.updated_by = user
    row.save()
    _sync_grants(account.workspace_id, settings)
