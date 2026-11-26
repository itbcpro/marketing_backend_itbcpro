"""Who performed an action: a human, an agent, or the system itself."""

import uuid
from dataclasses import dataclass
from typing import Self

from audit.models import ActorType


@dataclass(frozen=True, slots=True)
class Actor:
    type: str
    id: str

    @classmethod
    def human(cls, user_id: uuid.UUID | str) -> Self:
        return cls(ActorType.HUMAN, str(user_id))

    @classmethod
    def agent(cls, name: str) -> Self:
        return cls(ActorType.AGENT, name)

    @classmethod
    def system(cls, name: str = "system") -> Self:
        return cls(ActorType.SYSTEM, name)
