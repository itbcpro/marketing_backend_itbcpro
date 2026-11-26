"""Access requests: download everything held about one contact."""

from __future__ import annotations

import json
import uuid

from django.http import HttpRequest, HttpResponse
from ninja import Router

from audit.actors import Actor
from crm.contacts import services as contacts
from crm.privacy import export
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-privacy"], auth=api_auth)


@router.get(
    "/contacts/{contact_id}/privacy-export",
    response={200: None},
    operation_id="crm_contact_privacy_export",
)
def privacy_export(request: HttpRequest, contact_id: uuid.UUID) -> HttpResponse:
    """The person's data as a JSON file. Only people allowed to export may ask, and it is logged."""
    m = resolve_workspace(request, Permission.CRM_IMPORT_EXPORT)
    contacts.get_visible_contact(m, contact_id)
    data = export.export_contact(m.workspace_id, Actor.human(m.user_id), contact_id)
    response = HttpResponse(json.dumps(data, indent=2), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="contact-{contact_id}.json"'
    return response
