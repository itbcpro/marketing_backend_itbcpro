"""What every demo step shares: the workspace, its people, a seeded random source and the clock."""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone

from audit.actors import Actor
from crm.contacts.models import Contact
from workspaces.models import Membership, Workspace

DEMO_DOMAIN = "demo.example.com"
DEMO_TAG = "demo"
SEED = 20261120
PHONE_PREFIX = "+1416555"  # 555 numbers are reserved for fiction


@dataclass
class Demo:
    workspace: Workspace
    members: list[Membership]
    actor: Actor
    rng: random.Random = field(default_factory=lambda: random.Random(SEED))
    now: datetime = field(default_factory=timezone.now)
    contacts: list[Contact] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def wid(self) -> uuid.UUID:
        return self.workspace.pk

    @property
    def owner(self) -> Membership:
        return next(m for m in self.members if m.role == "owner")

    def user_ids(self) -> list[uuid.UUID]:
        return [m.user_id for m in self.members]

    def ago(self, **delta: float) -> datetime:
        return self.now - timedelta(**delta)

    def ahead(self, **delta: float) -> datetime:
        return self.now + timedelta(**delta)

    def count(self, key: str, amount: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + amount

    def phone(self, index: int) -> str:
        return f"{PHONE_PREFIX}{index:04d}"


def build(workspace: Workspace, owner_email: str | None) -> Demo:
    members = list(
        Membership.objects.filter(workspace=workspace).select_related("user").order_by("created_at")
    )
    lead = next((m for m in members if m.user.email == owner_email), None)
    lead = lead or next(m for m in members if m.role == "owner")
    demo = Demo(workspace=workspace, members=members, actor=Actor.human(lead.user_id))
    demo.contacts = list(
        Contact.objects.for_workspace(workspace.pk)
        .active()
        .filter(email__endswith=f"@{DEMO_DOMAIN}")
        .order_by("created_at", "id")
    )
    return demo
