"""Instagram: pieces go through the existing post pipeline (checks, approval record, scheduling,
kill switch, audit and verification), so the studio adds no second way to post."""

import uuid
from typing import ClassVar

from django.db import transaction

from core.errors import NotFoundError, ValidationFailedError
from identity.models import User
from social import drafts
from social.models import DraftStatus, PostDraft, SocialAccount
from studio.models import ContentPiece, PieceKind, PieceStatus
from studio.publishers.ports import AccountInfo, HandOver, PieceUpdate

KIND_MAP: dict[str, str] = {
    PieceKind.IMAGE: "image",
    PieceKind.CAROUSEL: "carousel",
    PieceKind.VIDEO: "reel",
    PieceKind.STORY: "story",
}
STATUS_MAP: dict[str, str] = {
    DraftStatus.SCHEDULED: PieceStatus.SCHEDULED,
    DraftStatus.PUBLISHING: PieceStatus.PUBLISHING,
    DraftStatus.PUBLISHED: PieceStatus.PUBLISHED,
    DraftStatus.FAILED: PieceStatus.FAILED,
    DraftStatus.CANCELLED: PieceStatus.CANCELLED,
}


class InstagramPublisher:
    channel: ClassVar[str] = "instagram"

    def accounts(self, workspace_id: uuid.UUID) -> list[AccountInfo]:
        rows = SocialAccount.objects.filter(workspace_id=workspace_id, connection__status="active")
        return [
            AccountInfo(str(a.pk), f"@{a.username}" if a.username else "", a.name) for a in rows
        ]

    def _account(self, piece: ContentPiece, account_id: str) -> SocialAccount:
        rows = SocialAccount.objects.filter(
            workspace_id=piece.workspace_id, connection__status="active"
        )
        found = rows.filter(pk=account_id).first() if account_id else rows.first()
        if found is None:
            raise NotFoundError("connect an Instagram account first")
        return found

    def _draft(self, piece: ContentPiece, account: SocialAccount, approver: User) -> PostDraft:
        kind = KIND_MAP.get(piece.kind)
        if kind is None:
            raise ValidationFailedError("Instagram has no text-only posts")
        copy = piece.copy
        return PostDraft.objects.create(
            workspace_id=piece.workspace_id,
            account=account,
            kind=kind,
            status=DraftStatus.DRAFTED,
            title=piece.title,
            brief=piece.brief,
            caption=str(copy.get("body", "")),
            hashtags=list(copy.get("hashtags", [])),
            first_comment=str(copy.get("first_comment", "")),
            media=piece.media,
            cover_asset_id=piece.cover_asset_id,
            pillar=piece.pillar[:30],
            origin="ai",
            created_by=approver,
            ai={"studio_piece": str(piece.pk)},
        )

    @transaction.atomic
    def hand_over(self, piece: ContentPiece, approver: User, account_id: str) -> HandOver:
        account = self._account(piece, account_id)
        draft = self._draft(piece, account, approver)
        drafts.approve(draft, approver, piece.scheduled_at)  # a failed check rolls everything back
        return HandOver({"draft_id": str(draft.pk), "account_id": str(account.pk)})

    def _draft_of(self, piece: ContentPiece) -> PostDraft | None:
        draft_id = piece.external.get("draft_id")
        return (
            PostDraft.objects.filter(pk=draft_id, workspace_id=piece.workspace_id).first()
            if draft_id
            else None
        )

    def refresh(self, piece: ContentPiece) -> PieceUpdate | None:
        draft = self._draft_of(piece)
        if draft is None:
            return None
        status = STATUS_MAP.get(DraftStatus(draft.status))
        if status is None:
            return PieceUpdate(PieceStatus.DRAFT)  # taken back to a draft on the Instagram side
        return PieceUpdate(
            status,
            permalink=draft.permalink,
            published_at=draft.published_at,
            error_code=draft.error_code,
            error_detail=draft.error_detail,
        )

    def withdraw(self, piece: ContentPiece) -> None:
        draft = self._draft_of(piece)
        if draft is not None and draft.status == DraftStatus.SCHEDULED:
            drafts.unschedule(draft)
            drafts.cancel(draft)
