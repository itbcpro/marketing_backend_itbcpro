"""The hands-off path: after a piece is written, a channel set to auto approves it when it is clean.

Clean means no errors and no voice warnings. Anything else is held for a person with the reason
recorded. The approval is made as the person who enabled automation, and their right to approve is
checked at that moment, so removing their role stops publishing.
"""

from core.errors import DomainError
from core.logging import get_logger
from studio import config_store, services
from studio.models import ContentPiece, PieceStatus, StudioProfile

log = get_logger(__name__)

HELD = "held_reason"


def _hold(piece: ContentPiece, reason: str) -> None:
    piece.ai_state = {**piece.ai_state, HELD: reason}
    piece.save(update_fields=["ai_state", "updated_at"])


def _clean(piece: ContentPiece) -> bool:
    return not any(r["severity"] in {"error", "warning"} for r in piece.checks)


def after_written(piece: ContentPiece) -> bool:
    """Approve ``piece`` if its channel runs on auto and it passed every check."""
    config = config_store.load(piece.brand_id)
    plan = config.plan_for(piece.channel)
    if not (config.automation.enabled and plan.enabled and plan.publish_mode == "auto"):
        return False
    if piece.origin != "ai" or piece.status != PieceStatus.DRAFT:
        return False
    profile = (
        StudioProfile.objects.select_related("automation_user")
        .filter(brand_id=piece.brand_id)
        .first()
    )
    user = profile.automation_user if profile else None
    if user is None:
        _hold(piece, "no_automation_user")
        return False
    if not _clean(piece):
        _hold(piece, "needs_review")
        return False
    try:
        services.approve(piece, user)
    except DomainError as exc:
        log.warning("studio_auto_approve_held", code=exc.code.value)
        _hold(piece, exc.code.value)
        return False
    return True
