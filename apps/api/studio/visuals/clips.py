"""Video providers. ``cards`` renders the plan locally as branded scenes; a vendor that makes
real footage (Runway, Veo, Kling and so on) is one more class registered next to it."""

import uuid
from typing import ClassVar

from social import video
from social.schemas import ReelPlan
from studio.visuals.ports import VisualRequest


class CardsVideoProvider:
    key: ClassVar[str] = "cards"

    def available(self, workspace_id: uuid.UUID) -> bool:
        return True

    def make(
        self, request: VisualRequest, plan: ReelPlan, *, previous: uuid.UUID | None
    ) -> uuid.UUID:
        asset = video.store_reel(
            request.workspace_id,
            request.user_id,
            video.render(plan, request.palette),
            name=(request.title or "Video")[:80],
        )
        return asset.pk
