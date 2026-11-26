"""After an erasure the analytics keep their counts but forget whom they were about."""

from __future__ import annotations

import uuid

from crm.reports.models import FirstResponse

NIL = uuid.UUID(int=0)


def anonymise_contact(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> int:
    rows = FirstResponse.objects.for_workspace(workspace_id).filter(contact_id=contact_id)
    return int(rows.update(contact_id=NIL))
