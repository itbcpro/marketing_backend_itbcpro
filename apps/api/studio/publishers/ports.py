"""Port for handing a finished piece to a channel's own publishing pipeline.

A publisher does not decide *when* or *whether*: the studio approves and schedules, then hands the
piece over once, and later asks for its state. A channel with no registered publisher (or no
connected account) falls back to the manual kit, where a person posts it by hand.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Protocol

from identity.models import User
from studio.models import Brand, ContentPiece


@dataclass(frozen=True, slots=True)
class AccountInfo:
    id: str
    handle: str
    label: str


@dataclass(frozen=True, slots=True)
class HandOver:
    external: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PieceUpdate:
    status: str
    permalink: str = ""
    published_at: datetime | None = None
    error_code: str = ""
    error_detail: str = ""


class Publisher(Protocol):
    channel: ClassVar[str]

    def accounts(self, workspace_id: uuid.UUID) -> list[AccountInfo]: ...

    def hand_over(self, piece: ContentPiece, approver: User, account_id: str) -> HandOver: ...

    def refresh(self, piece: ContentPiece) -> PieceUpdate | None: ...

    def withdraw(self, piece: ContentPiece) -> None: ...


_PUBLISHERS: dict[str, Publisher] = {}


def register_publisher(publisher: Publisher) -> None:
    _PUBLISHERS[publisher.channel] = publisher


def publisher_for(channel: str) -> Publisher | None:
    return _PUBLISHERS.get(channel)


def account_for(workspace_id: uuid.UUID, channel: str, wanted: str = "") -> AccountInfo | None:
    """The connected account a channel posts to: the chosen one, else the first available.

    With several companies in one workspace there is no safe "first": a post of one company must
    never go to another's account, so an account that was not chosen is not guessed.
    """
    publisher = publisher_for(channel)
    if publisher is None:
        return None
    accounts = publisher.accounts(workspace_id)
    if wanted:
        return next((a for a in accounts if a.id == wanted), None)
    several = Brand.objects.filter(workspace_id=workspace_id, archived_at__isnull=True).count() > 1
    return None if several or not accounts else accounts[0]
