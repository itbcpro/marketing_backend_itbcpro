"""The first steps in a new workspace, and which of them are done."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from crm.contacts import services as contacts
from crm.inbox import services as inbox
from crm.pipelines import services as pipelines
from crm.templates import services as templates
from workspaces.models import Membership

# Each step names the screen that does it; the web app turns the key into words.
STEPS = ("contacts", "channel", "pipeline", "template", "team")


@dataclass(frozen=True)
class Step:
    key: str
    done: bool
    path: str


def checklist(workspace_id: uuid.UUID) -> list[Step]:
    done = {
        "contacts": contacts.count_active(workspace_id) > 0,
        "channel": inbox.any_account(workspace_id),
        "pipeline": pipelines.list_pipelines(workspace_id).exists(),
        "template": bool(templates.list_templates(workspace_id)),
        "team": Membership.objects.filter(workspace_id=workspace_id).count() > 1,
    }
    paths = {
        "contacts": "/crm/contacts",
        "channel": "/crm/email",
        "pipeline": "/crm/pipelines",
        "template": "/crm/templates",
        "team": "/settings",
    }
    return [Step(key, done[key], paths[key]) for key in STEPS]
