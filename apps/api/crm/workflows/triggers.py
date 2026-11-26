"""Triggers: what makes a contact enter a workflow. One class per trigger, registered by key."""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Trigger:
    """Base of every trigger. Subclasses name the event they listen to and say when it counts."""

    key: ClassVar[str]
    event_type: ClassVar[str]
    config_model: ClassVar[type[BaseModel]] = Empty

    def contact_id(self, data: dict[str, Any]) -> uuid.UUID | None:
        raw = data.get("contact_id")
        return uuid.UUID(str(raw)) if raw else None

    def matches(self, data: dict[str, Any], config: dict[str, Any]) -> bool:
        return True


class ContactCreated(Trigger):
    key = "contact_created"
    event_type = "crm.contact.created"

    class Config(BaseModel):
        model_config = ConfigDict(extra="forbid")
        source: str = ""

    config_model = Config

    def matches(self, data: dict[str, Any], config: dict[str, Any]) -> bool:
        return not config.get("source") or data.get("source") == config["source"]


class TagAdded(Trigger):
    key = "tag_added"
    event_type = "crm.tag.added"

    class Config(BaseModel):
        model_config = ConfigDict(extra="forbid")
        tag: str

    config_model = Config

    def matches(self, data: dict[str, Any], config: dict[str, Any]) -> bool:
        return str(data.get("name", "")).lower() == str(config["tag"]).lower()


class StageReached(Trigger):
    key = "stage_reached"
    event_type = "crm.opportunity.stage_changed"

    class Config(BaseModel):
        model_config = ConfigDict(extra="forbid")
        stage_id: uuid.UUID

    config_model = Config

    def matches(self, data: dict[str, Any], config: dict[str, Any]) -> bool:
        return str(data.get("to_stage_id")) == str(config["stage_id"])


class DealStatusChanged(Trigger):
    key = "deal_status_changed"
    event_type = "crm.opportunity.status_changed"

    class Config(BaseModel):
        model_config = ConfigDict(extra="forbid")
        status: str

    config_model = Config

    def matches(self, data: dict[str, Any], config: dict[str, Any]) -> bool:
        return bool(data.get("to_status") == config["status"])


class InboundMessage(Trigger):
    key = "inbound_message"
    event_type = "crm.message.inbound"

    class Config(BaseModel):
        model_config = ConfigDict(extra="forbid")
        channel: str = ""

    config_model = Config

    def matches(self, data: dict[str, Any], config: dict[str, Any]) -> bool:
        return not config.get("channel") or data.get("channel") == config["channel"]


class LeadCaptured(Trigger):
    key = "lead_captured"
    event_type = "crm.lead.captured"

    class Config(BaseModel):
        model_config = ConfigDict(extra="forbid")
        form_id: str = ""

    config_model = Config

    def matches(self, data: dict[str, Any], config: dict[str, Any]) -> bool:
        return not config.get("form_id") or data.get("form_id") == config["form_id"]


class MissedCall(Trigger):
    key = "missed_call"
    event_type = "crm.call.missed"


_triggers: dict[str, Trigger] = {}


def register(trigger: Trigger) -> None:
    _triggers[trigger.key] = trigger


def get(key: str) -> Trigger | None:
    return _triggers.get(key)


def all_triggers() -> list[Trigger]:
    return sorted(_triggers.values(), key=lambda t: t.key)


def event_types() -> list[str]:
    return sorted({t.event_type for t in _triggers.values()})


for _cls in (
    ContactCreated,
    TagAdded,
    StageReached,
    DealStatusChanged,
    InboundMessage,
    LeadCaptured,
    MissedCall,
):
    register(_cls())
