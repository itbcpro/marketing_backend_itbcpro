"""Action kinds for Instagram: replies, hiding, messages, post settings and publishing.

Risk classes: hiding a comment and toggling comments are reversible (R1); anything public or sent
to a person is R2; deleting a comment is irreversible (R3, never automatic). Publishing runs the
resumable flow in ``publishing``. Every executor re-checks local state first, so a retry or a second
approval never produces a duplicate reply.
"""

from typing import Any

from django.utils import timezone

from actions.models import ProposedAction, RiskClass
from actions.registry import ActionKind, register_action_kind
from connections.models import Connection, ConnectionStatus
from core.errors import NotFoundError
from social import publishing
from social.instagram import factory
from social.instagram.client import PROVIDER
from social.models import (
    CommentStatus,
    SocialComment,
    SocialConversation,
    SocialMedia,
    SocialMessage,
)
from social.response_settings import PREFIX
from social.schemas import (
    DeleteComment,
    HideComment,
    PublishPost,
    ReplyComment,
    SendMessage,
    SetCommentsEnabled,
)


def _connection(action: ProposedAction) -> Connection:
    found = (
        Connection.objects.filter(
            pk=str(action.effective_payload["connection_id"]),
            workspace_id=action.workspace_id,
            provider=PROVIDER,
        )
        .exclude(status=ConnectionStatus.REVOKED)
        .first()
    )
    if found is None:
        raise NotFoundError("Instagram connection not found for this workspace")
    return found


def _by(action: ProposedAction) -> str:
    return "human" if action.requested_by.startswith("user:") else "ai"


def _comment(action: ProposedAction, external_id: str) -> SocialComment | None:
    return SocialComment.objects.filter(
        workspace_id=action.workspace_id,
        account__connection_id=_connection(action).pk,
        external_id=external_id,
    ).first()


# --- comments ---------------------------------------------------------------------------------


def _already_posted(client: Any, action: ProposedAction, payload: ReplyComment) -> str | None:
    """On a retry, the id of the reply an earlier attempt posted before its answer was lost."""
    if action.attempts <= 1:
        return None
    for existing in client.comment_replies(payload.comment_id):
        if str(existing.get("text") or "").strip() == payload.text.strip():
            return str(existing["id"])
    return None


def _mark_replied(
    comment: SocialComment | None, action: ProposedAction, text: str, reply_id: str
) -> None:
    if comment is None:
        return
    comment.status = CommentStatus.REPLIED
    comment.reply_text = text
    comment.reply_external_id = reply_id
    comment.handled_by = _by(action)
    comment.handled_at = timezone.now()
    comment.action_id = action.pk
    comment.save()


def _reply_executor(action: ProposedAction) -> dict[str, Any]:
    payload = ReplyComment.model_validate(action.effective_payload)
    comment = _comment(action, payload.comment_id)
    if (
        comment is not None
        and comment.status == CommentStatus.REPLIED
        and comment.reply_external_id
    ):
        return {
            "comment_id": payload.comment_id,
            "reply_id": comment.reply_external_id,
            "skipped": True,
        }
    with factory.open_client(_connection(action)) as client:
        reply_id = _already_posted(client, action, payload)
        skipped = reply_id is not None
        if reply_id is None:
            reply_id = str(
                client.reply_to_comment(payload.comment_id, payload.text).get("id") or ""
            )
    _mark_replied(comment, action, payload.text, reply_id)
    result: dict[str, Any] = {"comment_id": payload.comment_id, "reply_id": reply_id}
    return {**result, "skipped": True} if skipped else result


def _hide_executor(action: ProposedAction) -> dict[str, Any]:
    payload = HideComment.model_validate(action.effective_payload)
    with factory.open_client(_connection(action)) as client:
        client.hide_comment(payload.comment_id, hidden=payload.hidden)
    comment = _comment(action, payload.comment_id)
    if comment is not None:
        comment.hidden = payload.hidden
        if payload.hidden:
            comment.status = CommentStatus.HIDDEN
            comment.handled_by = _by(action)
            comment.handled_at = timezone.now()
        comment.action_id = action.pk
        comment.save()
    return {"comment_id": payload.comment_id, "hidden": payload.hidden}


def _hide_rollback(action: ProposedAction) -> dict[str, Any]:
    payload = HideComment.model_validate(action.effective_payload)
    return {**payload.model_dump(mode="json"), "hidden": not payload.hidden}


def _delete_executor(action: ProposedAction) -> dict[str, Any]:
    payload = DeleteComment.model_validate(action.effective_payload)
    with factory.open_client(_connection(action)) as client:
        client.delete_comment(payload.comment_id)
    SocialComment.objects.filter(
        workspace_id=action.workspace_id, external_id=payload.comment_id
    ).delete()
    return {"comment_id": payload.comment_id, "deleted": True}


def _toggle_executor(action: ProposedAction) -> dict[str, Any]:
    payload = SetCommentsEnabled.model_validate(action.effective_payload)
    with factory.open_client(_connection(action)) as client:
        client.set_comments_enabled(payload.media_id, enabled=payload.enabled)
    SocialMedia.objects.filter(
        workspace_id=action.workspace_id, external_id=payload.media_id
    ).update(comments_enabled=payload.enabled)
    return {"media_id": payload.media_id, "enabled": payload.enabled}


def _toggle_rollback(action: ProposedAction) -> dict[str, Any]:
    payload = SetCommentsEnabled.model_validate(action.effective_payload)
    return {**payload.model_dump(mode="json"), "enabled": not payload.enabled}


# --- messages ---------------------------------------------------------------------------------


def _message_executor(action: ProposedAction) -> dict[str, Any]:
    payload = SendMessage.model_validate(action.effective_payload)
    conversation = SocialConversation.objects.filter(
        pk=payload.conversation_id, workspace_id=action.workspace_id
    ).first()
    if conversation is None:
        raise NotFoundError("conversation not found")
    with factory.open_client(_connection(action)) as client:
        sent = client.send_message(payload.recipient_id, payload.text)
    now = timezone.now()
    message_id = str(sent.get("message_id") or sent.get("id") or f"local-{action.pk}")
    SocialMessage.objects.get_or_create(
        conversation=conversation,
        external_id=message_id,
        defaults={
            "workspace_id": action.workspace_id,
            "inbound": False,
            "author": _by(action),
            "text": payload.text,
            "sent_at": now,
        },
    )
    conversation.last_message_at = conversation.last_outbound_at = now
    conversation.needs_reply = False
    conversation.draft_reply = ""
    conversation.save()
    return {"conversation_id": str(conversation.pk), "message_id": message_id}


# --- publishing -------------------------------------------------------------------------------


def _publish_preview(
    action: ProposedAction,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    payload = PublishPost.model_validate(action.payload)
    return None, {
        "kind": payload.kind,
        "caption": payload.caption,
        "media_count": len(payload.media),
        "first_comment": payload.first_comment,
    }


def _kinds() -> list[ActionKind]:
    return [
        ActionKind(
            kind=PREFIX + "reply_comment",
            payload_model=ReplyComment,
            risk=RiskClass.R2,
            executor=_reply_executor,
            description="Reply publicly to a comment on the Instagram account.",
        ),
        ActionKind(
            kind=PREFIX + "hide_comment",
            payload_model=HideComment,
            risk=RiskClass.R1,
            executor=_hide_executor,
            description="Hide (or unhide) a comment. Reversible.",
            rollback_kind=PREFIX + "hide_comment",
            rollback_payload=_hide_rollback,
        ),
        ActionKind(
            kind=PREFIX + "delete_comment",
            payload_model=DeleteComment,
            risk=RiskClass.R3,
            executor=_delete_executor,
            description="Delete a comment permanently. Irreversible, always needs a human.",
        ),
        ActionKind(
            kind=PREFIX + "set_comments_enabled",
            payload_model=SetCommentsEnabled,
            risk=RiskClass.R1,
            executor=_toggle_executor,
            description="Turn comments on or off for one post.",
            rollback_kind=PREFIX + "set_comments_enabled",
            rollback_payload=_toggle_rollback,
        ),
        ActionKind(
            kind=PREFIX + "send_message",
            payload_model=SendMessage,
            risk=RiskClass.R2,
            executor=_message_executor,
            description="Send a direct message in an existing Instagram conversation.",
        ),
        ActionKind(
            kind=PREFIX + "publish_post",
            payload_model=PublishPost,
            risk=RiskClass.R2,
            executor=publishing.execute,
            description="Publish an approved post (image, carousel, reel or story).",
            preview=_publish_preview,
            verify=publishing.verify,
        ),
    ]


def register_action_kinds() -> None:
    for kind in _kinds():
        register_action_kind(kind)
